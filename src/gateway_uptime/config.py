"""Settings, read once from the environment.

Everything is overridable per route by annotation; these are the defaults that apply
when a route says nothing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

ANNOTATION_PREFIX = "betterstack.sevenlab.nl"

# Alleen 2xx. Een 3xx erbij zou betekenen dat de redirect niet gevolgd mag
# worden (de API weigert de combinatie), en dan controleer je het doorsturen in
# plaats van of de site er is. Wie dat wel wil zet het per route.
DEFAULT_STATUS_CODES = "200,201,202,204"


def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def _ints(raw: str) -> list[int]:
    return [int(p) for p in _csv(raw)]


class ConfigError(ValueError):
    """Raised for a configuration that cannot work, so it fails at start rather than
    silently monitoring nothing."""


@dataclass(frozen=True)
class Config:
    token: str
    cluster_name: str
    exclude_suffixes: tuple[str, ...] = ()
    check_frequency: int = 180
    request_timeout: int = 30
    expected_status_codes: tuple[int, ...] = field(
        default_factory=lambda: tuple(_ints(DEFAULT_STATUS_CODES)))
    regions: tuple[str, ...] = ("eu",)
    resync_seconds: int = 900
    dry_run: bool = False

    @property
    def monitor_group_name(self) -> str:
        return self.cluster_name

    def excluded(self, hostname: str) -> bool:
        return any(hostname == s or hostname.endswith("." + s.lstrip("."))
                   for s in self.exclude_suffixes)


VALID_REGIONS = {"us", "eu", "as", "au"}

# Better Stack only accepts these for HTTP monitors. Anything else is rejected at
# create time, which is a confusing place to learn about a typo in a ConfigMap.
VALID_TIMEOUTS = (2, 3, 5, 10, 15, 30, 45, 60)


def from_env(env: dict[str, str] | None = None) -> Config:
    e = os.environ if env is None else env

    token = e.get("BETTERSTACK_TOKEN", "").strip()
    if not token:
        raise ConfigError("BETTERSTACK_TOKEN is required")

    cluster = e.get("CLUSTER_NAME", "").strip()
    if not cluster:
        # Without this every cluster would share one monitor group and they would
        # delete each other's monitors on the first resync. Refuse to start.
        raise ConfigError("CLUSTER_NAME is required; it names the monitor group that "
                          "marks which monitors belong to this cluster")

    regions = tuple(_csv(e.get("REGIONS", "eu")))
    unknown = set(regions) - VALID_REGIONS
    if unknown:
        raise ConfigError("unknown region(s) %s; allowed: %s"
                          % (sorted(unknown), sorted(VALID_REGIONS)))

    frequency = int(e.get("CHECK_FREQUENCY", "180"))
    timeout = int(e.get("REQUEST_TIMEOUT", "30"))
    if timeout not in VALID_TIMEOUTS:
        raise ConfigError("REQUEST_TIMEOUT must be one of %s, got %d"
                          % (list(VALID_TIMEOUTS), timeout))
    if frequency < timeout:
        # Better Stack rejects this, but it is a confusing error to receive from an API
        # call two layers down.
        raise ConfigError("CHECK_FREQUENCY (%d) must be at least REQUEST_TIMEOUT (%d)"
                          % (frequency, timeout))

    return Config(
        token=token,
        cluster_name=cluster,
        exclude_suffixes=tuple(_csv(e.get("EXCLUDE_SUFFIXES", ""))),
        check_frequency=frequency,
        request_timeout=timeout,
        expected_status_codes=tuple(_ints(
            e.get("EXPECTED_STATUS_CODES", DEFAULT_STATUS_CODES))),
        regions=regions,
        resync_seconds=int(e.get("RESYNC_SECONDS", "900")),
        dry_run=e.get("DRY_RUN", "false").lower() in ("1", "true", "yes"),
    )
