"""Orphaned-Jira-key resolver.

A "Jira key" becomes orphaned when the Jira issue it points to no
longer resolves (typically a 404 from /rest/api/3/issue/{key}) but
its Notion / Confluence counterparts still exist. The resolver:

  * persists a tombstone in ``.sync_state.json`` under
    ``orphaned_jira_keys`` so phases can skip the key without making
    a destructive write or logging a fresh ERROR every cycle;
  * removes the tombstone the moment Jira returns 200 again
    (e.g. the issue was restored, or a permission flap recovered);
  * never marks a tombstone on transient failures (5xx, network
    errors, 403) — for those, the orphan status is unknown and the
    previous state is preserved.

The bucket lives alongside other top-level state buckets
(``subtask_todos``, ``template_backfilled``, …). Read-modify-write
on every change keeps the file safe for concurrent updates from
other phases that load and re-save the same file.

Tombstone shape::

    "orphaned_jira_keys": {
        "VC-114": {
            "first_seen": "2026-05-07T12:00:00+00:00",
            "last_seen":  "2026-05-07T12:34:56+00:00",
            "source":     "jira_404"
        },
        ...
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("taskautomation.orphan_keys")

_BUCKET = "orphaned_jira_keys"


def _now_iso() -> str:
    """Timezone-aware UTC ISO-8601 string, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(
            "orphan resolver: state file %s unreadable (%s) — "
            "starting with empty bucket",
            path, e,
        )
        return {}


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("orphan resolver: could not save state to %s: %s", path, e)


class OrphanResolver:
    """Read/write tombstones for Jira keys that point to deleted issues.

    All operations are read-modify-write against the JSON state file
    so the bucket coexists with other top-level state buckets without
    clobbering them.
    """

    def __init__(self, state_path: Path):
        self._path = Path(state_path)

    # ---- low-level state I/O ----

    def _read_bucket(self) -> Dict[str, Any]:
        return dict(_load_state(self._path).get(_BUCKET, {}))

    def _write_bucket(self, bucket: Dict[str, Any]) -> None:
        state = _load_state(self._path)
        if bucket:
            state[_BUCKET] = bucket
        else:
            state.pop(_BUCKET, None)
        _save_state(self._path, state)

    # ---- public API ----

    def is_orphaned(self, key: str) -> bool:
        """Return True iff a tombstone exists for the key."""
        return key in self._read_bucket()

    def mark_orphaned(self, key: str, source: str = "jira_404") -> None:
        """Record a tombstone. Re-marking preserves first_seen and
        bumps last_seen — useful for telemetry / debugging.
        """
        bucket = self._read_bucket()
        existing = bucket.get(key)
        now = _now_iso()
        if existing:
            existing["last_seen"] = now
            # leave first_seen and source as-is
        else:
            bucket[key] = {
                "first_seen": now,
                "last_seen":  now,
                "source":     source,
            }
            log.warning(
                "%s: Jira issue not found; marked orphaned (source=%s) "
                "and skipped. Notion/Confluence left unchanged.",
                key, source,
            )
        self._write_bucket(bucket)

    def clear_orphaned(self, key: str) -> None:
        """Remove the tombstone. No-op if not present."""
        bucket = self._read_bucket()
        if key in bucket:
            del bucket[key]
            self._write_bucket(bucket)
            log.info("%s: Jira issue resolved again; tombstone cleared.", key)

    def probe_and_resolve(self, key: str, jira: Any) -> bool:
        """Probe Jira for the key and update the tombstone accordingly.

        Returns the orphan status AFTER the probe:
          * True  — currently orphaned (just marked, or marked-and-confirmed)
          * False — not orphaned (Jira returned 200, or probe failed
            and there was no prior tombstone)

        ``jira.issue_exists(key)`` must return:
          * True  → 200 OK, issue exists                → clear tombstone
          * False → confirmed 404                       → mark tombstone
          * None  → unknown (5xx, network, 403, etc.)   → leave state as-is
        """
        try:
            exists = jira.issue_exists(key)
        except Exception as e:
            log.warning(
                "%s: orphan probe raised %s — leaving state unchanged",
                key, type(e).__name__,
            )
            return self.is_orphaned(key)

        if exists is True:
            self.clear_orphaned(key)
            return False
        if exists is False:
            self.mark_orphaned(key, source="jira_404")
            return True
        # Unknown — keep current state.
        return self.is_orphaned(key)
