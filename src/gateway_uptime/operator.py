"""The kopf entry point.

Thin on purpose: every handler funnels into the same full reconcile, so there is one
code path to reason about and to test. The handlers exist to make it prompt, not to
make it correct — correctness comes from the resync loop.
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

_cfg: Config | None = None
_provider: BetterStack | None = None
# Reconciles are serialised. Two racing would each see a half-applied world, and could
# both decide the same monitor is missing.
_lock = asyncio.Lock()


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
        # the provider calls are blocking, so keep them off the event loop
        result = await asyncio.to_thread(run, routes, provider, cfg)
    if any(result.values()):
        logger.info("%s: %s", reason, result)


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

    async def resync_loop():
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

    # once at startup, so a restart converges immediately instead of after an interval
    try:
        await reconcile("startup", logger)
    except Exception:
        logger.exception("initial reconcile failed; the resync loop will retry")

    asyncio.create_task(resync_loop())


@kopf.on.event(GROUP, VERSION, PLURAL)
async def on_route_event(event, meta, logger, **_):
    """Any change to any route, including deletion.

    on.event rather than on.create/update/delete: a deletion event carries no useful
    spec, and we do not need one — the reconciler recomputes the desired set from
    whatever is left.
    """
    # `type` is present but None for the synthetic events kopf emits when it first
    # lists what already exists, so a default argument never fires
    kind = (event.get("type") or "SYNC").lower()
    await reconcile("route %s/%s %s" % (meta.get("namespace"), meta.get("name"), kind),
                    logger)


@kopf.on.probe(id="ready")
async def ready(**_):
    return "ok"
