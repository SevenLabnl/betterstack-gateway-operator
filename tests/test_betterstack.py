"""The Better Stack client, against a fake transport.

Worth testing without a network because the two things most likely to be wrong here are
invisible until they bite: pagination that stops early (which would look like monitors
needing deletion) and a payload field the API silently ignores.
"""
import json

import pytest

from gateway_uptime.model import DesiredMonitor, ExistingMonitor
from gateway_uptime.providers.base import ProviderError
from gateway_uptime.providers.betterstack import BetterStack


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHTTP:
    """Records calls and replies from a scripted map of (method, path) -> payload."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, url, body))
        self.headers = dict(req.headers)
        # match on the end of the URL, longest first: /monitor-groups must not
        # answer for /monitor-groups/77/monitors, and neither may answer for a
        # ?page=2 continuation
        best = None
        for (m, fragment), payload in self.routes.items():
            if m == method and url.endswith(fragment):
                if best is None or len(fragment) > len(best[0]):
                    best = (fragment, payload)
        if best is None:
            raise AssertionError("unexpected call %s %s" % (method, url))
        payload = best[1]
        return FakeResponse(payload() if callable(payload) else payload)


def want(url="https://shop.example.com/"):
    return DesiredMonitor(hostname="shop.example.com", url=url, check_frequency=180,
                          request_timeout=30, expected_status_codes=(200, 204),
                          regions=("eu",), namespace="acme", route="shop")


GROUP_PAGE = {"data": [{"id": "77", "attributes": {"name": "test-cluster"}}],
              "pagination": {"next": None}}


def test_finds_an_existing_monitor_group_instead_of_making_another():
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("GET", "/monitor-groups/77/monitors"): {"data": [],
                                                              "pagination": {}}})
    BetterStack("tok", "test-cluster", opener=http).list_managed()
    assert not any(m == "POST" for m, _, _ in http.calls)


def test_creates_the_group_when_it_is_missing():
    http = FakeHTTP({
        ("GET", "/monitor-groups"): {"data": [], "pagination": {}},
        ("POST", "/monitor-groups"): {"data": {"id": "88"}},
        ("GET", "/monitor-groups/88/monitors"): {"data": [], "pagination": {}},
    })
    BetterStack("tok", "test-cluster", opener=http).list_managed()
    post = [c for c in http.calls if c[0] == "POST"][0]
    assert post[2] == {"name": "test-cluster"}


def test_pagination_is_followed_to_the_end():
    # a half-read list would look like monitors that need deleting, so this matters
    page2 = "https://uptime.betterstack.com/api/v2/monitor-groups/77/monitors?page=2"
    http = FakeHTTP({
        ("GET", "/monitor-groups"): GROUP_PAGE,
        ("GET", "page=2"): {"data": [{"id": "2", "attributes": {"url": "https://b/"}}],
                            "pagination": {"next": None}},
        ("GET", "/monitor-groups/77/monitors"): {
            "data": [{"id": "1", "attributes": {"url": "https://a/"}}],
            "pagination": {"next": page2}},
    })
    got = BetterStack("tok", "test-cluster", opener=http).list_managed()
    assert sorted(m.id for m in got) == ["1", "2"]


def test_pagination_that_loops_does_not_hang():
    same = "https://uptime.betterstack.com/api/v2/monitor-groups/77/monitors"
    http = FakeHTTP({
        ("GET", "/monitor-groups"): GROUP_PAGE,
        ("GET", "/monitor-groups/77/monitors"): {"data": [], "pagination": {"next": same}},
    })
    assert BetterStack("tok", "test-cluster", opener=http).list_managed() == []


def test_create_sends_the_fields_we_manage():
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("POST", "/monitors"): {"data": {"id": "5"}}})
    mid = BetterStack("tok", "test-cluster", opener=http).create(want())
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    assert mid == "5"
    assert body["monitor_type"] == "expected_status_code"
    assert body["url"] == "https://shop.example.com/"
    assert body["pronounceable_name"] == "shop.example.com"
    assert body["expected_status_codes"] == [200, 204]
    assert body["monitor_group_id"] == 77


def test_update_never_moves_the_url():
    # url is how we match a monitor to a route; changing it would silently retarget an
    # existing monitor and take its history with it
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("PATCH", "/monitors/5"): {}})
    existing = ExistingMonitor(id="5", url="https://shop.example.com/",
                               display_name="old", check_frequency=60,
                               request_timeout=30, expected_status_codes=(200,),
                               regions=("eu",))
    BetterStack("tok", "test-cluster", opener=http).update(existing, want())
    body = [c for c in http.calls if c[0] == "PATCH"][0][2]
    assert "url" not in body
    assert body["pronounceable_name"] == "shop.example.com"


def test_delete_calls_the_right_monitor():
    http = FakeHTTP({("DELETE", "/monitors/5"): {}})
    bs = BetterStack("tok", "test-cluster", opener=http)
    bs.delete(ExistingMonitor(id="5", url="u", display_name="n", check_frequency=1,
                              request_timeout=1, expected_status_codes=(),
                              regions=()))
    assert http.calls[0][0] == "DELETE"


def test_an_http_error_becomes_a_provider_error_with_context():
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, None)

    with pytest.raises(ProviderError) as e:
        BetterStack("tok", "test-cluster", opener=boom).list_managed()
    assert "422" in str(e.value)


def test_the_token_is_sent_as_a_bearer_header():
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("GET", "/monitor-groups/77/monitors"): {"data": [],
                                                              "pagination": {}}})
    BetterStack("s3cret", "test-cluster", opener=http).list_managed()
    # urllib title-cases header names
    assert http.headers["Authorization"] == "Bearer s3cret"

def test_expecting_a_3xx_turns_off_following_redirects():
    # the API refuses the combination, and it is right to: following the redirect
    # means never seeing the status code you asked for
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("POST", "/monitors"): {"data": {"id": "6"}}})
    w = DesiredMonitor(hostname="www.example.com", url="https://www.example.com/",
                       check_frequency=180, request_timeout=30,
                       expected_status_codes=(301,), regions=("eu",),
                       namespace="acme", route="www")
    BetterStack("tok", "test-cluster", opener=http).create(w)
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    assert body["follow_redirects"] is False
    assert body["remember_cookies"] is False


def test_expecting_only_2xx_leaves_redirect_handling_alone():
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("POST", "/monitors"): {"data": {"id": "7"}}})
    BetterStack("tok", "test-cluster", opener=http).create(want())
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    assert "follow_redirects" not in body


def test_the_policy_is_set_when_a_monitor_is_created():
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("POST", "/monitors"): {"data": {"id": "8"}}})
    w = DesiredMonitor(hostname="shop.example.com", url="https://shop.example.com/",
                       check_frequency=180, request_timeout=30,
                       expected_status_codes=(200,), regions=("eu",),
                       namespace="acme", route="shop", policy_id="121827")
    BetterStack("tok", "test-cluster", opener=http).create(w)
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    assert body["policy_id"] == 121827


def test_the_policy_is_never_touched_on_an_update():
    # whoever tunes alerting in Better Stack keeps their change; a reconcile every
    # fifteen minutes must not put it back
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("PATCH", "/monitors/8"): {}})
    w = DesiredMonitor(hostname="shop.example.com", url="https://shop.example.com/",
                       check_frequency=60, request_timeout=30,
                       expected_status_codes=(200,), regions=("eu",),
                       namespace="acme", route="shop", policy_id="121827")
    existing = ExistingMonitor(id="8", url=w.url, display_name="acme/shop",
                               check_frequency=180, request_timeout=30,
                               expected_status_codes=(200,), regions=("eu",))
    BetterStack("tok", "test-cluster", opener=http).update(existing, w)
    body = [c for c in http.calls if c[0] == "PATCH"][0][2]
    assert "policy_id" not in body


def test_no_policy_configured_means_none_is_sent():
    http = FakeHTTP({("GET", "/monitor-groups"): GROUP_PAGE,
                     ("POST", "/monitors"): {"data": {"id": "9"}}})
    BetterStack("tok", "test-cluster", opener=http).create(want())
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    assert "policy_id" not in body
