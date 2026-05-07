"""Tests for content converter idempotency.

P0.5: Verifies that roundtrip conversions produce stable hashes.
Notion blocks → XHTML → Notion blocks → XHTML should give the same hash.
"""

import pytest
from taskautomation.content_converter import (
    compute_content_hash,
    notion_blocks_to_xhtml,
    xhtml_to_notion_blocks,
)


def roundtrip_xhtml(xhtml: str) -> str:
    """XHTML → Notion blocks → XHTML (one roundtrip)."""
    blocks = xhtml_to_notion_blocks(xhtml)
    return notion_blocks_to_xhtml(blocks)


def roundtrip_notion(blocks: list) -> list:
    """Notion blocks → XHTML → Notion blocks (one roundtrip)."""
    xhtml = notion_blocks_to_xhtml(blocks)
    return xhtml_to_notion_blocks(xhtml)


class TestXHTMLRoundtrip:
    """XHTML → Notion → XHTML should produce same hash."""

    def test_simple_paragraph(self):
        xhtml = "<p>Hello world</p>"
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_bold_italic(self):
        xhtml = "<p><strong>bold</strong> and <em>italic</em></p>"
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_link(self):
        xhtml = '<p><a href="https://example.com">link text</a></p>'
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_bulleted_list(self):
        xhtml = "<ul><li>item one</li><li>item two</li></ul>"
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_numbered_list(self):
        xhtml = "<ol><li>first</li><li>second</li></ol>"
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_code_block(self):
        xhtml = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            '<ac:plain-text-body><![CDATA[print("hello")]]></ac:plain-text-body>'
            '</ac:structured-macro>'
        )
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_blockquote(self):
        xhtml = "<blockquote><p>quoted text</p></blockquote>"
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_mixed_content(self):
        xhtml = (
            "<p>Paragraph one</p>\n"
            "<ul><li>bullet</li></ul>\n"
            "<p><strong>bold text</strong> normal</p>"
        )
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result), \
            f"Roundtrip changed:\n  IN:  {xhtml}\n  OUT: {result}"

    def test_double_roundtrip_stable(self):
        """Two roundtrips should give same result as one (convergence)."""
        xhtml = "<p>Some <strong>bold</strong> text with <em>style</em></p>"
        r1 = roundtrip_xhtml(xhtml)
        r2 = roundtrip_xhtml(r1)
        assert compute_content_hash(r1) == compute_content_hash(r2), \
            f"Double roundtrip not stable:\n  R1: {r1}\n  R2: {r2}"

    def test_empty_content(self):
        xhtml = ""
        result = roundtrip_xhtml(xhtml)
        assert compute_content_hash(xhtml) == compute_content_hash(result)

    def test_whitespace_variations(self):
        """Hash should be stable regardless of whitespace in XHTML."""
        xhtml1 = "<p>Hello world</p>"
        xhtml2 = "<p>Hello  world</p>"
        # These should be normalized to same hash by compute_content_hash
        h1 = compute_content_hash(xhtml1)
        h2 = compute_content_hash(xhtml2)
        assert h1 == h2, "Whitespace normalization should handle extra spaces"


class TestNotionRoundtrip:
    """Notion blocks → XHTML → Notion blocks → XHTML should produce same hash."""

    def test_paragraph_blocks(self):
        blocks = [
            {
                "object": "block", "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": "Hello"}}]
                },
            }
        ]
        xhtml1 = notion_blocks_to_xhtml(blocks)
        blocks2 = xhtml_to_notion_blocks(xhtml1)
        xhtml2 = notion_blocks_to_xhtml(blocks2)
        assert compute_content_hash(xhtml1) == compute_content_hash(xhtml2), \
            f"Notion roundtrip changed:\n  X1: {xhtml1}\n  X2: {xhtml2}"

    def test_todo_blocks(self):
        blocks = [
            {
                "object": "block", "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": "Task one"}}],
                    "checked": True,
                },
            },
            {
                "object": "block", "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": "Task two"}}],
                    "checked": False,
                },
            },
        ]
        xhtml1 = notion_blocks_to_xhtml(blocks)
        blocks2 = xhtml_to_notion_blocks(xhtml1)
        xhtml2 = notion_blocks_to_xhtml(blocks2)
        assert compute_content_hash(xhtml1) == compute_content_hash(xhtml2), \
            f"Todo roundtrip changed:\n  X1: {xhtml1}\n  X2: {xhtml2}"

    def test_annotated_text(self):
        blocks = [
            {
                "object": "block", "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": "bold"},
                            "annotations": {"bold": True},
                        },
                        {
                            "type": "text",
                            "text": {"content": " normal "},
                        },
                        {
                            "type": "text",
                            "text": {"content": "italic"},
                            "annotations": {"italic": True},
                        },
                    ]
                },
            }
        ]
        xhtml1 = notion_blocks_to_xhtml(blocks)
        blocks2 = xhtml_to_notion_blocks(xhtml1)
        xhtml2 = notion_blocks_to_xhtml(blocks2)
        assert compute_content_hash(xhtml1) == compute_content_hash(xhtml2), \
            f"Annotated roundtrip changed:\n  X1: {xhtml1}\n  X2: {xhtml2}"
