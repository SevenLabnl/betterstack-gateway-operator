"""Better Stack Uptime.

Ownership is a monitor group named after the cluster. Everything this operator creates
goes in it, and `list_managed` reads it back — so the group is the record of what we
own and the cluster never has to store anything.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..model import DesiredMonitor, ExistingMonitor
from .base import ProviderError

log = logging.getLogger(__name__)

API = "https://uptime.betterstack.com/api/v2"


class BetterStack:
    name = "betterstack"

    def __init__(self, token: str, group_name: str, timeout: int = 30,
                 opener=None) -> None:
        self._token = token
        self._group_name = group_name
        self._timeout = timeout
        # injectable so tests never touch the network
        self._open = opener or urllib.request.urlopen
        self._group_id: str | None = None

    # ------------------------------------------------------------------ transport
    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json",
        })
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ProviderError("%s %s -> %s %s" % (method, url, e.code, detail)) from e
        except urllib.error.URLError as e:
            raise ProviderError("%s %s unreachable: %s" % (method, url, e.reason)) from e

    def _paged(self, path: str) -> list[dict]:
        """Follow pagination to the end.

        Not optional: the default page size is smaller than the number of hostnames a
        busy cluster serves, and a half-read list would look to the reconciler like
        monitors that need deleting.
        """
        out: list[dict] = []
        url: str | None = path
        seen: set[str] = set()
        while url:
            # the first call is a relative path and every next link comes back
            # absolute; compare them in one form or the same page counts as two
            canonical = url if url.startswith("http") else API + url
            if canonical in seen:  # a provider bug should not become a loop
                log.warning("pagination revisited %s, stopping", canonical)
                break
            seen.add(canonical)
            page = self._call("GET", url)
            out.extend(page.get("data") or [])
            url = ((page.get("pagination") or {}).get("next")) or None
        return out

    # ------------------------------------------------------------------ the group
    def _ensure_group(self) -> str:
        if self._group_id:
            return self._group_id
        for g in self._paged("/monitor-groups"):
            if (g.get("attributes") or {}).get("name") == self._group_name:
                self._group_id = str(g["id"])
                log.info("using existing monitor group %r (id %s)",
                         self._group_name, self._group_id)
                return self._group_id
        created = self._call("POST", "/monitor-groups", {"name": self._group_name})
        self._group_id = str((created.get("data") or {})["id"])
        log.info("created monitor group %r (id %s)", self._group_name, self._group_id)
        return self._group_id

    # ------------------------------------------------------------------ mapping
    @staticmethod
    def _to_existing(item: dict) -> ExistingMonitor:
        a = item.get("attributes") or {}
        codes = a.get("expected_status_codes") or []
        return ExistingMonitor(
            id=str(item["id"]),
            url=a.get("url") or "",
            display_name=a.get("pronounceable_name") or "",
            check_frequency=int(a.get("check_frequency") or 0),
            request_timeout=int(a.get("request_timeout") or 0),
            expected_status_codes=tuple(int(c) for c in codes),
            regions=tuple(a.get("regions") or []),
        )

    def _payload(self, want: DesiredMonitor, creating: bool = False) -> dict:
        body = {
            "monitor_type": "expected_status_code",
            "url": want.url,
            "pronounceable_name": want.display_name,
            "check_frequency": want.check_frequency,
            "request_timeout": want.request_timeout,
            "expected_status_codes": list(want.expected_status_codes),
            "regions": list(want.regions),
            "monitor_group_id": int(self._ensure_group()),
        }
        # Better Stack refuses to follow a redirect while expecting a 3xx, and it is
        # right to: you would never observe the status you asked for. A monitor that
        # checks a redirect has to stop at it.
        if any(300 <= c < 400 for c in want.expected_status_codes):
            body["follow_redirects"] = False
            body["remember_cookies"] = False
        # An escalation policy is applied when the monitor is created and never on an
        # update. A new monitor should not start out notifying everyone by default,
        # but who gets woken up after that is a decision for the people carrying the
        # pager — and a reconcile that reset it every fifteen minutes would be worse
        # than useless. team_wait and email/sms/call/push are never set for the same
        # reason.
        if creating and want.policy_id:
            body["policy_id"] = int(want.policy_id)
        return body

    # ------------------------------------------------------------------ interface
    def list_managed(self) -> list[ExistingMonitor]:
        gid = self._ensure_group()
        items = self._paged("/monitor-groups/%s/monitors" % gid)
        return [self._to_existing(i) for i in items]

    def create(self, want: DesiredMonitor) -> str:
        res = self._call("POST", "/monitors", self._payload(want, creating=True))
        return str((res.get("data") or {})["id"])

    def update(self, existing: ExistingMonitor, want: DesiredMonitor) -> None:
        body = self._payload(want)
        # url is the identity we match on; changing it would silently retarget a
        # monitor instead of replacing it, and lose its history along the way
        body.pop("url", None)
        self._call("PATCH", "/monitors/%s" % existing.id, body)

    def delete(self, existing: ExistingMonitor) -> None:
        self._call("DELETE", "/monitors/%s" % existing.id)
