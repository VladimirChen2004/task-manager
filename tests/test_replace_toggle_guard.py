"""Tests for replace_toggle_content idempotency guard.

Phase 1.1: The guard must consider nested children when comparing
existing vs new content. Otherwise the guard either:
  - falsely matches (skips a write that should have happened), losing
    deeply nested user edits, or
  - falsely diverges (rebuilds when content is actually equivalent),
    causing a destructive delete+append cycle and bumping page versions.

These tests use mocks only — no live API calls.
"""

from unittest.mock import patch, MagicMock


def make_notion_client():
    """Create a NotionClient bypassing __init__ network/config."""
    from taskautomation.notion_client import NotionClient
    client = NotionClient.__new__(NotionClient)
    client.api_token = "test-token"
    client.database_id = "test-db"
    client.headers = {
        "Authorization": "Bearer test-token",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    return client


def make_paragraph(text, has_children=False, block_id=None):
    """Build a Notion paragraph block dict similar to API responses."""
    return {
        "id": block_id or f"block-{abs(hash(text)) % (10**8)}",
        "object": "block",
        "type": "paragraph",
        "has_children": has_children,
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": text},
                "plain_text": text,
                "annotations": {
                    "bold": False, "italic": False, "strikethrough": False,
                    "underline": False, "code": False, "color": "default",
                },
            }],
        },
    }


def make_bulleted(text, has_children=False, block_id=None):
    return {
        "id": block_id or f"block-{abs(hash('b'+text)) % (10**8)}",
        "object": "block",
        "type": "bulleted_list_item",
        "has_children": has_children,
        "bulleted_list_item": {
            "rich_text": [{
                "type": "text",
                "text": {"content": text},
                "plain_text": text,
                "annotations": {
                    "bold": False, "italic": False, "strikethrough": False,
                    "underline": False, "code": False, "color": "default",
                },
            }],
        },
    }


class TestNestedGuardFalseMatch:
    """When existing has deep nested children but the guard only compares
    top-level, two semantically different states can hash equal — and
    the guard skips a write that should have happened.
    """

    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "find_toggle_by_text")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "get_block_children")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "delete_block")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "append_children")
    def test_existing_has_nested_grandchildren_new_has_different_nested(
        self, mock_append, mock_delete, mock_get_children, mock_find_toggle
    ):
        """Existing top-level matches new top-level, but existing
        nested grandchildren differ from new nested grandchildren.
        Guard must NOT skip — the contents are semantically different.
        """
        client = make_notion_client()
        mock_find_toggle.return_value = "toggle-id"

        # Existing: top-level paragraph WITH nested bullets
        existing_top = make_paragraph("Step 1", has_children=True,
                                      block_id="b1")
        existing_nested = [make_bulleted("old detail A"),
                           make_bulleted("old detail B")]

        def get_children_side_effect(block_id):
            if block_id == "toggle-id":
                return [existing_top]
            if block_id == "b1":
                return existing_nested
            return []

        mock_get_children.side_effect = get_children_side_effect

        # New: same top-level paragraph but DIFFERENT nested bullets
        new_top = make_paragraph("Step 1", has_children=True, block_id="b1")
        new_top["_children"] = [
            make_bulleted("new detail A"),
            make_bulleted("new detail B"),
        ]

        result = client.replace_toggle_content(
            "page-id", "Heading", [new_top]
        )

        # The guard, if it considered nested children, would NOT skip:
        # there's a real semantic difference. So delete+append must run.
        assert mock_delete.called or mock_append.called, (
            "Guard falsely matched on top-level only: nested grandchildren "
            "differ but the write was skipped. delete_block / append_children "
            "should have been called."
        )
        assert result is True or result is None or result is not False


class TestNestedGuardFalseDiverge:
    """When existing has nested children but is read flat by the guard
    while new_children come with _children attached, the converter
    serialises them differently for list items / toggle headings →
    hashes diverge → destructive rebuild even when content is identical.
    """

    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "find_toggle_by_text")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "get_block_children")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "delete_block")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "append_children")
    def test_existing_bulleted_with_nested_matches_new_bulleted_with_nested(
        self, mock_append, mock_delete, mock_get_children, mock_find_toggle
    ):
        """Existing bulleted item has nested bullets in Notion. New
        comes with _children attached representing the same nesting.
        Guard MUST skip — content is semantically identical.

        Currently fails: existing is read via get_block_children (flat),
        no _children attached. _list_items_to_xhtml omits the nested
        <ul>...</ul> for existing but emits it for new → hashes differ.
        """
        client = make_notion_client()
        mock_find_toggle.return_value = "toggle-id"

        existing_top = make_bulleted("Step 1", has_children=True,
                                     block_id="b1")
        existing_nested = [make_bulleted("nested A"),
                           make_bulleted("nested B")]

        def get_children_side_effect(block_id):
            if block_id == "toggle-id":
                return [existing_top]
            if block_id == "b1":
                return existing_nested
            return []

        mock_get_children.side_effect = get_children_side_effect
        mock_delete.return_value = True
        mock_append.return_value = True

        new_top = make_bulleted("Step 1", has_children=True, block_id="b1")
        new_top["_children"] = [
            make_bulleted("nested A"),
            make_bulleted("nested B"),
        ]

        result = client.replace_toggle_content(
            "page-id", "Heading", [new_top]
        )

        assert not mock_delete.called, (
            "Guard falsely diverged: existing bulleted item was read flat "
            "(no _children), new came with _children attached → converter "
            "produced different XHTML for the same logical content, and "
            "the destructive delete+append cycle ran for nothing."
        )
        assert not mock_append.called
        assert result is True


class TestFlatNoNestedStillWorks:
    """Sanity: when neither side has nested children, guard works."""

    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "find_toggle_by_text")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "get_block_children")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "delete_block")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "append_children")
    def test_flat_identical_skips(
        self, mock_append, mock_delete, mock_get_children, mock_find_toggle
    ):
        client = make_notion_client()
        mock_find_toggle.return_value = "toggle-id"
        mock_get_children.return_value = [
            make_paragraph("Hello", block_id="b1"),
            make_paragraph("World", block_id="b2"),
        ]

        new_children = [
            make_paragraph("Hello", block_id="b1"),
            make_paragraph("World", block_id="b2"),
        ]
        result = client.replace_toggle_content(
            "page-id", "Heading", new_children
        )

        assert result is True
        assert not mock_delete.called
        assert not mock_append.called

    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "find_toggle_by_text")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "get_block_children")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "delete_block")
    @patch.object(__import__("taskautomation.notion_client",
                             fromlist=["NotionClient"]).NotionClient,
                  "append_children")
    def test_flat_different_rebuilds(
        self, mock_append, mock_delete, mock_get_children, mock_find_toggle
    ):
        client = make_notion_client()
        mock_find_toggle.return_value = "toggle-id"
        mock_get_children.return_value = [
            make_paragraph("Old", block_id="b1"),
        ]
        mock_delete.return_value = True
        mock_append.return_value = True

        new_children = [make_paragraph("New", block_id="b2")]
        result = client.replace_toggle_content(
            "page-id", "Heading", new_children
        )

        # Different content — must delete and append
        assert mock_delete.called
        assert mock_append.called
        assert result is True
