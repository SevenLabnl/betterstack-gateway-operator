"""The plan and its execution.

The provider is faked, so these cover the decisions — what gets created, changed and
removed — without a network or a cluster.
"""
import pytest

from gateway_uptime.config import Config
from gateway_uptime.model import DesiredMonitor, ExistingMonitor, plan
from gateway_uptime.reconcile import run

CFG = Config(token="t", cluster_name="c", exclude_suffixes=("7dev.nl",))


def want(url, name="team/app", freq=180, codes=(200,), regions=("eu",)):
    return DesiredMonitor(hostname=url.split("/")[2], url=url, check_frequency=freq,
                          request_timeout=30, expected_status_codes=codes,
                          regions=regions, namespace=name.split("/")[0],
                          route=name.split("/")[1])


def have(mid, url, name="team/app", freq=180, codes=(200,), regions=("eu",)):
    return ExistingMonitor(id=mid, url=url, display_name=name, check_frequency=freq,
                           request_timeout=30, expected_status_codes=codes,
                           regions=regions)


class FakeProvider:
    name = "fake"

    def __init__(self, existing=None, fail_on=()):
        self.existing = list(existing or [])
        self.fail_on = set(fail_on)
        self.created, self.updated, self.deleted = [], [], []

    def list_managed(self):
        return list(self.existing)

    def create(self, w):
        if "create" in self.fail_on:
            raise RuntimeError("provider says no")
        self.created.append(w.url)
        return "new-%d" % len(self.created)

    def update(self, e, w):
        if "update" in self.fail_on:
            raise RuntimeError("provider says no")
        self.updated.append(e.id)

    def delete(self, e):
        if "delete" in self.fail_on:
            raise RuntimeError("provider says no")
        self.deleted.append(e.id)


# ------------------------------------------------------------------ planning
def test_missing_monitor_is_planned_for_creation():
    p = plan([want("https://a.example.com/")], [])
    assert [m.url for m in p.create] == ["https://a.example.com/"]
    assert not p.update and not p.delete


def test_identical_monitor_is_left_alone():
    w = want("https://a.example.com/")
    p = plan([w], [have("1", w.url)])
    assert p.empty and p.unchanged == 1


def test_changed_field_is_planned_for_update_and_names_what_changed():
    w = want("https://a.example.com/", freq=60)
    p = plan([w], [have("1", w.url, freq=180)])
    assert len(p.update) == 1
    _, _, diff = p.update[0]
    assert diff == ["check_frequency"]


def test_renamed_route_updates_the_monitor_name():
    w = want("https://a.example.com/", name="acme/shop")
    p = plan([w], [have("1", w.url, name="team/app")])
    assert p.update[0][2] == ["name"]


def test_status_code_order_is_not_a_difference():
    w = want("https://a.example.com/", codes=(200, 301))
    p = plan([w], [have("1", w.url, codes=(301, 200))])
    assert p.empty


def test_monitor_without_a_route_is_planned_for_deletion():
    p = plan([], [have("1", "https://gone.example.com/")])
    assert [m.id for m in p.delete] == ["1"]


def test_a_full_picture_at_once():
    desired = [want("https://keep.example.com/"),
               want("https://new.example.com/"),
               want("https://change.example.com/", freq=60)]
    existing = [have("1", "https://keep.example.com/"),
                have("2", "https://change.example.com/", freq=180),
                have("3", "https://gone.example.com/")]
    p = plan(desired, existing)
    assert [m.url for m in p.create] == ["https://new.example.com/"]
    assert [e.id for e, _, _ in p.update] == ["2"]
    assert [e.id for e in p.delete] == ["3"]
    assert p.unchanged == 1


# ------------------------------------------------------------------ executing
def route(ns, name, hosts):
    return {"metadata": {"name": name, "namespace": ns, "annotations": {}},
            "spec": {"hostnames": list(hosts)}}


def test_run_creates_what_the_cluster_serves():
    prov = FakeProvider()
    res = run([route("acme", "shop", ["shop.example.com"])], prov, CFG)
    assert prov.created == ["https://shop.example.com/"]
    assert res["created"] == 1


def test_run_deletes_a_monitor_whose_route_is_gone():
    prov = FakeProvider(existing=[have("9", "https://old.example.com/")])
    res = run([], prov, CFG)
    assert prov.deleted == ["9"]
    assert res["deleted"] == 1


def test_run_ignores_excluded_hostnames():
    prov = FakeProvider()
    run([route("acme", "dev", ["app.7dev.nl"])], prov, CFG)
    assert prov.created == []


def test_dry_run_changes_nothing_but_says_it_would_have():
    # the counts are all zero because nothing was done; `planned` is what stops a
    # caller concluding there was nothing to do, which is the one thing a dry run
    # must never claim
    cfg = Config(token="t", cluster_name="c", dry_run=True)
    prov = FakeProvider(existing=[have("9", "https://old.example.com/")])
    res = run([route("acme", "shop", ["shop.example.com"])], prov, cfg)
    assert prov.created == [] and prov.deleted == []
    assert res["created"] == 0 and res["deleted"] == 0
    assert res["planned"] == 2  # one create, one delete


def test_nothing_to_do_plans_nothing():
    w = want("https://shop.example.com/", name="acme/shop",
             codes=CFG.expected_status_codes)
    prov = FakeProvider(existing=[have("1", w.url, name="acme/shop",
                                       codes=CFG.expected_status_codes)])
    res = run([route("acme", "shop", ["shop.example.com"])], prov, CFG)
    assert res["planned"] == 0


def test_one_failing_call_does_not_abandon_the_rest():
    # a provider hiccup on a delete must not stop the creates from happening
    prov = FakeProvider(existing=[have("9", "https://old.example.com/")],
                        fail_on=("delete",))
    res = run([route("acme", "shop", ["shop.example.com"])], prov, CFG)
    assert prov.created == ["https://shop.example.com/"]
    assert res["created"] == 1 and res["failed"] == 1


def test_nothing_to_do_calls_nothing():
    # the existing monitor has to match what CFG would produce, defaults and all,
    # otherwise this quietly becomes a test that an update happens
    w = want("https://shop.example.com/", name="acme/shop",
             codes=CFG.expected_status_codes)
    prov = FakeProvider(existing=[have("1", w.url, name="acme/shop",
                                       codes=CFG.expected_status_codes)])
    res = run([route("acme", "shop", ["shop.example.com"])], prov, CFG)
    assert prov.created == [] and prov.updated == [] and prov.deleted == []
    assert res["created"] == 0
