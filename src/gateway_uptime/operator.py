"""The kopf entry point.

Thin on purpose: everything funnels into the same full reconcile, so there is one code
path to reason about and to test. Events make it prompt, the resync makes it correct.
"""
from __future__ import annotations

import asyncio
import logging

import kopf

from .config import Config, from_env
from .providers.betterstack import BetterStack
from .reconcile import run

log = logging.getLogger(__name__)

GROUP = "gateway.networking.k8s.io"
VERSION = "v1"
PLURAL = "httproutes"

# How long to wait for a burst of events to settle before reconciling. One deploy
# touches several routes at once — a redirect and its target, or every app in a sync —
# and each of those would otherwise mean a full listing and a round of provider calls
# with the same outcome.
DEBOUNCE_SECONDS = 3

_cfg: Config | None = None
_provider: BetterStack | None = None
# Reconciles are serialised. Two racing would each see a half-applied world and could
# both decide the same monitor is missing.
_lock = asyncio.Lock()
# Set by any event; the worker below collapses however many arrive into one pass.
_dirty = asyncio.Event()


def setup() -> tuple[Config, BetterStack]:
    global _cfg, _provider
    if _cfg is None or _provider is None:
        _cfg = from_env()
        _provider = BetterStack(token=_cfg.token,
                                group_name=_cfg.monitor_group_name,
                                timeout=30)
    return _cfg, _provider


async def list_routes() -> list[dict]:
    """Every HTTPRoute in the cluster.

    kopf hands a handler only the object that changed; the reconciler wants the whole
    set, so this reads it directly.
    """
    import kubernetes_asyncio.client as k8s
    import kubernetes_asyncio.config as k8sconfig

    try:
        k8sconfig.load_incluster_config()
    except Exception:
        await k8sconfig.load_kube_config()

    async with k8s.ApiClient() as api:
        custom = k8s.CustomObjectsApi(api)
        res = await custom.list_cluster_custom_object(GROUP, VERSION, PLURAL)
        return res.get("items", [])


async def reconcile(reason: str, logger) -> None:
    cfg, provider = setup()
    async with _lock:
        routes = await list_routes()
        # provider calls are blocking, so keep them off the event loop
        result = await asyncio.to_thread(run, routes, provider, cfg)
    # logged even when nothing changed: reconciles are rare by design, and a line
    # saying so is how you tell a working operator from a stuck one
    if any(result.values()):
        logger.info("%s: %s", reason, result)
    else:
        logger.info("%s: already in sync (%d route(s))", reason, len(routes))


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, logger, **_):
    cfg, _p = setup()
    # We own no CRDs and no objects, so peering would only add a resource to grant
    # ourselves rights on.
    settings.peering.standalone = True
    settings.posting.enabled = False

    logger.info("cluster %r, excluding %s, resync every %ds%s",
                cfg.cluster_name,
                ", ".join(cfg.exclude_suffixes) or "nothing",
                cfg.resync_seconds,
                "  [DRY RUN]" if cfg.dry_run else "")

    async def on_change():
        """Collapses a burst of route events into one reconcile."""
        while True:
            await _dirty.wait()
            _dirty.clear()
            await asyncio.sleep(DEBOUNCE_SECONDS)
            # anything that arrived during the wait is covered by this pass; if more
            # arrive while it runs, the flag is set again and we come straight back
            _dirty.clear()
            try:
                await reconcile("routes changed", logger)
            except Exception:
                logger.exception("reconcile after a route change failed; "
                                 "the resync will pick it up")

    async def resync():
        """What makes this self-healing.

        A watch can miss things: a restart at the wrong moment, an event dropped under
        load, a monitor deleted by hand in the Better Stack UI. Recomputing everything
        on a timer means none of those needs a recovery path of its own.
        """
        while True:
            await asyncio.sleep(cfg.resync_seconds)
            try:
                await reconcile("periodic resync", logger)
            except Exception:
                logger.exception("resync failed; retrying in %ds", cfg.resync_seconds)

    # once at startup, so a restart converges immediately rather than after an interval
    try:
        await reconcile("startup", logger)
    except Exception:
        logger.exception("initial reconcile failed; the resync loop will retry")

    asyncio.create_task(on_change())
    asyncio.create_task(resync())


@kopf.on.event(GROUP, VERSION, PLURAL)
async def on_route_event(event, meta, logger, **_):
    """Any change to any route, including deletion.

    on.event rather than on.create/update/delete: a deletion event carries no useful
    spec, and we do not need one — the reconciler recomputes the desired set from
    whatever is left. This only rings the bell; the worker decides when to answer.
    """
    # `type` is present but None for the synthetic events kopf emits when it first
    # lists what already exists, so a default argument never fires
    kind = (event.get("type") or "sync").lower()
    logger.debug("route %s/%s %s", meta.get("namespace"), meta.get("name"), kind)
    _dirty.set()


@kopf.on.probe(id="ready")
async def ready(**_):
    return "ok"
