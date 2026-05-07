"""Tests for SubtaskTodoSync._resolve_item safety behavior.

Verifies that:
1. When known state is empty (first encounter), we ONLY create missing items,
   never change existing checked states.
2. When no changes detected vs known state, we do nothing.
3. Confluence priority is correct in _update_confluence_from_unified.
"""

import pytest
from unittest.mock import MagicMock, patch


def make_sync(dry_run=False):
    """Create a SubtaskTodoSync with mocked dependencies."""
    from taskautomation.sync import SubtaskTodoSync

    sync = SubtaskTodoSync.__new__(SubtaskTodoSync)
    sync.jira = MagicMock()
    sync.notion = MagicMock()
    sync.confluence = MagicMock()
    sync.dry_run = dry_run
    sync.stats = {"todos_synced": 0}
    sync.PLAN_HEADING = "План выполнения"
    return sync


class TestResolveItemNoKnown:
    """P0.1: When known state is empty, only create missing items."""

    def test_all_three_exist_no_actions(self):
        """If item exists in all 3 sources, no actions on first encounter."""
        sync = make_sync()
        item = {
            "title": "Test task",
            "match_key": "test task",
            "notion": {"checked": True, "id": "n1"},
            "jira": {"is_done": False, "key": "VC-1", "summary": "Test task", "updated": ""},
            "conf": {"checked": True, "text": "Test task"},
        }
        actions = sync._resolve_item("VC-1", item, {}, {}, "h1")
        assert actions == [], f"Expected no actions when all sources exist, got {actions}"

    def test_missing_jira_creates_subtask_only(self):
        """If item exists in Notion+Confluence but not Jira, create subtask only."""
        sync = make_sync()
        item = {
            "title": "Test task",
            "match_key": "test task",
            "notion": {"checked": True, "id": "n1"},
            "jira": None,
            "conf": {"checked": True, "text": "Test task"},
        }
        actions = sync._resolve_item("VC-1", item, {}, {}, "h1")
        assert len(actions) == 1
        assert actions[0][0] == "create_subtask"
        assert actions[0][1] == "Test task"

    def test_missing_notion_creates_todo_only(self):
        """If item exists in Jira+Confluence but not Notion, create todo only."""
        sync = make_sync()
        item = {
            "title": "Test task",
            "match_key": "test task",
            "notion": None,
            "jira": {"is_done": False, "key": "VC-1", "summary": "Test task", "updated": ""},
            "conf": {"checked": False, "text": "Test task"},
        }
        actions = sync._resolve_item("VC-1", item, {}, {}, "h1")
        assert len(actions) == 1
        assert actions[0][0] == "create_todo"

    def test_never_changes_existing_checked_states(self):
        """Even if Notion=checked and Jira=unchecked, neither should be modified."""
        sync = make_sync()
        item = {
            "title": "Test task",
            "match_key": "test task",
            "notion": {"checked": True, "id": "n1"},
            "jira": {"is_done": False, "key": "VC-1", "summary": "Test task", "updated": ""},
            "conf": {"checked": True, "text": "Test task"},
        }
        actions = sync._resolve_item("VC-1", item, {}, {}, "h1")
        # Must NOT contain check_todo, uncheck_todo, close_subtask,
        # reopen_subtask, or set_conf_item
        forbidden = {"check_todo", "uncheck_todo", "close_subtask",
                     "reopen_subtask", "set_conf_item"}
        for action in actions:
            assert action[0] not in forbidden, \
                f"Baseline should not modify existing state, got action: {action}"


class TestResolveItemNoChanges:
    """P0.1: When known state exists but nothing changed, do nothing."""

    def test_no_delta_no_actions(self):
        """If current state matches known state, return empty actions."""
        sync = make_sync()
        item = {
            "title": "Test task",
            "match_key": "test task",
            "notion": {"checked": True, "id": "n1"},
            "jira": {"is_done": False, "key": "VC-1", "summary": "Test task", "updated": ""},
            "conf": {"checked": True, "text": "Test task"},
        }
        known_items = {
            "test task": {
                "notion_checked": True,
                "jira_checked": False,
                "conf_checked": True,
                "jira_key": "VC-1",
            }
        }
        actions = sync._resolve_item("VC-1", item, known_items, {}, "h1")
        # Values disagree (notion=True, jira=False) but nothing changed
        # vs known → safe default: do nothing
        assert actions == [], f"Expected no actions when no delta, got {actions}"


class TestConfluencePriority:
    """P0.2: When no override, Confluence state should be preserved (not overwritten by Jira)."""

    def test_conf_preserved_without_override(self):
        """Without override, Confluence checked value should be kept, not replaced by Jira."""
        from taskautomation.confluence_client import ConfluenceClient

        sync = make_sync()

        unified = [{
            "title": "Test task",
            "match_key": "test task",
            "notion": {"checked": False, "id": "n1"},
            "jira": {"is_done": True, "key": "VC-1", "summary": "Test task", "updated": ""},
            "conf": {"checked": False, "text": "Test task"},
        }]

        # No overrides — Confluence state should be preserved
        overrides = {}

        # Mock Confluence page
        conf_page = {"id": "123", "title": "VC-1 — Test"}
        conf_body = (
            '<h2>План выполнения</h2>\n'
            '<ac:task-list>\n'
            '<ac:task><ac:task-id>1</ac:task-id>'
            '<ac:task-uuid>uuid1</ac:task-uuid>'
            '<ac:task-status>incomplete</ac:task-status>'
            '<ac:task-body><span class="placeholder-inline-tasks">Test task</span>'
            '</ac:task-body></ac:task>\n'
            '</ac:task-list>'
        )

        sync._update_confluence_from_unified(
            "VC-1", conf_page, conf_body, 1, unified, overrides,
        )

        # The item should keep conf's checked=False, NOT jira's is_done=True
        if sync.confluence.update_page.called:
            call_args = sync.confluence.update_page.call_args
            new_body = call_args[0][2]  # third positional arg is body
            assert "incomplete" in new_body, \
                "Without override, Confluence should preserve its own state (incomplete)"
            assert "complete" not in new_body.replace("incomplete", ""), \
                "Jira's is_done=True should NOT override Confluence's checked=False"


class TestExecuteActionsConfluenceGuard:
    """P1: _execute_actions should only rebuild Confluence when conf_overrides is non-empty."""

    def test_jira_only_action_no_confluence_rebuild(self):
        """Actions that only touch Jira/Notion should NOT trigger Confluence rebuild."""
        sync = make_sync()

        actions = [("close_subtask", {"key": "VCSUB-1", "summary": "Task"})]
        conf_page = {"id": "123", "title": "VC-1 — Test"}
        conf_body = "<h2>План выполнения</h2><ac:task-list></ac:task-list>"
        unified = [{"title": "Task", "match_key": "task",
                    "notion": None, "jira": {"is_done": True}, "conf": None}]

        sync._do_create_subtask = MagicMock()
        sync._do_create_todo = MagicMock()
        sync._check_todo = MagicMock()
        sync._uncheck_todo = MagicMock()
        sync._close_subtask = MagicMock()
        sync._reopen_subtask = MagicMock()
        sync._update_confluence_from_unified = MagicMock()

        sync._execute_actions("VC-1", actions, "h1", conf_page, conf_body, 1, unified)

        sync._update_confluence_from_unified.assert_not_called(), \
            "Confluence should NOT be rebuilt for Jira-only actions"

    def test_conf_action_triggers_confluence_rebuild(self):
        """set_conf_item action should trigger Confluence rebuild."""
        sync = make_sync()

        actions = [("set_conf_item", "New task", True)]
        conf_page = {"id": "123", "title": "VC-1 — Test"}
        conf_body = "<h2>План выполнения</h2><ac:task-list></ac:task-list>"
        unified = [{"title": "New task", "match_key": "new task",
                    "notion": None, "jira": None, "conf": None}]

        sync._do_create_subtask = MagicMock()
        sync._do_create_todo = MagicMock()
        sync._update_confluence_from_unified = MagicMock()

        sync._execute_actions("VC-1", actions, "h1", conf_page, conf_body, 1, unified)

        sync._update_confluence_from_unified.assert_called_once()


class TestFindOrCreatePage:
    """P0.3: find_or_create_page should NOT overwrite existing pages."""

    def test_existing_page_returned_without_update(self):
        """When page already exists, return it as-is without calling update_page."""
        from taskautomation.confluence_client import ConfluenceClient

        client = ConfluenceClient.__new__(ConfluenceClient)
        client.base_url = "https://test.atlassian.net/wiki"
        client.space_key = "TEST"
        client.parent_page_id = "123"
        client._auth = ("test@test.com", "token")

        existing_page = {"id": "456", "title": "VC-1 — Test Task"}

        with patch.object(client, 'find_page_by_jira_key', return_value=existing_page), \
             patch.object(client, 'update_page') as mock_update, \
             patch.object(client, 'get_page') as mock_get:

            result = client.find_or_create_page("VC-1", "VC-1 — Test Task", "<p>template</p>")

            assert result == existing_page
            mock_update.assert_not_called(), "update_page must NOT be called for existing pages"
            mock_get.assert_not_called(), "get_page must NOT be called for existing pages"
