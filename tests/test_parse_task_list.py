"""Regression tests for ConfluenceClient.parse_task_list().

Phase 1.2: parse_task_list() previously relied on a single regex with
a fixed child-element order, a mandatory <ac:task-uuid>, and a body
wrapped in <span class="placeholder-inline-tasks">. Real Confluence
storage does NOT guarantee any of those — child elements may appear
in any order, legacy pages can omit the uuid, and bodies may be plain
text or arbitrary inline rich content.

If the parser misses a task, sync sees fewer items than really exist
on the page and may rebuild the task-list, destroying user-entered
content (uuids, ordering, formatting).

These tests use synthetic fixtures from
tests/fixtures/confluence_task_lists/ — no live API calls.
"""

from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "confluence_task_lists"


def load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def parse(html: str):
    from taskautomation.confluence_client import ConfluenceClient
    return ConfluenceClient.parse_task_list(html)


def normalize_text(s: str) -> str:
    """Collapse internal whitespace for stable comparison."""
    return " ".join(s.split())


class TestCanonical:
    """v1 — the format the existing parser was written against."""

    def test_v1_returns_three_tasks(self):
        tasks = parse(load("v1_canonical.xml"))
        assert len(tasks) == 3

    def test_v1_preserves_identity(self):
        tasks = parse(load("v1_canonical.xml"))
        ids = [t.get("task_id") for t in tasks]
        uuids = [t.get("uuid") for t in tasks]
        statuses = [t.get("checked") for t in tasks]
        texts = [normalize_text(t.get("text", "")) for t in tasks]

        assert ids == ["1", "2", "3"]
        assert uuids == ["abc-001", "abc-002", "abc-003"]
        assert statuses == [False, True, False]
        assert texts == ["Design schema", "Write tests", "Deploy to staging"]


class TestWhitespace:
    """v2 — same format, but with newlines/indentation between tags.
    Confluence reformats stored XHTML on edit; whitespace must not
    cause the parser to drop tasks or pull garbage into text.
    """

    def test_v2_returns_two_tasks(self):
        tasks = parse(load("v2_whitespace.xml"))
        assert len(tasks) == 2, (
            f"Expected 2 tasks, got {len(tasks)}. "
            f"Whitespace between child tags broke parsing."
        )

    def test_v2_text_is_clean(self):
        tasks = parse(load("v2_whitespace.xml"))
        texts = [normalize_text(t.get("text", "")) for t in tasks]
        assert texts == ["First item with newlines", "Second item"]

    def test_v2_identity_preserved(self):
        tasks = parse(load("v2_whitespace.xml"))
        assert [t["uuid"] for t in tasks] == ["ws-001", "ws-002"]
        assert [t["checked"] for t in tasks] == [False, True]


class TestRichBody:
    """v3 — body contains inline rich text (<strong>, <a>, entities).
    The parser must recover all tasks, and the text must not silently
    drop the rich content.
    """

    def test_v3_returns_three_tasks(self):
        tasks = parse(load("v3_rich_body.xml"))
        assert len(tasks) == 3, (
            f"Expected 3 tasks, got {len(tasks)}. Rich-body content "
            f"(strong, a, entities) caused the parser to drop tasks."
        )

    def test_v3_strong_text_not_lost(self):
        tasks = parse(load("v3_rich_body.xml"))
        # The text "OAuth2" must appear somewhere in task 1's text
        # — we don't mandate exact markup, but the word must survive.
        assert "OAuth2" in tasks[0].get("text", ""), (
            f"Expected 'OAuth2' in task[0].text, got: {tasks[0].get('text')!r}"
        )

    def test_v3_link_text_not_lost(self):
        tasks = parse(load("v3_rich_body.xml"))
        assert "the spec" in tasks[1].get("text", ""), (
            f"Expected link text 'the spec' in task[1].text, "
            f"got: {tasks[1].get('text')!r}"
        )

    def test_v3_entities_decoded(self):
        tasks = parse(load("v3_rich_body.xml"))
        text = tasks[2].get("text", "")
        # &amp; → &, &lt; → <, &gt; → >, &quot; → "
        assert "&" in text and "<tags>" in text and '"input"' in text, (
            f"HTML entities not decoded: {text!r}"
        )


class TestLegacyNoUuid:
    """v4 — older Confluence pages (or imports) may have <ac:task>
    without <ac:task-uuid>. The parser MUST still surface those tasks
    (with uuid empty/None) instead of silently dropping them, so sync
    doesn't think the page is empty and clobber it.
    """

    def test_v4_returns_two_tasks(self):
        tasks = parse(load("v4_legacy_no_uuid.xml"))
        assert len(tasks) == 2, (
            f"Expected 2 legacy tasks (no uuid), got {len(tasks)}. "
            f"Parser is dropping tasks that lack <ac:task-uuid>."
        )

    def test_v4_uuid_absent_or_empty(self):
        tasks = parse(load("v4_legacy_no_uuid.xml"))
        for t in tasks:
            uid = t.get("uuid")
            assert uid in (None, ""), (
                f"Expected empty uuid for legacy task, got {uid!r}"
            )

    def test_v4_other_fields_preserved(self):
        tasks = parse(load("v4_legacy_no_uuid.xml"))
        assert [t["task_id"] for t in tasks] == ["1", "2"]
        assert [t["checked"] for t in tasks] == [False, True]
        assert [normalize_text(t["text"]) for t in tasks] == [
            "Old task without uuid", "Another legacy task",
        ]


class TestReorderedChildren:
    """v5 — child elements of <ac:task> appear in different orders.
    Confluence storage format does NOT mandate a specific order of
    task-id / task-uuid / task-status / task-body. The parser must
    handle any order.
    """

    def test_v5_returns_three_tasks(self):
        tasks = parse(load("v5_reordered_children.xml"))
        assert len(tasks) == 3, (
            f"Expected 3 tasks, got {len(tasks)}. The parser depends "
            f"on a fixed child-element order, but Confluence does not "
            f"guarantee it."
        )

    def test_v5_identity_preserved_regardless_of_order(self):
        tasks = parse(load("v5_reordered_children.xml"))
        # Sort by task_id to compare deterministically; reordering of
        # children must not change the parsed identity.
        by_id = {t["task_id"]: t for t in tasks}
        assert by_id["1"]["uuid"] == "ord-001"
        assert by_id["1"]["checked"] is False
        assert "Status before id" in by_id["1"]["text"]

        assert by_id["2"]["uuid"] == "ord-002"
        assert by_id["2"]["checked"] is True
        assert "Status before uuid" in by_id["2"]["text"]

        assert by_id["3"]["uuid"] == "ord-003"
        assert by_id["3"]["checked"] is False
        assert "Body first" in by_id["3"]["text"]


class TestBareBody:
    """v6 — body without the <span class="placeholder-inline-tasks">
    wrapper. Confluence sometimes stores plain bodies that way,
    especially for tasks created via the older editor or imported.
    """

    def test_v6_returns_two_tasks(self):
        tasks = parse(load("v6_body_no_span.xml"))
        assert len(tasks) == 2, (
            f"Expected 2 tasks with bare body (no span wrapper), "
            f"got {len(tasks)}. Parser requires the span wrapper."
        )

    def test_v6_text_extracted(self):
        tasks = parse(load("v6_body_no_span.xml"))
        assert "Plain text body" in tasks[0].get("text", "")
        assert "Another bare body" in tasks[1].get("text", "")
