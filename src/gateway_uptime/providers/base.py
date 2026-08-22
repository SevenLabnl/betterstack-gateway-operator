"""What a monitoring provider has to be able to do.

Deliberately small. Everything about which hostnames matter, and what a monitor should
look like, is decided before we get here — a provider only reflects a set of
DesiredMonitors into its own world.
"""
from __future__ import annotations

from typing import Protocol

from ..model import DesiredMonitor, ExistingMonitor


class Provider(Protocol):
    name: str

    def list_managed(self) -> list[ExistingMonitor]:
        """Every monitor this operator owns for this cluster.

        Ownership must be recorded on the provider's side rather than in the cluster,
        so that a route which disappears without warning still leaves a trail we can
        clean up.
        """

    def create(self, want: DesiredMonitor) -> str:
        """Create it and return the provider's id."""

    def update(self, existing: ExistingMonitor, want: DesiredMonitor) -> None:
        ...

    def delete(self, existing: ExistingMonitor) -> None:
        ...


class ProviderError(RuntimeError):
    """The provider could not be reached or refused a call.

    Raised rather than swallowed: a failed reconcile should be retried by the caller,
    not quietly treated as success.
    """
