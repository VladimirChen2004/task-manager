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
                "skipped": 0, "skipped_orphan": 0, "errors": 0,
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
        action runs while we wait for clarity."""
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
        assert cs.stats["skipped_orphan"] == 1
        assert cs.stats["errors"] == 0
        on_disk = json.loads(sf.read_text())
        assert "VC-114" in on_disk.get("orphaned_jira_keys", {})

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
