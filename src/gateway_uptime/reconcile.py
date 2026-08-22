"""Making the provider agree with the cluster.

One function, called both by the watch handlers and by the periodic resync. It always
works on the complete picture rather than on the one route that changed — that costs a
list call and buys convergence: a missed event, a restart mid-delete, or a monitor
someone removed by hand all get corrected on the next pass without special cases.
"""
from __future__ import annotations

import logging

from .config import Config
from .model import Plan, plan
from .providers.base import Provider
from .selector import monitors_for_all

log = logging.getLogger(__name__)


def compute(routes: list[dict], provider: Provider, cfg: Config) -> Plan:
    desired = monitors_for_all(routes, cfg)
    existing = provider.list_managed()
    return plan(desired, existing)


def apply(p: Plan, provider: Provider, cfg: Config) -> dict[str, int]:
    """Execute a plan. Returns what actually happened, which is not always what was
    planned — a single failing call should not abandon the rest of the work."""
    done = {"created": 0, "updated": 0, "deleted": 0, "failed": 0}

    if cfg.dry_run:
        for m in p.create:
            log.info("[dry-run] create %s (%s)", m.url, m.display_name)
        for have, want, diff in p.update:
            log.info("[dry-run] update %s: %s", have.url, ", ".join(diff))
        for have in p.delete:
            log.info("[dry-run] delete %s", have.url)
        return done

    for m in p.create:
        try:
            mid = provider.create(m)
            log.info("created monitor %s for %s (%s)", mid, m.url, m.display_name)
            done["created"] += 1
        except Exception:
            log.exception("could not create monitor for %s", m.url)
            done["failed"] += 1

    for have, want, diff in p.update:
        try:
            provider.update(have, want)
            log.info("updated monitor %s for %s: %s", have.id, have.url,
                     ", ".join(diff))
            done["updated"] += 1
        except Exception:
            log.exception("could not update monitor %s (%s)", have.id, have.url)
            done["failed"] += 1

    for have in p.delete:
        try:
            provider.delete(have)
            log.info("deleted monitor %s for %s — no route serves it any more",
                     have.id, have.url)
            done["deleted"] += 1
        except Exception:
            log.exception("could not delete monitor %s (%s)", have.id, have.url)
            done["failed"] += 1

    return done


def run(routes: list[dict], provider: Provider, cfg: Config) -> dict[str, int]:
    p = compute(routes, provider, cfg)
    if p.empty:
        log.debug("in sync: %s", p.summary())
        return {"created": 0, "updated": 0, "deleted": 0, "failed": 0}
    log.info("reconciling: %s", p.summary())
    return apply(p, provider, cfg)
