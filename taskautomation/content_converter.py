"""Bidirectional converter: Notion blocks <-> Confluence XHTML."""

import hashlib
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional


def compute_content_hash(content: str) -> str:
    """Compute a short hash for change detection (whitespace-normalized)."""
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Notion → Confluence XHTML
# ---------------------------------------------------------------------------

def _rt_to_xhtml(rich_text: List[Dict[str, Any]]) -> str:
    """Convert Notion rich_text array to inline XHTML."""
    parts: List[str] = []
    for rt in rich_text:
        text = rt.get("text", {}).get("content", "") if rt.get("type") == "text" else ""
        text = _esc(text)
        ann = rt.get("annotations", {})
        link = rt.get("text", {}).get("link")
        if ann.get("code"):
            text = f"<code>{text}</code>"
        if ann.get("bold"):
            text = f"<strong>{text}</strong>"
        if ann.get("italic"):
            text = f"<em>{text}</em>"
        if ann.get("strikethrough"):
            text = f"<s>{text}</s>"
        if link and link.get("url"):
            text = f'<a href="{_esc(link["url"])}">{text}</a>'
        parts.append(text)
    return "".join(parts)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def notion_blocks_to_xhtml(blocks: List[Dict[str, Any]]) -> str:
    """Convert a list of Notion blocks to Confluence XHTML."""
    return _blocks_to_xhtml(blocks)


def _blocks_to_xhtml(blocks: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        btype = block.get("type", "")

        if btype == "paragraph":
            inner = _rt_to_xhtml(block.get("paragraph", {}).get("rich_text", []))
            parts.append(f"<p>{inner}</p>")

        elif btype == "bulleted_list_item":
            # Collect consecutive bulleted items
            items, i = _collect_list(blocks, i, "bulleted_list_item")
            parts.append(_list_items_to_xhtml(items, "ul", "bulleted_list_item"))
            continue

        elif btype == "numbered_list_item":
            items, i = _collect_list(blocks, i, "numbered_list_item")
            parts.append(_list_items_to_xhtml(items, "ol", "numbered_list_item"))
            continue

        elif btype == "to_do":
            # Collect consecutive to_do items
            items, i = _collect_list(blocks, i, "to_do")
            parts.append(_todo_items_to_xhtml(items))
            continue

        elif btype == "code":
            code_data = block.get("code", {})
            lang = code_data.get("language", "")
            text = _plain_text(code_data.get("rich_text", []))
            parts.append(
                f'<ac:structured-macro ac:name="code">'
                f'<ac:parameter ac:name="language">{_esc(lang)}</ac:parameter>'
                f'<ac:plain-text-body><![CDATA[{text}]]></ac:plain-text-body>'
                f'</ac:structured-macro>'
            )

        elif btype == "table":
            parts.append(_table_to_xhtml(block))

        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = btype[-1]
            hdata = block.get(btype, {})
            inner = _rt_to_xhtml(hdata.get("rich_text", []))
            if hdata.get("is_toggleable"):
                # Toggle → Confluence expand macro
                children_html = ""
                if block.get("has_children") and block.get("_children"):
                    children_html = _blocks_to_xhtml(block["_children"])
                parts.append(
                    f'<ac:structured-macro ac:name="expand">'
                    f'<ac:parameter ac:name="title">{inner}</ac:parameter>'
                    f'<ac:rich-text-body>{children_html}</ac:rich-text-body>'
                    f'</ac:structured-macro>'
                )
            else:
                parts.append(f"<h{level}>{inner}</h{level}>")

        elif btype == "divider":
            parts.append("<hr/>")

        elif btype == "callout":
            inner = _rt_to_xhtml(block.get("callout", {}).get("rich_text", []))
            parts.append(
                f'<ac:structured-macro ac:name="info">'
                f'<ac:rich-text-body><p>{inner}</p></ac:rich-text-body>'
                f'</ac:structured-macro>'
            )

        elif btype == "quote":
            inner = _rt_to_xhtml(block.get("quote", {}).get("rich_text", []))
            parts.append(f"<blockquote><p>{inner}</p></blockquote>")

        i += 1
    return "\n".join(parts)


def _collect_list(
    blocks: List[Dict], start: int, btype: str
) -> tuple:
    """Collect consecutive blocks of same type. Returns (items, next_index)."""
    items = []
    i = start
    while i < len(blocks) and blocks[i].get("type") == btype:
        items.append(blocks[i])
        i += 1
    return items, i


def _list_items_to_xhtml(items: List[Dict], tag: str, btype: str) -> str:
    parts = [f"<{tag}>"]
    for item in items:
        inner = _rt_to_xhtml(item.get(btype, {}).get("rich_text", []))
        children_html = ""
        if item.get("has_children") and item.get("_children"):
            children_html = _blocks_to_xhtml(item["_children"])
        parts.append(f"<li>{inner}{children_html}</li>")
    parts.append(f"</{tag}>")
    return "".join(parts)


def _todo_items_to_xhtml(items: List[Dict]) -> str:
    parts = ["<ac:task-list>"]
    for item in items:
        td = item.get("to_do", {})
        status = "complete" if td.get("checked") else "incomplete"
        body = _rt_to_xhtml(td.get("rich_text", []))
        parts.append(
            f"<ac:task><ac:task-status>{status}</ac:task-status>"
            f"<ac:task-body>{body}</ac:task-body></ac:task>"
        )
    parts.append("</ac:task-list>")
    return "".join(parts)


def _table_to_xhtml(block: Dict) -> str:
    table = block.get("table", {})
    rows_data = block.get("_children", [])
    if not rows_data:
        return ""
    parts = ["<table>"]
    for idx, row_block in enumerate(rows_data):
        row = row_block.get("table_row", {})
        cells = row.get("cells", [])
        tag = "th" if idx == 0 and table.get("has_column_header") else "td"
        parts.append("<tr>")
        for cell in cells:
            parts.append(f"<{tag}>{_rt_to_xhtml(cell)}</{tag}>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _plain_text(rich_text: List[Dict]) -> str:
    return "".join(rt.get("plain_text", rt.get("text", {}).get("content", "")) for rt in rich_text)


# ---------------------------------------------------------------------------
# Confluence XHTML → Notion blocks
# ---------------------------------------------------------------------------

class _XHTMLToNotionParser(HTMLParser):
    """Parse Confluence XHTML section content into Notion blocks."""

    def __init__(self):
        super().__init__()
        self.blocks: List[Dict[str, Any]] = []
        self._stack: List[str] = []  # tag stack
        self._rt: List[Dict[str, Any]] = []  # current rich_text accumulator
        self._list_stack: List[str] = []  # ul/ol nesting
        self._ann: Dict[str, bool] = {}  # current annotations
        self._link_url: Optional[str] = None
        self._in_code_macro = False
        self._code_lang = ""
        self._code_body = ""
        self._in_task_list = False
        self._task_status = ""
        self._in_task_body = False
        self._macro_stack: List[str] = []  # nested macros
        self._param_name = ""
        self._in_expand = False
        self._expand_title = ""
        self._expand_body_parts: List[str] = []
        # Table
        self._in_table = False
        self._table_rows: List[List[List[Dict]]] = []
        self._current_row: List[List[Dict]] = []
        self._in_cell = False
        self._has_header = False
        self._header_row_idx = 0

    def handle_starttag(self, tag: str, attrs: List[tuple]):
        attr_dict = dict(attrs)
        ltag = tag.lower()
        self._stack.append(ltag)

        if ltag in ("strong", "b"):
            self._ann["bold"] = True
        elif ltag in ("em", "i"):
            self._ann["italic"] = True
        elif ltag == "s":
            self._ann["strikethrough"] = True
        elif ltag == "code" and not self._in_code_macro:
            self._ann["code"] = True
        elif ltag == "a":
            self._link_url = attr_dict.get("href")
        elif ltag == "p":
            self._rt = []
        elif ltag == "li":
            self._rt = []
        elif ltag in ("ul", "ol"):
            self._list_stack.append(ltag)
        elif ltag == "table":
            self._in_table = True
            self._table_rows = []
        elif ltag == "tr":
            self._current_row = []
        elif ltag in ("td", "th"):
            self._in_cell = True
            self._rt = []
            if ltag == "th":
                self._has_header = True
        elif ltag == "blockquote":
            self._rt = []

        # Confluence macros (ac:*)
        if ltag == "ac:structured-macro":
            macro_name = attr_dict.get("ac:name", "")
            self._macro_stack.append(macro_name)
            if macro_name == "code":
                self._in_code_macro = True
                self._code_lang = ""
                self._code_body = ""
            elif macro_name == "expand":
                self._in_expand = True
                self._expand_title = ""
        elif ltag == "ac:parameter":
            self._param_name = attr_dict.get("ac:name", "")
        elif ltag == "ac:task-list":
            self._in_task_list = True
        elif ltag == "ac:task":
            self._task_status = ""
            self._rt = []
        elif ltag == "ac:task-status":
            pass
        elif ltag == "ac:task-body":
            self._in_task_body = True
            self._rt = []

    def handle_endtag(self, tag: str):
        ltag = tag.lower()
        if self._stack and self._stack[-1] == ltag:
            self._stack.pop()

        if ltag in ("strong", "b"):
            self._ann.pop("bold", None)
        elif ltag in ("em", "i"):
            self._ann.pop("italic", None)
        elif ltag == "s":
            self._ann.pop("strikethrough", None)
        elif ltag == "code" and not self._in_code_macro:
            self._ann.pop("code", None)
        elif ltag == "a":
            self._link_url = None
        elif ltag == "p":
            if self._in_cell:
                pass  # handled at cell end
            elif self._rt:
                self.blocks.append(_make_paragraph(self._rt))
                self._rt = []
        elif ltag == "li":
            if self._rt:
                list_type = self._list_stack[-1] if self._list_stack else "ul"
                if list_type == "ol":
                    self.blocks.append(_make_numbered_item(self._rt))
                else:
                    self.blocks.append(_make_bulleted_item(self._rt))
                self._rt = []
        elif ltag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
        elif ltag == "blockquote":
            if self._rt:
                self.blocks.append(_make_quote(self._rt))
                self._rt = []
        elif ltag in ("td", "th"):
            self._in_cell = False
            self._current_row.append(self._rt[:])
            self._rt = []
        elif ltag == "tr":
            self._table_rows.append(self._current_row)
            self._current_row = []
        elif ltag == "table":
            self._in_table = False
            if self._table_rows:
                self.blocks.append(_make_table(self._table_rows, self._has_header))
            self._table_rows = []
            self._has_header = False

        # Macros
        if ltag == "ac:structured-macro":
            if self._macro_stack:
                macro_name = self._macro_stack.pop()
                if macro_name == "code":
                    self._in_code_macro = False
                    self.blocks.append(_make_code(self._code_body, self._code_lang))
                elif macro_name == "expand":
                    self._in_expand = False
                    # Parse expand body recursively
                    inner_blocks = xhtml_to_notion_blocks(
                        "".join(self._expand_body_parts)
                    )
                    self.blocks.append(
                        _make_toggle_heading(self._expand_title, inner_blocks)
                    )
                    self._expand_body_parts = []
        elif ltag == "ac:task-list":
            self._in_task_list = False
        elif ltag == "ac:task-status":
            pass  # text was captured in handle_data
        elif ltag == "ac:task-body":
            self._in_task_body = False
        elif ltag == "ac:task":
            if self._in_task_list:
                checked = self._task_status.strip().lower() == "complete"
                self.blocks.append(_make_todo(self._rt, checked))
                self._rt = []

    def handle_data(self, data: str):
        # ac:parameter content
        if self._stack and self._stack[-1] == "ac:parameter":
            if self._in_code_macro and self._param_name == "language":
                self._code_lang = data
            elif self._in_expand and self._param_name == "title":
                self._expand_title = data
            return

        # Code body (CDATA)
        if self._in_code_macro and "ac:plain-text-body" in self._stack:
            self._code_body += data
            return

        # Expand body
        if self._in_expand and "ac:rich-text-body" in self._stack:
            self._expand_body_parts.append(data)
            return

        # Task status
        if "ac:task-status" in self._stack:
            self._task_status += data
            return

        # Normal text
        if not data.strip():
            return

        rt_item: Dict[str, Any] = {
            "type": "text",
            "text": {"content": data},
        }
        ann = {}
        if self._ann.get("bold"):
            ann["bold"] = True
        if self._ann.get("italic"):
            ann["italic"] = True
        if self._ann.get("strikethrough"):
            ann["strikethrough"] = True
        if self._ann.get("code"):
            ann["code"] = True
        if ann:
            rt_item["annotations"] = ann
        if self._link_url:
            rt_item["text"]["link"] = {"url": self._link_url}
        self._rt.append(rt_item)


def xhtml_to_notion_blocks(xhtml: str) -> List[Dict[str, Any]]:
    """Parse Confluence XHTML section content into Notion blocks."""
    if not xhtml or not xhtml.strip():
        return []
    parser = _XHTMLToNotionParser()
    parser.feed(xhtml)
    return parser.blocks


# ---- Notion block builders ----

def _make_paragraph(rt: List[Dict]) -> Dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt}}


def _make_bulleted_item(rt: List[Dict]) -> Dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt}}


def _make_numbered_item(rt: List[Dict]) -> Dict:
    return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": rt}}


def _make_todo(rt: List[Dict], checked: bool) -> Dict:
    return {"object": "block", "type": "to_do", "to_do": {"rich_text": rt, "checked": checked}}


def _make_quote(rt: List[Dict]) -> Dict:
    return {"object": "block", "type": "quote", "quote": {"rich_text": rt}}


def _make_code(text: str, language: str) -> Dict:
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "language": language or "plain text",
        },
    }


def _make_toggle_heading(title: str, children: List[Dict]) -> Dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": title}}],
            "is_toggleable": True,
            "children": children if children else [],
        },
    }


def _make_table(rows: List[List[List[Dict]]], has_header: bool) -> Dict:
    width = max(len(row) for row in rows) if rows else 0
    table_rows = []
    for row in rows:
        # Pad cells to width
        cells = [cell for cell in row]
        while len(cells) < width:
            cells.append([])
        table_rows.append({
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": cells},
        })
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": has_header,
            "has_row_header": False,
            "children": table_rows,
        },
    }
