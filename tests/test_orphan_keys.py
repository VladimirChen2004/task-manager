"""Tests for the orphaned-Jira-key resolver.

Phase 1 follow-up: the daemon repeatedly hits /rest/api/3/issue/{key}
with 404 for Jira issues that have been deleted while their Notion
and Confluence counterparts live on (e.g. VC-114). Treating that as
an ERROR every cycle drowns real signal in known noise.

Resolver responsibilities:
- mark_orphaned(key, source): record a tombstone in state with
  first_seen / last_seen / source.
- is_orphaned(key): O(1) check used by phase loops to skip the key
  before they make any destructive write.
- clear_orphaned(key): remove tombstone (e.g. when Jira returns 200
  again — issue restored).
- probe_and_resolve(key, jira): single-call helper used per cycle:
  GET /issue/{key}; on 404 → mark; on 200 → clear; on anything else
  (5xx, network, permission) → do NOT mark, do NOT clear.

Tests use mocks only — no live API.
"""

from __future__ import annotations

import json
from typing import Optional
from unittest.mock import MagicMock

import pytest


def make_state_file(tmp_path, state: Optional[dict] = None):
    """Write a state JSON, return its Path."""
    p = tmp_path / "sync_state.json"
    p.write_text(json.dumps(state or {}, ensure_ascii=False), encoding="utf-8")
    return p


class TestMarkAndCheck:
    def test_mark_then_is_orphaned_true(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        assert r.is_orphaned("VC-114") is True

    def test_unknown_key_not_orphaned(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        assert r.is_orphaned("VC-999") is False

    def test_mark_persists_to_disk(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")

        on_disk = json.loads(sf.read_text(encoding="utf-8"))
        assert "orphaned_jira_keys" in on_disk
        bucket = on_disk["orphaned_jira_keys"]
        assert "VC-114" in bucket
        entry = bucket["VC-114"]
        assert entry["source"] == "jira_404"
        assert "first_seen" in entry and entry["first_seen"]
        assert "last_seen" in entry and entry["last_seen"]

    def test_mark_again_keeps_first_seen_updates_last_seen(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        first = json.loads(sf.read_text())["orphaned_jira_keys"]["VC-114"]
        original_first_seen = first["first_seen"]

        # Re-mark — must not overwrite first_seen, must update last_seen.
        # We simulate "later" by stubbing _now via fresh resolver.
        r2 = OrphanResolver(sf)
        r2.mark_orphaned("VC-114", source="jira_404")
        again = json.loads(sf.read_text())["orphaned_jira_keys"]["VC-114"]
        assert again["first_seen"] == original_first_seen
        # last_seen may equal or exceed; just ensure present
        assert again["last_seen"]

    def test_clear_removes_tombstone(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        r.clear_orphaned("VC-114")
        assert r.is_orphaned("VC-114") is False
        on_disk = json.loads(sf.read_text())
        assert "VC-114" not in on_disk.get("orphaned_jira_keys", {})

    def test_clear_unknown_key_is_noop(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.clear_orphaned("VC-999")  # must not raise
        assert r.is_orphaned("VC-999") is False

    def test_loads_existing_tombstones_from_state(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path, {
            "orphaned_jira_keys": {
                "VC-114": {
                    "first_seen": "2026-05-07T12:00:00Z",
                    "last_seen": "2026-05-07T12:00:00Z",
                    "source": "jira_404",
                },
            },
        })
        r = OrphanResolver(sf)
        assert r.is_orphaned("VC-114") is True


class TestProbeAndResolve:
    """Single-call helper: probe Jira, update tombstone accordingly."""

    def test_404_marks_orphan(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        jira = MagicMock()
        jira.issue_exists.return_value = False  # 404

        result = r.probe_and_resolve("VC-114", jira)

        assert result is True  # is_orphaned after probe
        assert r.is_orphaned("VC-114") is True

    def test_200_clears_existing_tombstone(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        jira = MagicMock()
        jira.issue_exists.return_value = True  # 200, restored

        result = r.probe_and_resolve("VC-114", jira)

        assert result is False  # not orphaned anymore
        assert r.is_orphaned("VC-114") is False

    def test_unknown_response_does_not_mark(self, tmp_path):
        """5xx / network / permission denied → issue_exists() returns
        None. Must NOT mark tombstone — orphan status is unknown."""
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        jira = MagicMock()
        jira.issue_exists.return_value = None  # Jira down / 503 / network

        result = r.probe_and_resolve("VC-999", jira)

        assert result is False  # current state: not orphaned
        assert r.is_orphaned("VC-999") is False

    def test_unknown_response_does_not_clear_existing(self, tmp_path):
        """Existing tombstone must survive a transient probe failure."""
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        jira = MagicMock()
        jira.issue_exists.return_value = None

        result = r.probe_and_resolve("VC-114", jira)

        assert result is True  # still orphaned per saved state
        assert r.is_orphaned("VC-114") is True


class TestProbeForWrite:
    """``probe_for_write`` is a fail-closed tri-state probe used by
    write paths (backfill, sync_page when it leads to writes, etc.).

    Verdict matrix:
    - issue_exists True   → "alive"     + tombstone cleared
    - issue_exists False  → "orphaned"  + tombstone marked
    - issue_exists None   → "unknown"   + tombstone untouched
    - exception           → "unknown"   + tombstone untouched
    """

    def test_alive_clears_existing_tombstone(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        jira = MagicMock()
        jira.issue_exists.return_value = True

        verdict = r.probe_for_write("VC-114", jira)

        assert verdict == r.WRITE_ALIVE
        assert r.is_orphaned("VC-114") is False

    def test_orphaned_marks_tombstone(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        jira = MagicMock()
        jira.issue_exists.return_value = False

        verdict = r.probe_for_write("VC-114", jira)

        assert verdict == r.WRITE_ORPHANED
        assert r.is_orphaned("VC-114") is True

    def test_unknown_returns_unknown_no_tombstone_change(self, tmp_path):
        """The critical fail-closed property: unknown probe must not
        invent a tombstone (we don't know it's orphaned) AND must not
        return ALIVE (caller would write into a possible orphan).
        """
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        jira = MagicMock()
        jira.issue_exists.return_value = None

        verdict = r.probe_for_write("VC-999", jira)

        assert verdict == r.WRITE_UNKNOWN
        # No tombstone fabricated.
        assert r.is_orphaned("VC-999") is False
        on_disk = json.loads(sf.read_text())
        assert "VC-999" not in on_disk.get("orphaned_jira_keys", {})

    def test_unknown_preserves_existing_tombstone(self, tmp_path):
        """If a tombstone already exists and the probe goes unknown,
        we must not clear it — we still don't have evidence the issue
        is alive."""
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        jira = MagicMock()
        jira.issue_exists.return_value = None

        verdict = r.probe_for_write("VC-114", jira)

        assert verdict == r.WRITE_UNKNOWN
        assert r.is_orphaned("VC-114") is True

    def test_exception_returns_unknown(self, tmp_path):
        """Any exception out of issue_exists must be caught and turned
        into UNKNOWN — write paths never see a raised probe error."""
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        r = OrphanResolver(sf)
        jira = MagicMock()
        jira.issue_exists.side_effect = RuntimeError("network blip")

        verdict = r.probe_for_write("VC-999", jira)

        assert verdict == r.WRITE_UNKNOWN
        assert r.is_orphaned("VC-999") is False

    def test_verdict_constants_are_strings_not_collisions(self):
        """Smoke: the three verdict constants are distinct strings,
        not booleans or accidentally-equal sentinels."""
        from taskautomation.orphan_keys import OrphanResolver
        verdicts = {
            OrphanResolver.WRITE_ALIVE,
            OrphanResolver.WRITE_ORPHANED,
            OrphanResolver.WRITE_UNKNOWN,
        }
        assert len(verdicts) == 3
        for v in verdicts:
            assert isinstance(v, str) and v


class TestIssueExistsClient:
    """JiraVCHEN.issue_exists must distinguish 404 from other failures."""

    def _make_jira(self):
        from taskautomation.jira_client import JiraVCHEN
        c = JiraVCHEN.__new__(JiraVCHEN)
        c.server = "https://example.atlassian.net"
        c._auth = ("u@x", "tok")
        return c

    def test_returns_true_on_200(self):
        c = self._make_jira()
        c._request = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        c._request.return_value = resp
        assert c.issue_exists("VC-1") is True

    def test_returns_false_on_404(self):
        c = self._make_jira()
        c._request = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        c._request.return_value = resp
        assert c.issue_exists("VC-114") is False

    def test_returns_none_on_5xx(self):
        c = self._make_jira()
        c._request = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        c._request.return_value = resp
        assert c.issue_exists("VC-1") is None

    def test_returns_none_on_403(self):
        """Permission removed — could be temporary, not deletion."""
        c = self._make_jira()
        c._request = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        c._request.return_value = resp
        assert c.issue_exists("VC-1") is None

    def test_returns_none_on_network_error(self):
        import requests as http_requests
        c = self._make_jira()
        c._request = MagicMock(
            side_effect=http_requests.exceptions.Timeout("read timed out")
        )
        assert c.issue_exists("VC-1") is None


class TestStateFileResilience:
    def test_handles_corrupt_state_file(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = tmp_path / "sync_state.json"
        sf.write_text("{not valid json", encoding="utf-8")
        # Must not raise; resolver starts with empty bucket.
        r = OrphanResolver(sf)
        assert r.is_orphaned("VC-1") is False
        # After mark, file is rewritten with valid JSON.
        r.mark_orphaned("VC-1", source="jira_404")
        on_disk = json.loads(sf.read_text())
        assert "VC-1" in on_disk["orphaned_jira_keys"]

    def test_preserves_other_state_buckets(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path, {
            "subtask_todos": {"VC-1": {"foo": "bar"}},
            "template_backfilled": ["VC-1", "VC-2"],
        })
        r = OrphanResolver(sf)
        r.mark_orphaned("VC-114", source="jira_404")
        on_disk = json.loads(sf.read_text())
        assert on_disk.get("subtask_todos") == {"VC-1": {"foo": "bar"}}
        assert on_disk.get("template_backfilled") == ["VC-1", "VC-2"]
        assert "VC-114" in on_disk["orphaned_jira_keys"]


class TestConfluenceSyncIntegration:
    """ConfluenceSync.run() must skip orphaned keys — no Notion /
    Confluence writes for them, no ERROR log, errors counter does
    not advance, skipped_orphan counter does."""

    def _make_sync(self, tmp_path):
        from taskautomation import sync as sync_module
        from taskautomation.sync import ConfluenceSync
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        # Redirect resolver state file used by ConfluenceSync.
        # ConfluenceSync resolves OrphanResolver(STATE_FILE) at __init__.
        # We swap the module-level STATE_FILE for the duration of the
        # test via attribute override.
        original_state_file = sync_module.STATE_FILE
        sync_module.STATE_FILE = sf
        try:
            cs = ConfluenceSync.__new__(ConfluenceSync)
            cs.jira = MagicMock()
            cs.notion = MagicMock()
            cs.confluence = MagicMock()
            cs.dry_run = False
            cs.stats = {
                "checked": 0, "created": 0, "updated": 0, "linked": 0,
                "skipped": 0, "skipped_orphan": 0, "skipped_unknown": 0, "errors": 0,
            }
            cs._state = {}
            cs._linked_keys = set()
            cs._orphans = OrphanResolver(sf)
        finally:
            sync_module.STATE_FILE = original_state_file
        return cs, sf

    def test_404_key_is_skipped_no_writes_no_error(self, tmp_path):
        from taskautomation.notion_client import NotionClient
        cs, sf = self._make_sync(tmp_path)

        # One Notion page with Jira Key VC-114
        cs.notion.query_all_pages_with_jira_key.return_value = [{
            "id": "p1",
            "properties": {
                "Jira Key": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "VC-114"},
                        "plain_text": "VC-114",
                    }],
                },
            },
        }]
        # Jira says: 404 → issue_exists returns False
        cs.jira.issue_exists.return_value = False

        # Make sleep instant for the test
        import taskautomation.sync as sync_module
        original_sleep = sync_module.time.sleep
        sync_module.time.sleep = lambda *a, **kw: None
        try:
            cs.run()
        finally:
            sync_module.time.sleep = original_sleep

        # Probe ran exactly once
        cs.jira.issue_exists.assert_called_once_with("VC-114")
        # No destructive writes / further Jira queries on the orphan
        cs.jira.get_issue.assert_not_called()
        cs.confluence.find_page_by_jira_key.assert_not_called()
        cs.confluence.find_or_create_page.assert_not_called()
        cs.confluence.update_page.assert_not_called()
        cs.notion.update_page_properties.assert_not_called()
        # Counters: orphan skipped, errors zero
        assert cs.stats["skipped_orphan"] == 1
        assert cs.stats["errors"] == 0
        assert cs.stats["created"] == 0
        # State file has tombstone
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {})

    def test_existing_tombstone_persists_when_jira_unknown(self, tmp_path):
        """If issue_exists returns None (5xx / network), the existing
        tombstone is kept and the key is still skipped — no destructive
        action runs while we wait for clarity. Under fail-closed
        semantics the verdict is "unknown" (not "orphaned"), so the
        skipped_unknown counter advances and tombstone is untouched."""
        from taskautomation.orphan_keys import OrphanResolver
        cs, sf = self._make_sync(tmp_path)
        # Pre-seed tombstone
        OrphanResolver(sf).mark_orphaned("VC-114", source="jira_404")
        cs._orphans = OrphanResolver(sf)

        cs.notion.query_all_pages_with_jira_key.return_value = [{
            "id": "p1",
            "properties": {
                "Jira Key": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "VC-114"},
                        "plain_text": "VC-114",
                    }],
                },
            },
        }]
        cs.jira.issue_exists.return_value = None  # transient

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        cs.run()

        cs.jira.get_issue.assert_not_called()
        cs.confluence.find_or_create_page.assert_not_called()
        # Verdict is "unknown" — skipped_unknown advances, not skipped_orphan.
        assert cs.stats["skipped_unknown"] == 1
        assert cs.stats["skipped_orphan"] == 0
        assert cs.stats["errors"] == 0
        # Pre-existing tombstone is untouched by the unknown probe.
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {})

    def test_save_does_not_clobber_orphan_bucket(self, tmp_path):
        """Regression: phase _save() must not overwrite the
        orphaned_jira_keys bucket that OrphanResolver wrote during
        the cycle. Previously SubtaskTodoSync._save and
        ConfluenceSync._save did `_save_state(self._state)` with a
        stale in-memory state, erasing tombstones added mid-cycle.
        Fix is read-modify-write in _save.
        """
        from taskautomation.orphan_keys import OrphanResolver
        cs, sf = self._make_sync(tmp_path)

        cs.notion.query_all_pages_with_jira_key.return_value = [{
            "id": "p1",
            "properties": {
                "Jira Key": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "VC-114"},
                        "plain_text": "VC-114",
                    }],
                },
            },
        }]
        cs.jira.issue_exists.return_value = False  # 404

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        cs.run()

        # Tombstone must SURVIVE the phase's _save() call.
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {}), (
            "phase _save() clobbered orphaned_jira_keys bucket — "
            "use read-modify-write in _save."
        )

    def test_restored_jira_clears_tombstone_and_runs_sync(self, tmp_path):
        """If Jira now returns 200 for a previously-orphaned key, the
        tombstone is cleared and normal sync proceeds."""
        from taskautomation.orphan_keys import OrphanResolver
        cs, sf = self._make_sync(tmp_path)
        OrphanResolver(sf).mark_orphaned("VC-114", source="jira_404")
        cs._orphans = OrphanResolver(sf)

        cs.notion.query_all_pages_with_jira_key.return_value = [{
            "id": "p1",
            "properties": {
                "Jira Key": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "VC-114"},
                        "plain_text": "VC-114",
                    }],
                    },
            },
        }]
        cs.jira.issue_exists.return_value = True  # restored
        # Stub _sync_page to a no-op so we don't have to mock the
        # full Confluence/Jira flow inside it.
        cs._sync_page = MagicMock()

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        cs.run()

        # Tombstone gone
        on_disk = json.loads(sf.read_text())
        assert "VC-114" not in on_disk.get("orphaned_jira_keys", {})
        # Normal sync proceeded
        cs._sync_page.assert_called_once()
        assert cs.stats["skipped_orphan"] == 0
        assert cs.stats["errors"] == 0


def _notion_page(jira_key: str, page_id: str = "p1") -> dict:
    """Helper: build a minimal Notion page dict carrying a Jira Key."""
    return {
        "id": page_id,
        "properties": {
            "Jira Key": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": jira_key},
                    "plain_text": jira_key,
                }],
            },
        },
    }


class TestSubtaskTodoSyncIntegration:
    """SubtaskTodoSync.run() must skip orphaned keys before any Jira
    fetch — no `get_subtask_details`, no Notion to-do read, no error.
    Mirrors the ConfluenceSync integration suite."""

    def _make_sync(self, tmp_path):
        from taskautomation import sync as sync_module
        from taskautomation.sync import SubtaskTodoSync
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        original = sync_module.STATE_FILE
        sync_module.STATE_FILE = sf
        try:
            s = SubtaskTodoSync.__new__(SubtaskTodoSync)
            s.jira = MagicMock()
            s.notion = MagicMock()
            s.confluence = MagicMock()
            s.dry_run = False
            s.stats = {
                "pages_checked": 0, "todos_synced": 0,
                "subtasks_created": 0, "subtasks_deleted": 0,
                "todos_created": 0, "checked_updated": 0,
                "skipped_orphan": 0, "skipped_unknown": 0, "errors": 0,
            }
            s._state = {}
            s._known = {}
            s._orphans = OrphanResolver(sf)
        finally:
            sync_module.STATE_FILE = original
        return s, sf

    def test_404_key_is_skipped_no_jira_fetch(self, tmp_path):
        s, sf = self._make_sync(tmp_path)
        s.notion.query_all_pages_with_jira_key.return_value = [
            _notion_page("VC-114"),
        ]
        s.jira.issue_exists.return_value = False

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        s.run()

        s.jira.issue_exists.assert_called_once_with("VC-114")
        s.jira.get_subtask_details.assert_not_called()
        s.notion.get_todo_children.assert_not_called()
        assert s.stats["skipped_orphan"] == 1
        assert s.stats["errors"] == 0
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {})

    def test_save_does_not_clobber_orphan_bucket(self, tmp_path):
        """Regression: SubtaskTodoSync._save() must use read-modify-
        write so it doesn't erase tombstones written mid-cycle."""
        s, sf = self._make_sync(tmp_path)
        s.notion.query_all_pages_with_jira_key.return_value = [
            _notion_page("VC-114"),
        ]
        s.jira.issue_exists.return_value = False

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        s.run()

        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {}), (
            "SubtaskTodoSync._save() clobbered orphan bucket — "
            "use read-modify-write."
        )

    def test_unknown_jira_skips_write_no_tombstone(self, tmp_path):
        """FAIL-CLOSED: when Jira probe returns None, _sync_page must
        NOT run (it would reach destructive Notion/Confluence writes),
        no tombstone is fabricated, skipped_unknown advances."""
        s, sf = self._make_sync(tmp_path)
        s.notion.query_all_pages_with_jira_key.return_value = [
            _notion_page("VC-200"),
        ]
        s.jira.issue_exists.return_value = None  # transient
        s._sync_page = MagicMock()

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        s.run()

        s._sync_page.assert_not_called()
        on_disk = json.loads(sf.read_text())
        assert "VC-200" not in on_disk.get("orphaned_jira_keys", {})
        assert s.stats["skipped_unknown"] == 1
        assert s.stats["skipped_orphan"] == 0
        assert s.stats["errors"] == 0


class TestSectionSyncIntegration:
    """SectionSync doesn't probe Jira directly — it consults the
    existing tombstone (written earlier in the same cycle by
    SubtaskTodoSync or ConfluenceSync) and skips destructive
    Notion↔Confluence reconciliation on orphan pairs."""

    def _make_sync(self, tmp_path):
        from taskautomation import sync as sync_module
        from taskautomation.sync import SectionSync
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        original = sync_module.STATE_FILE
        sync_module.STATE_FILE = sf
        try:
            s = SectionSync.__new__(SectionSync)
            s.jira = MagicMock()
            s.notion = MagicMock()
            s.confluence = MagicMock()
            s.dry_run = False
            s.stats = {
                "checked": 0, "notion_to_conf": 0, "conf_to_notion": 0,
                "conflicts": 0, "skipped": 0, "skipped_orphan": 0, "skipped_unknown": 0, "errors": 0,
            }
            s._state = {}
            s._section_state = {}
            s._orphans = OrphanResolver(sf)
        finally:
            sync_module.STATE_FILE = original
        return s, sf

    def test_pre_marked_orphan_is_skipped(self, tmp_path):
        from taskautomation.orphan_keys import OrphanResolver
        s, sf = self._make_sync(tmp_path)
        # Pre-existing tombstone (e.g. ConfluenceSync wrote it earlier
        # this cycle).
        OrphanResolver(sf).mark_orphaned("VC-114", source="jira_404")
        s._orphans = OrphanResolver(sf)
        # probe_and_resolve will still call issue_exists; emulate 404.
        s.jira.issue_exists.return_value = False

        s.notion.query_all_pages_with_jira_key.return_value = [
            _notion_page("VC-114"),
        ]
        s._sync_task = MagicMock()

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        s.run()

        s._sync_task.assert_not_called()
        assert s.stats["skipped_orphan"] == 1
        assert s.stats["errors"] == 0

    def test_orphan_skip_works_without_prior_tombstone(self, tmp_path):
        """SectionSync must NOT depend on phase ordering. If it runs
        first (or stand-alone) and Jira returns 404, SectionSync
        itself marks the tombstone via probe_and_resolve and skips."""
        s, sf = self._make_sync(tmp_path)
        # No pre-existing tombstone.
        s.jira.issue_exists.return_value = False  # 404

        s.notion.query_all_pages_with_jira_key.return_value = [
            _notion_page("VC-114"),
        ]
        s._sync_task = MagicMock()

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        s.run()

        s._sync_task.assert_not_called()
        assert s.stats["skipped_orphan"] == 1
        # Tombstone is now persisted for future phases / cycles.
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {})

    def test_no_tombstone_lets_sync_proceed(self, tmp_path):
        s, sf = self._make_sync(tmp_path)
        s.jira.issue_exists.return_value = True  # 200 — issue exists
        s.notion.query_all_pages_with_jira_key.return_value = [
            _notion_page("VC-200"),
        ]
        s._sync_task = MagicMock()

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        s.run()

        s._sync_task.assert_called_once()
        assert s.stats["skipped_orphan"] == 0

    def test_unknown_jira_skips_write_no_tombstone(self, tmp_path):
        """FAIL-CLOSED: SectionSync writes both Notion and Confluence
        from _sync_task. Unknown probe must skip without tombstone."""
        s, sf = self._make_sync(tmp_path)
        s.jira.issue_exists.return_value = None  # transient
        s.notion.query_all_pages_with_jira_key.return_value = [
            _notion_page("VC-200"),
        ]
        s._sync_task = MagicMock()

        import taskautomation.sync as sync_module
        sync_module.time.sleep = lambda *a, **kw: None

        s.run()

        s._sync_task.assert_not_called()
        on_disk = json.loads(sf.read_text())
        assert "VC-200" not in on_disk.get("orphaned_jira_keys", {})
        assert s.stats["skipped_unknown"] == 1
        assert s.stats["skipped_orphan"] == 0


class TestNotionToJiraDeletePath:
    """When a Notion page disappears, NotionToJiraSync._handle_deleted_pages
    waits 2 cycles, then archives the Jira issue. If the Jira issue
    is already orphaned, it must NOT call jira.delete_issue (which
    would 404), and it must clean local bookkeeping silently."""

    def _make_sync(self, tmp_path):
        from taskautomation import sync as sync_module
        from taskautomation.sync import NotionToJiraSync
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        original = sync_module.STATE_FILE
        sync_module.STATE_FILE = sf
        try:
            s = NotionToJiraSync.__new__(NotionToJiraSync)
            s.jira = MagicMock()
            s.notion = MagicMock()
            s.dry_run = False
            s.stats = {
                "checked": 0, "updated": 0, "skipped": 0,
                "skipped_orphan": 0, "skipped_unknown": 0, "errors": 0,
            }
            # Pretend VC-114 has been known for a while and is now
            # missing from current Notion query for 2nd consecutive
            # cycle.
            s._state = {"missing_keys": {"VC-114": 1}}
            s._known = {"VC-114": {"notion": "Done", "jira": "Done"}}
            s._orphans = OrphanResolver(sf)
        finally:
            sync_module.STATE_FILE = original
        return s, sf

    def test_orphan_skips_jira_delete_when_pre_marked(self, tmp_path):
        """Pre-existing tombstone means we already know Jira lost the
        issue — don't call delete_issue."""
        from taskautomation.orphan_keys import OrphanResolver
        s, sf = self._make_sync(tmp_path)
        OrphanResolver(sf).mark_orphaned("VC-114", source="jira_404")
        s._orphans = OrphanResolver(sf)

        s._handle_deleted_pages(current_keys=set())  # VC-114 missing

        s.jira.delete_issue.assert_not_called()
        s.jira.issue_exists.assert_not_called()
        assert s.stats["skipped_orphan"] == 1
        assert s.stats["errors"] == 0
        # Local bookkeeping cleaned
        assert "VC-114" not in s._known
        assert "VC-114" not in s._state["missing_keys"]

    def test_jira_404_via_probe_marks_orphan_no_delete_call(self, tmp_path):
        """No prior tombstone, but live probe shows Jira is 404 →
        mark orphan, don't call delete_issue, clean local state."""
        s, sf = self._make_sync(tmp_path)
        s.jira.issue_exists.return_value = False

        s._handle_deleted_pages(current_keys=set())

        s.jira.issue_exists.assert_called_once_with("VC-114")
        s.jira.delete_issue.assert_not_called()
        assert s.stats["skipped_orphan"] == 1
        assert s.stats["errors"] == 0
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {})
        assert "VC-114" not in s._known

    def test_transient_probe_failure_does_not_delete_or_mark(self, tmp_path):
        """issue_exists returning None → skip this cycle, retry later.
        No delete, no tombstone, skipped_unknown counter advances."""
        s, sf = self._make_sync(tmp_path)
        s.jira.issue_exists.return_value = None

        s._handle_deleted_pages(current_keys=set())

        s.jira.delete_issue.assert_not_called()
        on_disk = json.loads(sf.read_text())
        assert "VC-114" not in on_disk.get("orphaned_jira_keys", {})
        # Local state preserved for next cycle's retry
        assert "VC-114" in s._known
        # skipped_unknown advanced; skipped_orphan did not.
        assert s.stats["skipped_unknown"] == 1
        assert s.stats["skipped_orphan"] == 0

    def test_jira_200_lets_delete_proceed(self, tmp_path):
        """Live issue → normal delete path. Tombstone must NOT be set."""
        s, sf = self._make_sync(tmp_path)
        s.jira.issue_exists.return_value = True
        s.jira.delete_issue.return_value = True

        s._handle_deleted_pages(current_keys=set())

        s.jira.issue_exists.assert_called_once_with("VC-114")
        s.jira.delete_issue.assert_called_once_with("VC-114")
        assert s.stats["updated"] == 1
        assert s.stats["skipped_orphan"] == 0
        on_disk = json.loads(sf.read_text())
        assert "VC-114" not in on_disk.get("orphaned_jira_keys", {})


class TestNotionToJiraBackfillSkipsOrphan:
    """Regression for the gap surfaced by post-merge audit:
    NotionToJiraSync._backfill_templates iterates every page with a
    Jira Key and may call notion.append_children via
    _add_template_sections. On an orphaned key that violates the
    "leave Notion/Confluence unchanged" invariant of orphan policy.
    """

    def _make_sync(self, tmp_path):
        from taskautomation import sync as sync_module
        from taskautomation.sync import NotionToJiraSync
        from taskautomation.orphan_keys import OrphanResolver
        sf = make_state_file(tmp_path)
        original = sync_module.STATE_FILE
        sync_module.STATE_FILE = sf
        try:
            s = NotionToJiraSync.__new__(NotionToJiraSync)
            s.jira = MagicMock()
            s.notion = MagicMock()
            s.dry_run = False
            s.stats = {
                "checked": 0, "updated": 0, "skipped": 0,
                "skipped_orphan": 0, "skipped_unknown": 0, "errors": 0,
            }
            s._state = {"template_backfilled": []}
            s._known = {}
            s._orphans = OrphanResolver(sf)
        finally:
            sync_module.STATE_FILE = original
        return s, sf

    def test_orphan_page_without_template_is_not_backfilled(
        self, tmp_path, monkeypatch
    ):
        """Pre-existing tombstone case: probe still runs but keeps the
        tombstone (Jira still 404), backfill is skipped."""
        from taskautomation.orphan_keys import OrphanResolver
        s, sf = self._make_sync(tmp_path)
        OrphanResolver(sf).mark_orphaned("VC-114", source="jira_404")
        s._orphans = OrphanResolver(sf)
        s.jira.issue_exists.return_value = False  # 404, still orphan

        # Notion page lacks the MVP toggle — without orphan gate, the
        # backfill would call _add_template_sections and write blocks.
        s.notion.find_toggle_by_text.return_value = None

        # Spy on the module-level helper that does the writes.
        import taskautomation.sync as sync_module
        called_with = []

        def fake_add(notion, page_id, jira_key):
            called_with.append((page_id, jira_key))

        monkeypatch.setattr(
            sync_module, "_add_template_sections", fake_add
        )

        s._backfill_templates([_notion_page("VC-114", page_id="p1")])

        assert called_with == [], (
            f"_add_template_sections must not run for orphaned key, "
            f"but was called with {called_with!r}"
        )
        assert s.stats["skipped_orphan"] == 1

    def test_first_cycle_orphan_caught_by_probe_no_append(
        self, tmp_path, monkeypatch
    ):
        """Critical regression: NotionToJiraSync runs in Phase 4,
        BEFORE the phases that normally set the tombstone (Subtask↔Todo
        in 5, Confluence in 6, SectionSync in 7). On the first cycle
        when a key becomes 404, no tombstone exists yet.

        Without an in-phase probe, _backfill_templates would call
        notion.append_children on the orphaned page if its MVP toggle
        is missing — violating the orphan policy on cycle 1.

        With probe_and_resolve, this phase itself catches the 404,
        marks the tombstone (so phases 5/6/7 also skip on cycle 1),
        and skips the destructive write.
        """
        s, sf = self._make_sync(tmp_path)
        # Empty state — no tombstone for VC-114.
        assert "VC-114" not in s._orphans._read_bucket()

        s.jira.issue_exists.return_value = False  # 404, fresh orphan
        # Page has no MVP toggle → would otherwise be backfilled.
        s.notion.find_toggle_by_text.return_value = None

        import taskautomation.sync as sync_module
        called_with = []

        def fake_add(notion, page_id, jira_key):
            called_with.append((page_id, jira_key))

        monkeypatch.setattr(
            sync_module, "_add_template_sections", fake_add
        )

        s._backfill_templates([_notion_page("VC-114", page_id="p1")])

        assert called_with == [], (
            "First-cycle orphan was written by _add_template_sections; "
            "probe_and_resolve must catch this before the append."
        )
        assert s.stats["skipped_orphan"] == 1
        # Tombstone was just written by the probe — downstream phases
        # in this same cycle will also skip.
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {})

    def test_live_page_without_template_is_backfilled(
        self, tmp_path, monkeypatch
    ):
        """Sanity: a non-orphan page still gets the template."""
        s, sf = self._make_sync(tmp_path)
        s.jira.issue_exists.return_value = True  # 200, alive
        s.notion.find_toggle_by_text.return_value = None

        import taskautomation.sync as sync_module
        called_with = []

        def fake_add(notion, page_id, jira_key):
            called_with.append((page_id, jira_key))

        monkeypatch.setattr(
            sync_module, "_add_template_sections", fake_add
        )

        s._backfill_templates([_notion_page("VC-200", page_id="p2")])

        assert called_with == [("p2", "VC-200")]
        assert s.stats["skipped_orphan"] == 0

    def test_transient_jira_failure_skips_write_but_does_not_tombstone(
        self, tmp_path, monkeypatch
    ):
        """FAIL-CLOSED on unknown. If Jira returns 5xx / network error,
        issue_exists() returns None and probe_for_write returns
        WRITE_UNKNOWN. Backfill MUST skip the destructive write —
        we don't know whether the key is alive or orphaned, and the
        previous fail-open policy could write into a real orphan.
        Tombstone is NOT created (we can't claim to know it's an
        orphan). The next cycle retries.
        """
        s, sf = self._make_sync(tmp_path)
        s.jira.issue_exists.return_value = None  # transient
        s.notion.find_toggle_by_text.return_value = None

        import taskautomation.sync as sync_module
        called_with = []

        def fake_add(notion, page_id, jira_key):
            called_with.append((page_id, jira_key))

        monkeypatch.setattr(
            sync_module, "_add_template_sections", fake_add
        )

        s._backfill_templates([_notion_page("VC-200", page_id="p2")])

        # Backfill SKIPPED — no Notion write happened.
        assert called_with == [], (
            "Backfill must skip on unknown Jira state, not fail-open. "
            "Got destructive call: %r" % called_with
        )
        # No tombstone fabricated — we don't know it's orphaned.
        on_disk = json.loads(sf.read_text())
        assert "VC-200" not in on_disk.get("orphaned_jira_keys", {})
        # skipped_unknown counter advanced; skipped_orphan did not.
        assert s.stats["skipped_unknown"] == 1
        assert s.stats["skipped_orphan"] == 0
