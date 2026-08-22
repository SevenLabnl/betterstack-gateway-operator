"""The rules for which hostnames get a monitor.

These are the decisions people will disagree with, so they are the ones worth pinning
down.
"""
from gateway_uptime.config import Config
from gateway_uptime.selector import monitors_for, monitors_for_all

CFG = Config(
    token="t",
    cluster_name="test-cluster",
    exclude_suffixes=("7dev.nl", "7test.nl"),
    expected_status_codes=(200, 301),
)


def route(name="app", ns="team", hosts=(), annotations=None):
    return {
        "metadata": {"name": name, "namespace": ns,
                     "annotations": annotations or {}},
        "spec": {"hostnames": list(hosts)},
    }


def urls(ms):
    return [m.url for m in ms]


def test_production_hostname_is_monitored():
    ms = monitors_for(route(hosts=["shop.example.com"]), CFG)
    assert urls(ms) == ["https://shop.example.com/"]


def test_excluded_suffix_is_skipped():
    assert monitors_for(route(hosts=["app.7dev.nl"]), CFG) == []


def test_exclusion_matches_the_domain_itself_not_just_subdomains():
    assert monitors_for(route(hosts=["7dev.nl"]), CFG) == []


def test_exclusion_does_not_match_a_lookalike_domain():
    # not7dev.nl must not be caught by the 7dev.nl rule
    ms = monitors_for(route(hosts=["not7dev.nl"]), CFG)
    assert urls(ms) == ["https://not7dev.nl/"]


def test_wildcard_hostnames_are_never_monitored():
    assert monitors_for(route(hosts=["*.example.com"]), CFG) == []


def test_opt_out_wins_over_everything():
    r = route(hosts=["shop.example.com"],
              annotations={"betterstack.sevenlab.nl/enabled": "false"})
    assert monitors_for(r, CFG) == []


def test_opt_in_overrides_an_excluded_suffix():
    r = route(hosts=["demo.7dev.nl"],
              annotations={"betterstack.sevenlab.nl/enabled": "true"})
    assert urls(monitors_for(r, CFG)) == ["https://demo.7dev.nl/"]


def test_path_annotation_is_used_and_normalised():
    r = route(hosts=["api.example.com"],
              annotations={"betterstack.sevenlab.nl/path": "healthz"})
    assert urls(monitors_for(r, CFG)) == ["https://api.example.com/healthz"]


def test_status_codes_and_frequency_can_be_overridden_per_route():
    r = route(hosts=["api.example.com"], annotations={
        "betterstack.sevenlab.nl/expected-status-codes": "200, 418",
        "betterstack.sevenlab.nl/check-frequency": "60",
    })
    m = monitors_for(r, CFG)[0]
    assert m.expected_status_codes == (200, 418)
    assert m.check_frequency == 60


def test_defaults_come_from_config():
    m = monitors_for(route(hosts=["a.example.com"]), CFG)[0]
    assert m.expected_status_codes == (200, 301)
    assert m.check_frequency == CFG.check_frequency
    assert m.regions == CFG.regions


def test_display_name_points_at_the_route_not_the_url():
    # this is what Better Stack reads out loud on a call at 3am
    m = monitors_for(route(name="shop", ns="acme", hosts=["a.example.com"]), CFG)[0]
    assert m.display_name == "acme/shop"


def test_hostnames_are_lowercased():
    m = monitors_for(route(hosts=["Shop.Example.COM"]), CFG)[0]
    assert m.url == "https://shop.example.com/"


def test_one_route_can_produce_several_monitors():
    ms = monitors_for(route(hosts=["a.example.com", "b.example.com"]), CFG)
    assert len(ms) == 2


def test_duplicate_hostnames_across_routes_collapse_to_one_monitor():
    # the http redirect route and the https route both claim the hostname; monitoring
    # it twice would double the noise and the bill
    routes = [route(name="web", hosts=["shop.example.com"]),
              route(name="web-redirect", hosts=["shop.example.com"])]
    assert len(monitors_for_all(routes, CFG)) == 1


def test_same_host_different_paths_are_separate_monitors():
    routes = [route(name="web", hosts=["shop.example.com"]),
              route(name="api", hosts=["shop.example.com"],
                    annotations={"betterstack.sevenlab.nl/path": "/api"})]
    assert len(monitors_for_all(routes, CFG)) == 2


def test_route_without_hostnames_produces_nothing():
    assert monitors_for(route(hosts=[]), CFG) == []


# ------------------------------------------------------------------ redirect pairs
# A hostname is normally served by two routes: the real one on :443 and a companion on
# :80 that only redirects. Both claim the hostname, which is where the next rules come
# from — found by running this against a cluster, not by thinking about it.

def redirect_route(name, ns="team", hosts=(), annotations=None):
    return {
        "metadata": {"name": name, "namespace": ns,
                     "annotations": annotations or {}},
        "spec": {"hostnames": list(hosts),
                 "rules": [{"filters": [{"type": "RequestRedirect"}]}]},
    }


def backend_route(name, ns="team", hosts=(), annotations=None):
    return {
        "metadata": {"name": name, "namespace": ns,
                     "annotations": annotations or {}},
        "spec": {"hostnames": list(hosts),
                 "rules": [{"backendRefs": [{"name": "svc", "port": 80}]}]},
    }


def test_the_serving_route_names_the_monitor_not_the_redirect():
    # whichever sorts first must not decide: the name should say what is being served
    ms = monitors_for_all([redirect_route("web-redirect", hosts=["shop.example.com"]),
                           backend_route("web", hosts=["shop.example.com"])], CFG)
    assert len(ms) == 1
    assert ms[0].display_name == "team/web"


def test_order_of_the_pair_does_not_matter():
    ms = monitors_for_all([backend_route("web", hosts=["shop.example.com"]),
                           redirect_route("web-redirect",
                                          hosts=["shop.example.com"])], CFG)
    assert ms[0].display_name == "team/web"


def test_opting_out_one_route_drops_the_hostname_entirely():
    # annotating only the serving route used to leave the monitor in place under the
    # redirect route's name, which looks exactly like the annotation being ignored
    ms = monitors_for_all([
        backend_route("web", hosts=["shop.example.com"],
                      annotations={"betterstack.sevenlab.nl/enabled": "false"}),
        redirect_route("web-redirect", hosts=["shop.example.com"]),
    ], CFG)
    assert ms == []


def test_opting_out_the_redirect_also_drops_the_hostname():
    ms = monitors_for_all([
        backend_route("web", hosts=["shop.example.com"]),
        redirect_route("web-redirect", hosts=["shop.example.com"],
                       annotations={"betterstack.sevenlab.nl/enabled": "false"}),
    ], CFG)
    assert ms == []


def test_opting_out_one_hostname_leaves_the_others_alone():
    ms = monitors_for_all([
        backend_route("a", hosts=["a.example.com"],
                      annotations={"betterstack.sevenlab.nl/enabled": "false"}),
        backend_route("b", hosts=["b.example.com"]),
    ], CFG)
    assert urls(ms) == ["https://b.example.com/"]


def test_recognising_a_redirect_only_route():
    from gateway_uptime.selector import is_redirect_only
    assert not is_redirect_only(backend_route("web", hosts=["a.example.com"]))
    assert is_redirect_only(redirect_route("web-r", hosts=["a.example.com"]))
    # no rules at all is not a redirect either
    assert not is_redirect_only(route(hosts=["a.example.com"]))
