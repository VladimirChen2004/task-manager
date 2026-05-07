"""Tests for the Notion baseline refresh after own writes.

Phase 1.3: After SubtaskTodoSync writes to Notion (create_todo,
check_todo, uncheck_todo), the page's last_edited_time on the server
becomes "our" write time. The known-state baseline must record that
fresh server timestamp, otherwise the next cycle reads
last_edited_time from a fresh page query, compares to the stale
saved baseline, and concludes "Notion changed" — a false delta that
re-triggers sync work and can mask whether real edits happened.

Approach A: after own writes, refetch page metadata (one extra
GET /pages/{id}) and store the server's last_edited_time.

Constraints (from review):
  - Only refetch when this item actually had Notion-mutating actions.
  - On transient refetch failure, keep going with the old timestamp
    and emit a warning — do not break sync.
  - No "ignore-if-within-N-seconds" heuristic. We trust the server
    timestamp as the new baseline.

These tests pin down the helper that gets called from _sync_page;
no live API.
"""

from unittest.mock import MagicMock, patch

import pytest


# Action types that mutate Notion (and bump last_edited_time).
NOTION_MUTATING_OPS = {"create_todo", "check_todo", "uncheck_todo"}


def make_sync():
    """Build a SubtaskTodoSync instance bypassing __init__."""
    from taskautomation.sync import SubtaskTodoSync
    s = SubtaskTodoSync.__new__(SubtaskTodoSync)
    s.notion = MagicMock()
    s.jira = MagicMock()
    s.confluence = MagicMock()
    s.dry_run = False
    s._known = {}
    s.stats = {"errors": 0}
    return s


class TestActionsIncludeNotionMutation:
    """Helper must report whether any of the actions actually wrote to
    Notion. Refetch is gated on this, so misclassification either
    misses the refresh or wastes an API call."""

    def test_actions_with_create_todo_is_notion_mutating(self):
        from taskautomation.sync import SubtaskTodoSync
        actions = [("create_todo", "title", False, "h-id")]
        assert SubtaskTodoSync._actions_mutate_notion(actions) is True

    def test_actions_with_check_todo_is_notion_mutating(self):
        from taskautomation.sync import SubtaskTodoSync
        todo = {"id": "b1", "text": "x", "checked": False}
        actions = [("check_todo", todo)]
        assert SubtaskTodoSync._actions_mutate_notion(actions) is True

    def test_actions_with_uncheck_todo_is_notion_mutating(self):
        from taskautomation.sync import SubtaskTodoSync
        todo = {"id": "b1", "text": "x", "checked": True}
        actions = [("uncheck_todo", todo)]
        assert SubtaskTodoSync._actions_mutate_notion(actions) is True

    def test_jira_only_actions_not_notion_mutating(self):
        from taskautomation.sync import SubtaskTodoSync
        actions = [
            ("create_subtask", "title", False),
            ("close_subtask", {"key": "VC-9"}),
            ("reopen_subtask", {"key": "VC-9"}),
        ]
        assert SubtaskTodoSync._actions_mutate_notion(actions) is False

    def test_confluence_only_actions_not_notion_mutating(self):
        from taskautomation.sync import SubtaskTodoSync
        actions = [("set_conf_item", "title", True)]
        assert SubtaskTodoSync._actions_mutate_notion(actions) is False

    def test_empty_actions_not_notion_mutating(self):
        from taskautomation.sync import SubtaskTodoSync
        assert SubtaskTodoSync._actions_mutate_notion([]) is False

    def test_mixed_actions_with_one_notion_mutating(self):
        """Even one Notion-mutating action means refetch is required."""
        from taskautomation.sync import SubtaskTodoSync
        actions = [
            ("create_subtask", "title", False),
            ("set_conf_item", "title", True),
            ("check_todo", {"id": "b1", "text": "x", "checked": False}),
        ]
        assert SubtaskTodoSync._actions_mutate_notion(actions) is True


class TestRefreshHelperHappyPath:
    """When called, the helper does exactly one GET /pages/{id} and
    returns the server's last_edited_time."""

    def test_returns_fresh_last_edited_from_server(self):
        sync = make_sync()
        sync.notion.get_page.return_value = {
            "id": "page-1",
            "last_edited_time": "2026-05-07T12:34:56.000Z",
        }

        fresh = sync._refresh_notion_last_edited(
            "page-1", "2026-05-07T12:00:00.000Z"
        )

        assert fresh == "2026-05-07T12:34:56.000Z"
        sync.notion.get_page.assert_called_once_with("page-1")

    def test_does_not_invent_timestamp_if_server_omits_it(self):
        """If server response somehow lacks last_edited_time, fall back
        to the previous timestamp instead of an empty string (which
        would be treated as 'never edited' and trigger a different
        bug). Warning expected, but no crash."""
        sync = make_sync()
        sync.notion.get_page.return_value = {"id": "page-1"}  # no field

        prev = "2026-05-07T12:00:00.000Z"
        fresh = sync._refresh_notion_last_edited("page-1", prev)

        assert fresh == prev


class TestRefreshHelperTransientFailure:
    """If the refetch raises (Timeout / ConnectionError / unexpected),
    sync must not crash. We keep the old timestamp and log a warning;
    next cycle will see a false delta at most once, then converge."""

    def test_timeout_returns_old_timestamp_no_raise(self):
        import requests
        sync = make_sync()
        sync.notion.get_page.side_effect = requests.exceptions.Timeout(
            "read timed out"
        )

        prev = "2026-05-07T12:00:00.000Z"
        fresh = sync._refresh_notion_last_edited("page-1", prev)

        assert fresh == prev

    def test_connection_error_returns_old_timestamp_no_raise(self):
        import requests
        sync = make_sync()
        sync.notion.get_page.side_effect = requests.exceptions.ConnectionError(
            "broken pipe"
        )

        prev = "2026-05-07T12:00:00.000Z"
        fresh = sync._refresh_notion_last_edited("page-1", prev)

        assert fresh == prev

    def test_get_page_returns_none_returns_old_timestamp(self):
        """get_page() returning None (404, auth lost, etc.) — same
        graceful behaviour."""
        sync = make_sync()
        sync.notion.get_page.return_value = None

        prev = "2026-05-07T12:00:00.000Z"
        fresh = sync._refresh_notion_last_edited("page-1", prev)

        assert fresh == prev


class TestSyncPageIntegration:
    """End-to-end check that _sync_page calls the refresh helper IFF
    Notion-mutating actions ran, and that the refreshed timestamp is
    what gets persisted."""

    def _stub_minimal_page_state(self, sync, jira_key, page_id,
                                 stale_last_edited):
        """Wire up a minimal second-cycle scenario.

        Previous cycle baseline: jira not done, notion unchecked.
        This cycle: jira flipped to done; notion still unchecked.
        Resolver will emit ('check_todo', ...) → Notion mutation →
        refresh helper must be invoked.
        """
        # Jira: one done subtask (changed since baseline)
        sync.jira.get_subtask_details.return_value = [{
            "key": "VC-9",
            "summary": "do thing",
            "is_done": True,
            "status": "Done",
            "updated": "2026-05-07T11:00:00.000+0000",
        }]
        # Notion: heading exists, one unchecked to-do (unchanged)
        sync.notion.find_toggle_by_text.return_value = "heading-id"
        sync.notion.get_todo_children.return_value = [{
            "id": "todo-1",
            "text": "do thing",
            "checked": False,
        }]
        # Confluence disabled for this test
        sync.confluence = None
        # Known baseline from a prior cycle: jira was NOT done.
        sync._known = {
            jira_key: {
                "page_last_edited": stale_last_edited,
                "conf_version_when": "",
                "items": {
                    "do thing": {
                        "notion_checked": False,
                        "jira_checked": False,
                        "conf_checked": None,
                        "jira_key": jira_key,
                    }
                },
            }
        }
        # noops for downstream calls we don't care about
        sync.jira.update_delivery_progress_field.return_value = True
        sync._check_todo = MagicMock()
        # Page metadata as it was on read (stale)
        page = {
            "id": page_id,
            "last_edited_time": stale_last_edited,
        }
        return page

    def test_sync_page_calls_refresh_when_notion_action_executed(self):
        sync = make_sync()
        stale = "2026-05-07T12:00:00.000Z"
        fresh = "2026-05-07T12:00:05.000Z"
        page = self._stub_minimal_page_state(sync, "VC-9", "page-1", stale)

        # Server returns a NEW last_edited_time on refetch
        sync.notion.get_page.return_value = {
            "id": "page-1",
            "last_edited_time": fresh,
        }

        sync._sync_page(page, "VC-9")

        # Refresh helper was invoked
        sync.notion.get_page.assert_called_with("page-1")
        # Saved baseline carries the FRESH server timestamp, not the stale one
        saved = sync._known.get("VC-9", {})
        assert saved.get("page_last_edited") == fresh, (
            f"Expected fresh timestamp {fresh!r} in known state, "
            f"got {saved.get('page_last_edited')!r}. The next cycle would "
            f"see a false notion_changed delta."
        )

    def test_sync_page_does_not_refresh_when_no_notion_actions(self):
        sync = make_sync()
        stale = "2026-05-07T12:00:00.000Z"

        # Set up: nothing changed, no actions will be emitted.
        # Jira subtask state matches Notion to-do state matches known.
        sync.jira.get_subtask_details.return_value = [{
            "key": "VC-9", "summary": "do thing", "is_done": False,
            "status": "To Do", "updated": "2026-05-07T10:00:00.000+0000",
        }]
        sync.notion.find_toggle_by_text.return_value = "heading-id"
        sync.notion.get_todo_children.return_value = [{
            "id": "todo-1", "text": "do thing", "checked": False,
        }]
        sync.confluence = None
        sync._known = {
            "VC-9": {
                "page_last_edited": stale,
                "conf_version_when": "",
                "items": {
                    "do thing": {
                        "notion_checked": False,
                        "jira_checked": False,
                        "conf_checked": None,
                        "jira_key": "VC-9",
                    }
                },
            }
        }

        page = {"id": "page-1", "last_edited_time": stale}
        sync._sync_page(page, "VC-9")

        # No actions ran → no refetch
        sync.notion.get_page.assert_not_called()

    def test_sync_page_survives_refetch_failure(self):
        import requests
        sync = make_sync()
        stale = "2026-05-07T12:00:00.000Z"
        page = self._stub_minimal_page_state(sync, "VC-9", "page-1", stale)
        sync.notion.get_page.side_effect = requests.exceptions.Timeout(
            "read timed out"
        )

        # Must not raise
        sync._sync_page(page, "VC-9")

        # Saved baseline keeps old timestamp (graceful fallback)
        saved = sync._known.get("VC-9", {})
        assert saved.get("page_last_edited") == stale
