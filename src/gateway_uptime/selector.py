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


def monitors_for(route: dict, cfg: Config) -> list[DesiredMonitor]:
    """Every monitor this route should have. Empty when it should have none."""
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
    for hostname in (route.get("spec") or {}).get("hostnames") or []:
        hostname = str(hostname).strip().lower()
        if not hostname:
            continue
        # A wildcard listener has no address to request. Monitoring "*.example.com"
        # would mean inventing a hostname, and inventing one that happens to 404 is
        # worse than not checking.
        if hostname.startswith("*"):
            continue
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
        ))
    return out


def monitors_for_all(routes: list[dict], cfg: Config) -> list[DesiredMonitor]:
    """The complete desired set, deduplicated.

    Two routes can legitimately claim the same hostname — a redirect route on :80 and
    the real one on :443 usually do. They would produce the same URL and therefore the
    same monitor, so the first one wins and the second is dropped rather than fighting
    over it on every resync.
    """
    seen: dict[str, DesiredMonitor] = {}
    for r in routes:
        for m in monitors_for(r, cfg):
            seen.setdefault(m.key, m)
    return sorted(seen.values(), key=lambda m: m.url)
