"""Turning HTTPRoutes into the monitors they deserve.

Kept separate from everything that talks to a network so the rules — which are the part
people will argue about — can be tested directly.
"""
from __future__ import annotations

from .config import ANNOTATION_PREFIX, Config
from .model import DesiredMonitor


def _ann(route: dict, name: str) -> str | None:
    anns = (route.get("metadata") or {}).get("annotations") or {}
    return anns.get("%s/%s" % (ANNOTATION_PREFIX, name))


def _ints(raw: str) -> tuple[int, ...]:
    return tuple(int(p.strip()) for p in raw.split(",") if p.strip())


def _hostnames(route: dict) -> list[str]:
    out = []
    for h in (route.get("spec") or {}).get("hostnames") or []:
        h = str(h).strip().lower()
        if h and not h.startswith("*"):
            out.append(h)
    return out


def is_redirect_only(route: dict) -> bool:
    """True when the route does nothing but redirect.

    The usual :80 companion of an HTTPS route: no backend, just a RequestRedirect. It
    claims the same hostname as the real route, so when both produce the same monitor
    we would rather name it after the one that actually serves something.
    """
    rules = (route.get("spec") or {}).get("rules") or []
    if not rules:
        return False
    for r in rules:
        if r.get("backendRefs"):
            return False
        if not any(f.get("type") == "RequestRedirect" for f in r.get("filters") or []):
            return False
    return True


def monitors_for(route: dict, cfg: Config) -> list[DesiredMonitor]:
    """Every monitor this route asks for. Empty when it asks for none.

    Note that a route asking for a monitor does not settle it: another route may claim
    the same hostname and opt out. See monitors_for_all.
    """
    meta = route.get("metadata") or {}
    ns, name = meta.get("namespace", ""), meta.get("name", "")

    # absent means "let the rules decide"; only an explicit value overrides them
    opt = (_ann(route, "enabled") or "").strip().lower()
    if opt in ("false", "no", "off"):
        return []
    forced = opt in ("true", "yes", "on")

    path = (_ann(route, "path") or "/").strip()
    if not path.startswith("/"):
        path = "/" + path

    codes = cfg.expected_status_codes
    raw_codes = _ann(route, "expected-status-codes")
    if raw_codes:
        codes = _ints(raw_codes)

    frequency = cfg.check_frequency
    raw_freq = _ann(route, "check-frequency")
    if raw_freq:
        frequency = int(raw_freq)

    out: list[DesiredMonitor] = []
    for hostname in _hostnames(route):
        if not forced and cfg.excluded(hostname):
            continue
        out.append(DesiredMonitor(
            hostname=hostname,
            url="https://%s%s" % (hostname, path),
            check_frequency=frequency,
            request_timeout=cfg.request_timeout,
            expected_status_codes=codes,
            regions=cfg.regions,
            namespace=ns,
            route=name,
            policy_id=cfg.policy_id,
        ))
    return out


def opted_out_hostnames(routes: list[dict]) -> set[str]:
    """Hostnames that any route has explicitly excluded.

    Opting out has to work per hostname rather than per route. A hostname is normally
    served by two routes — the real one and its :80 redirect — and annotating only one
    of them would leave the monitor in place under the other's name, which looks like
    the annotation was ignored. Saying "do not monitor this" once is enough.
    """
    out: set[str] = set()
    for r in routes:
        if (_ann(r, "enabled") or "").strip().lower() in ("false", "no", "off"):
            out.update(_hostnames(r))
    return out


def monitors_for_all(routes: list[dict], cfg: Config) -> list[DesiredMonitor]:
    """The complete desired set, deduplicated.

    Two routes can legitimately claim the same hostname, so the same URL can be asked
    for twice. The one that serves a backend wins over one that only redirects, so the
    monitor is named after the thing being monitored rather than after whichever route
    happened to sort first.
    """
    excluded = opted_out_hostnames(routes)
    chosen: dict[str, tuple[bool, DesiredMonitor]] = {}

    for r in routes:
        redirect_only = is_redirect_only(r)
        for m in monitors_for(r, cfg):
            if m.hostname in excluded:
                continue
            current = chosen.get(m.key)
            if current is None or (current[0] and not redirect_only):
                chosen[m.key] = (redirect_only, m)

    return sorted((m for _, m in chosen.values()), key=lambda m: m.url)
