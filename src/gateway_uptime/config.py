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


def _clock(raw: str) -> str:
    """Normalises 01:00 to 01:00:00, which is what the API wants."""
    raw = raw.strip()
    if not raw:
        return ""
    parts = raw.split(":")
    if len(parts) == 2:
        parts.append("00")
    if len(parts) != 3 or not all(p.isdigit() and len(p) == 2 for p in parts):
        raise ConfigError("maintenance times look like 01:00 or 01:00:00, got %r" % raw)
    h, m, sec = (int(p) for p in parts)
    if h > 23 or m > 59 or sec > 59:
        raise ConfigError("not a time of day: %r" % raw)
    return ":".join(parts)


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
    # A window in which Better Stack does not check at all. Empty means the operator
    # does not manage the fields, leaving whatever is already on the monitor alone.
    maintenance_from: str = ""
    maintenance_to: str = ""
    maintenance_timezone: str = "UTC"
    maintenance_days: tuple[str, ...] = ()
    # Optioneel. Alleen gebruikt bij het aanmaken van een monitor — zie de provider.
    policy_id: str | None = None
    # Days before certificate expiry to warn on. None leaves the field unmanaged.
    ssl_expiration: int | None = None

    @property
    def monitor_group_name(self) -> str:
        return self.cluster_name

    @property
    def manages_ssl_expiration(self) -> bool:
        return self.ssl_expiration is not None

    @property
    def manages_maintenance(self) -> bool:
        return bool(self.maintenance_from and self.maintenance_to)

    def excluded(self, hostname: str) -> bool:
        return any(hostname == s or hostname.endswith("." + s.lstrip("."))
                   for s in self.exclude_suffixes)


VALID_REGIONS = {"us", "eu", "as", "au"}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Better Stack only accepts these for HTTP monitors. Anything else is rejected at
# create time, which is a confusing place to learn about a typo in a ConfigMap.
VALID_TIMEOUTS = (2, 3, 5, 10, 15, 30, 45, 60)

# Days before expiry that Better Stack will warn on. Its own list; anything else is
# refused at create time.
VALID_SSL_EXPIRATION = (1, 2, 3, 7, 14, 30, 60)


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

    raw_ssl = e.get("SSL_EXPIRATION", "").strip()
    ssl_expiration: int | None = None
    if raw_ssl:
        try:
            ssl_expiration = int(raw_ssl)
        except ValueError:
            raise ConfigError("SSL_EXPIRATION must be a number of days, got %r"
                              % raw_ssl) from None
        if ssl_expiration not in VALID_SSL_EXPIRATION:
            raise ConfigError("SSL_EXPIRATION must be one of %s days, got %d"
                              % (list(VALID_SSL_EXPIRATION), ssl_expiration))

    mf = _clock(e.get("MAINTENANCE_FROM", ""))
    mt = _clock(e.get("MAINTENANCE_TO", ""))
    if bool(mf) != bool(mt):
        raise ConfigError("MAINTENANCE_FROM and MAINTENANCE_TO must be set together; "
                          "a window with only one end is not a window")
    days = tuple(d.lower() for d in _csv(e.get("MAINTENANCE_DAYS", "")))
    bad = [d for d in days if d not in WEEKDAYS]
    if bad:
        raise ConfigError("unknown day(s) %s in MAINTENANCE_DAYS; allowed: %s"
                          % (bad, list(WEEKDAYS)))
    if days and not mf:
        raise ConfigError("MAINTENANCE_DAYS without MAINTENANCE_FROM does nothing")

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
        policy_id=(e.get("BETTERSTACK_POLICY_ID", "").strip() or None),
        ssl_expiration=ssl_expiration,
        dry_run=e.get("DRY_RUN", "false").lower() in ("1", "true", "yes"),
        maintenance_from=mf,
        maintenance_to=mt,
        maintenance_timezone=e.get("MAINTENANCE_TIMEZONE", "UTC").strip() or "UTC",
        # no days means every day, which is what a bare "01:00 to 03:00" reads as
        maintenance_days=days or (WEEKDAYS if mf else ()),
    )
