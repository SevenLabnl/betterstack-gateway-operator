"""What we want to exist, expressed independently of any provider.

A DesiredMonitor is derived purely from an HTTPRoute plus configuration. The provider's
job is to make its own world match a set of these; it never reasons about Kubernetes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesiredMonitor:
    """One hostname we expect to be up."""

    hostname: str
    url: str
    check_frequency: int
    request_timeout: int
    expected_status_codes: tuple[int, ...]
    regions: tuple[str, ...]
    # where it came from, for the monitor's name and for log lines
    namespace: str
    route: str
    # Applied when the monitor is first created and never afterwards, so that whoever
    # tunes alerting in Better Stack keeps their change.
    policy_id: str | None = None

    @property
    def key(self) -> str:
        """Identity of a monitor. The URL, not the hostname: two routes may serve the
        same host on different paths and both deserve a check."""
        return self.url

    @property
    def display_name(self) -> str:
        """Shown in Better Stack and read out loud when it calls someone at night, so
        it says where to look rather than repeating the URL that is already there."""
        return "%s/%s" % (self.namespace, self.route)


@dataclass(frozen=True)
class ExistingMonitor:
    """A monitor the provider already has, reduced to the fields we manage."""

    id: str
    url: str
    display_name: str
    check_frequency: int
    request_timeout: int
    expected_status_codes: tuple[int, ...]
    regions: tuple[str, ...]

    def differs_from(self, want: DesiredMonitor) -> list[str]:
        """Which managed fields disagree. Returns names so a log line can say what is
        being changed instead of just that something is."""
        out = []
        if self.display_name != want.display_name:
            out.append("name")
        if self.check_frequency != want.check_frequency:
            out.append("check_frequency")
        if self.request_timeout != want.request_timeout:
            out.append("request_timeout")
        if sorted(self.expected_status_codes) != sorted(want.expected_status_codes):
            out.append("expected_status_codes")
        if sorted(self.regions) != sorted(want.regions):
            out.append("regions")
        return out


@dataclass
class Plan:
    """The difference between what should exist and what does. Computed before anything
    is called, so a dry run can print exactly what a real run would do."""

    create: list[DesiredMonitor]
    update: list[tuple[ExistingMonitor, DesiredMonitor, list[str]]]
    delete: list[ExistingMonitor]
    unchanged: int = 0

    @property
    def empty(self) -> bool:
        return not (self.create or self.update or self.delete)

    def summary(self) -> str:
        return ("%d te maken, %d te wijzigen, %d te verwijderen, %d ongewijzigd"
                % (len(self.create), len(self.update), len(self.delete),
                   self.unchanged))


def plan(desired: list[DesiredMonitor], existing: list[ExistingMonitor]) -> Plan:
    """Pure: no I/O, so the interesting logic is testable without a cluster or an API."""
    by_url = {m.url: m for m in existing}
    seen: set[str] = set()
    p = Plan(create=[], update=[], delete=[])

    for want in desired:
        have = by_url.get(want.key)
        if have is None:
            p.create.append(want)
            continue
        seen.add(have.id)
        diff = have.differs_from(want)
        if diff:
            p.update.append((have, want, diff))
        else:
            p.unchanged += 1

    for have in existing:
        if have.id not in seen:
            p.delete.append(have)

    return p
