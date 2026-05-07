"""Notion API client for task sync and page content updates."""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import NotionConfig, NOTION_DATABASE_ID, get_progress_emoji

log = logging.getLogger("taskautomation.notion")


class NotionClient:
    """Notion API client for sync and content updates."""

    API_URL = "https://api.notion.com/v1"
    VERSION = "2022-06-28"

    def __init__(
        self,
        api_token: Optional[str] = None,
        database_id: Optional[str] = None,
    ):
        cfg = NotionConfig()
        self.api_token = api_token or cfg.api_token
        self.database_id = database_id or cfg.database_id
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Notion-Version": self.VERSION,
            "Content-Type": "application/json",
        }

    # ---- HTTP helper with retry + backoff ----

    _RETRYABLE_STATUS = {429, 502, 503, 504}
    _MAX_RETRIES = 4
    _SAFE_METHODS = {"get", "head", "options"}

    def _request(self, method: str, url: str,
                 idempotent: Optional[bool] = None,
                 **kwargs) -> requests.Response:
        """HTTP request with retry on transient errors.

        Retry policy:
          - 429 (Too Many Requests) — always retried (server explicitly
            said the request was NOT processed). Respects Retry-After.
          - 502/503/504, Timeout, ConnectionError — retried ONLY when
            the call is idempotent (outcome of a retry is safe).
          - Any other status — returned as-is (no retry).

        ``idempotent`` defaults:
          - True for GET/HEAD/OPTIONS (safe by HTTP semantics).
          - False for POST/PATCH/PUT/DELETE — caller must opt in with
            ``idempotent=True`` only when they know the endpoint is safe
            to repeat (e.g. PATCH a known property by id, DELETE a known
            block by id). For CREATE / append endpoints leave default.

        On exhausted retries returns the last response (or re-raises the
        last network exception when no response was ever obtained).
        """
        import random
        if idempotent is None:
            idempotent = method.lower() in self._SAFE_METHODS
        kwargs.setdefault("timeout", 30)
        last_exc = None
        resp = None

        for attempt in range(self._MAX_RETRIES):
            try:
                resp = getattr(requests, method)(
                    url, headers=self.headers, **kwargs
                )
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                if not idempotent:
                    # Outcome unknown — server may have processed it.
                    # Re-raise so caller can probe state and reconcile.
                    raise
                last_exc = e
                delay = min(2 ** attempt + random.uniform(0, 1), 30)
                log.warning(
                    "Request %s %s failed (%s), retry %d/%d in %.1fs",
                    method.upper(), url.split("?")[0].split("/")[-1],
                    type(e).__name__, attempt + 1, self._MAX_RETRIES, delay,
                )
                time.sleep(delay)
                continue

            if resp.status_code not in self._RETRYABLE_STATUS:
                return resp

            # For non-idempotent calls, only 429 is safe to retry.
            if not idempotent and resp.status_code != 429:
                return resp

            if resp.status_code == 429:
                delay = float(resp.headers.get("Retry-After", 2 ** attempt))
            else:
                delay = min(2 ** attempt + random.uniform(0, 1), 30)
            log.warning(
                "Request %s %s returned %d, retry %d/%d in %.1fs",
                method.upper(), url.split("?")[0].split("/")[-1],
                resp.status_code, attempt + 1, self._MAX_RETRIES, delay,
            )
            time.sleep(delay)

        if last_exc is not None:
            raise last_exc
        return resp

    # ---- Page Queries ----

    def find_page_by_jira_key(self, jira_key: str) -> Optional[Dict[str, Any]]:
        """Find a Notion page by its 'Jira Key' property."""
        url = f"{self.API_URL}/databases/{self.database_id}/query"
        payload = {
            "filter": {
                "property": "Jira Key",
                "rich_text": {"equals": jira_key},
            },
            "page_size": 1,
        }

        # Database query is a POST-as-GET — idempotent.
        resp = self._request("post", url, json=payload, idempotent=True)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def query_all_pages_with_jira_key(self) -> List[Dict[str, Any]]:
        """Get all pages that have a non-empty Jira Key (with pagination)."""
        url = f"{self.API_URL}/databases/{self.database_id}/query"
        all_pages = []
        has_more = True
        start_cursor = None

        while has_more:
            payload = {
                "filter": {
                    "property": "Jira Key",
                    "rich_text": {"is_not_empty": True},
                },
                "page_size": 100,
            }
            if start_cursor:
                payload["start_cursor"] = start_cursor

            # Database query is a POST-as-GET — idempotent.
            resp = self._request("post", url, json=payload, idempotent=True)
            resp.raise_for_status()
            data = resp.json()
            all_pages.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
            time.sleep(0.4)

        return all_pages

    def get_page(self, page_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single page (metadata + properties).

        GET — idempotent and retried automatically by ``_request``.
        Returns the raw Notion page dict, or ``None`` on hard failure
        (404, auth lost). Caller is expected to be tolerant of None.
        """
        url = f"{self.API_URL}/pages/{page_id}"
        try:
            resp = self._request("get", url)
        except Exception as e:
            log.warning("get_page %s failed: %s", page_id, e)
            return None
        if resp.status_code == 200:
            return resp.json()
        log.warning(
            "get_page %s returned %d", page_id, resp.status_code,
        )
        return None

    def get_page_status(self, page: Dict[str, Any]) -> Optional[str]:
        """Extract current Status from a Notion page dict."""
        status_prop = page.get("properties", {}).get("Status", {})
        status_obj = status_prop.get("status")
        if status_obj:
            return status_obj.get("name")
        return None

    @staticmethod
    def get_jira_key(page: Dict[str, Any]) -> Optional[str]:
        """Extract Jira Key from a Notion page dict."""
        jira_prop = page.get("properties", {}).get("Jira Key", {})
        rich_text = jira_prop.get("rich_text", [])
        if rich_text:
            return rich_text[0].get("plain_text", "").strip()
        return None

    @staticmethod
    def get_page_title(page: Dict[str, Any]) -> Optional[str]:
        """Extract Task name (title) from page properties."""
        title_prop = page.get("properties", {}).get("Task name", {})
        title_arr = title_prop.get("title", [])
        if title_arr:
            return "".join(t.get("plain_text", "") for t in title_arr).strip()
        return None

    @staticmethod
    def get_page_summary(page: Dict[str, Any]) -> Optional[str]:
        """Extract Summary from page properties."""
        prop = page.get("properties", {}).get("Summary", {})
        rt = prop.get("rich_text", [])
        if rt:
            return "".join(t.get("plain_text", "") for t in rt).strip()
        return None

    @staticmethod
    def get_page_priority(page: Dict[str, Any]) -> Optional[str]:
        """Extract Priority from page properties."""
        prop = page.get("properties", {}).get("Priority", {})
        sel = prop.get("select")
        if sel:
            return sel.get("name")
        return None

    def query_pages_without_jira_key(self) -> List[Dict[str, Any]]:
        """Get all pages that have empty Jira Key (new tasks needing Jira issue)."""
        url = f"{self.API_URL}/databases/{self.database_id}/query"
        all_pages = []
        has_more = True
        start_cursor = None

        while has_more:
            payload = {
                "filter": {
                    "property": "Jira Key",
                    "rich_text": {"is_empty": True},
                },
                "page_size": 100,
            }
            if start_cursor:
                payload["start_cursor"] = start_cursor

            # Database query is a POST-as-GET — idempotent.
            resp = self._request("post", url, json=payload, idempotent=True)
            resp.raise_for_status()
            data = resp.json()
            all_pages.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
            time.sleep(0.4)

        return all_pages

    # ---- Page Property Updates ----

    def update_page_status(self, page_id: str, status_name: str) -> bool:
        """Update the Status property of a Notion page."""
        return self.update_page_properties(
            page_id, {"Status": {"status": {"name": status_name}}}
        )

    def update_page_properties(self, page_id: str, properties: Dict[str, Any]) -> bool:
        """Update arbitrary properties of a Notion page."""
        url = f"{self.API_URL}/pages/{page_id}"
        payload = {"properties": properties}

        # PATCH page-by-id with absolute property values — idempotent.
        resp = self._request("patch", url, json=payload, idempotent=True)
        if resp.status_code == 200:
            return True
        log.error(
            "Failed to update page %s: %s %s",
            page_id,
            resp.status_code,
            resp.text,
        )
        return False

    def update_page_jira_key(self, page_id: str, jira_key: str) -> bool:
        """Set the 'Jira Key' property on a Notion page."""
        url = f"{self.API_URL}/pages/{page_id}"
        payload = {
            "properties": {
                "Jira Key": {
                    "rich_text": [
                        {"type": "text", "text": {"content": jira_key}}
                    ]
                }
            }
        }
        # PATCH page-by-id — idempotent.
        resp = self._request("patch", url, json=payload, idempotent=True)
        if resp.status_code == 200:
            return True
        log.error(
            "Failed to update Jira Key on %s: %s %s",
            page_id, resp.status_code, resp.text,
        )
        return False

    def create_page(
        self,
        title: str,
        status: str = "Not started",
        summary: str = "",
        jira_key: str = "",
        priority: str = "",
        jira_url: str = "",
        children: Optional[List[Dict]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new page in the Tasks database."""
        url = f"{self.API_URL}/pages"
        properties: Dict[str, Any] = {
            "Task name": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "Status": {"status": {"name": status}},
        }
        if summary:
            properties["Summary"] = {
                "rich_text": [
                    {"type": "text", "text": {"content": summary[:2000]}}
                ]
            }
        if jira_key:
            properties["Jira Key"] = {
                "rich_text": [
                    {"type": "text", "text": {"content": jira_key}}
                ]
            }
        if priority:
            properties["Priority"] = {"select": {"name": priority}}

        payload: Dict[str, Any] = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        if children:
            payload["children"] = children

        # CREATE endpoint — not idempotent, retry would risk duplicates.
        resp = self._request("post", url, json=payload, idempotent=False)
        if resp.status_code == 200:
            return resp.json()
        log.error(
            "Failed to create page '%s': %s %s",
            title, resp.status_code, resp.text,
        )
        return None

    # ---- Block API (for progress sync) ----

    def get_block_children(self, block_id: str) -> List[Dict[str, Any]]:
        """Get child blocks of a block/page."""
        url = f"{self.API_URL}/blocks/{block_id}/children"
        all_blocks = []
        has_more = True
        start_cursor = None

        while has_more:
            params = {"page_size": 100}
            if start_cursor:
                params["start_cursor"] = start_cursor

            resp = self._request("get", url, params=params)
            resp.raise_for_status()
            data = resp.json()
            all_blocks.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        return all_blocks

    def find_progress_block(
        self, page_id: str
    ) -> Optional[Tuple[str, str]]:
        """Find the block containing 'Прогресс:' text inside a 🎯 callout.

        Returns: (block_id, current_plain_text) or None.
        """
        top_blocks = self.get_block_children(page_id)

        # Find callout blocks (progress lives inside a callout with 🎯 icon)
        for block in top_blocks:
            # Check column_list → columns → callout (the template uses <columns>)
            if block.get("type") == "column_list":
                columns = self.get_block_children(block["id"])
                for col in columns:
                    col_children = self.get_block_children(col["id"])
                    result = self._find_progress_in_blocks(col_children)
                    if result:
                        return result

            # Also check direct callouts at top level
            if block.get("type") == "callout":
                result = self._find_progress_in_callout(block)
                if result:
                    return result

        return None

    def _find_progress_in_blocks(
        self, blocks: List[Dict[str, Any]]
    ) -> Optional[Tuple[str, str]]:
        """Search for progress line in a list of blocks."""
        for block in blocks:
            if block.get("type") == "callout":
                result = self._find_progress_in_callout(block)
                if result:
                    return result
        return None

    def _find_progress_in_callout(
        self, callout_block: Dict[str, Any]
    ) -> Optional[Tuple[str, str]]:
        """Search for progress line inside a callout's children."""
        # Check callout's own rich_text first
        callout_data = callout_block.get("callout", {})
        rich_text = callout_data.get("rich_text", [])
        plain = "".join(rt.get("plain_text", "") for rt in rich_text)
        if "Прогресс:" in plain:
            return (callout_block["id"], plain)

        # Check children of the callout
        if callout_block.get("has_children"):
            children = self.get_block_children(callout_block["id"])
            for child in children:
                child_type = child.get("type", "")
                type_data = child.get(child_type, {})
                child_rt = type_data.get("rich_text", [])
                child_plain = "".join(
                    rt.get("plain_text", "") for rt in child_rt
                )
                if "Прогресс:" in child_plain:
                    return (child["id"], child_plain)

        return None

    def update_block_text(
        self, block_id: str, block_type: str, rich_text: List[Dict]
    ) -> bool:
        """Update rich_text of a block via PATCH /v1/blocks/{block_id}."""
        url = f"{self.API_URL}/blocks/{block_id}"
        payload = {block_type: {"rich_text": rich_text}}

        # PATCH block-by-id with absolute rich_text — idempotent.
        resp = self._request("patch", url, json=payload, idempotent=True)
        if resp.status_code == 200:
            return True
        log.error(
            "Failed to update block %s: %s %s",
            block_id,
            resp.status_code,
            resp.text,
        )
        return False

    def find_toggle_by_text(
        self, page_id: str, heading_text: str
    ) -> Optional[str]:
        """Find a toggle heading block by its text.

        Returns block_id or None.
        """
        blocks = self.get_block_children(page_id)
        for block in blocks:
            block_type = block.get("type", "")
            if "heading" in block_type:
                type_data = block.get(block_type, {})
                rt = type_data.get("rich_text", [])
                plain = "".join(r.get("plain_text", "") for r in rt)
                if heading_text.lower() in plain.lower():
                    return block["id"]
        return None

    def append_children(
        self, block_id: str, children: List[Dict[str, Any]]
    ) -> bool:
        """Append child blocks to a parent block.

        WARNING: append is NOT idempotent — retrying after a timeout/5xx
        could duplicate the appended blocks. We rely on the default
        retry policy (POST/PATCH default ``idempotent=False``) to skip
        retries on 5xx and network errors.
        """
        url = f"{self.API_URL}/blocks/{block_id}/children"
        payload = {"children": children}

        # Default policy: PATCH is non-idempotent. Network/5xx errors
        # are NOT retried to avoid duplicate blocks.
        resp = self._request("patch", url, json=payload)
        if resp.status_code == 200:
            return True
        log.error(
            "Failed to append children to %s: %s %s",
            block_id,
            resp.status_code,
            resp.text,
        )
        return False

    def delete_block(self, block_id: str) -> bool:
        """Delete a block."""
        url = f"{self.API_URL}/blocks/{block_id}"
        # DELETE-by-id is idempotent (deleting an already-deleted block returns 404,
        # but the desired end state is reached either way).
        resp = self._request("delete", url, idempotent=True)
        return resp.status_code == 200

    # ---- Section content API ----

    def get_toggle_content(
        self, page_id: str, heading_text: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Get child blocks of a toggle heading (recursively fetching nested children).

        Returns list of raw Notion blocks, or None if toggle not found.
        """
        toggle_id = self.find_toggle_by_text(page_id, heading_text)
        if not toggle_id:
            return None
        children = self.get_block_children(toggle_id)
        # Recursively fetch nested children and attach as _children
        for child in children:
            if child.get("has_children"):
                child["_children"] = self._fetch_children_recursive(child["id"])
        return children

    def _fetch_children_recursive(
        self, block_id: str, depth: int = 0,
    ) -> List[Dict[str, Any]]:
        """Recursively fetch children up to depth 3."""
        if depth > 3:
            return []
        children = self.get_block_children(block_id)
        for child in children:
            if child.get("has_children"):
                child["_children"] = self._fetch_children_recursive(
                    child["id"], depth + 1
                )
        return children

    @staticmethod
    def _block_tree_fingerprint(blocks: List[Dict[str, Any]]) -> str:
        """Structural fingerprint of a list of Notion blocks.

        Walks the full tree (block + ``_children`` recursively) so that
        differences buried in nested grandchildren are reflected in the
        hash. Independent of the XHTML converter — needed because the
        converter doesn't serialise ``_children`` for every block type
        (e.g. ``paragraph`` ignores them), which would let the guard
        either falsely match or falsely diverge.

        The fingerprint is stable for semantically identical content:
        type + checked-state + plain text + nested fingerprint, joined
        in order, hashed with sha256.
        """
        import hashlib

        def _normalise(rt_list):
            # Concatenate plain_text or text.content; ignore ids/timestamps.
            parts = []
            for rt in rt_list or []:
                txt = rt.get("plain_text")
                if txt is None:
                    txt = rt.get("text", {}).get("content", "")
                parts.append(txt)
            return "".join(parts)

        def _walk(block_list):
            sig_parts = []
            for b in block_list or []:
                btype = b.get("type", "")
                bdata = b.get(btype, {}) if btype else {}
                text = _normalise(bdata.get("rich_text", []))
                checked = bdata.get("checked", "")
                children_sig = ""
                if b.get("has_children") and b.get("_children"):
                    children_sig = _walk(b["_children"])
                sig_parts.append(f"{btype}|{checked}|{text}|[{children_sig}]")
            return "\x1f".join(sig_parts)

        digest = hashlib.sha256(_walk(blocks).encode("utf-8")).hexdigest()
        return digest[:16]

    def replace_toggle_content(
        self, page_id: str, heading_text: str,
        new_children: List[Dict[str, Any]],
    ) -> bool:
        """Replace all children of a toggle heading with new blocks.

        1. Finds toggle by heading_text
        2. Builds a structural fingerprint of existing (with nested
           grandchildren fetched) and new blocks; skips the write if
           they match (idempotency guard)
        3. Deletes all existing children
        4. Appends new_children

        The fingerprint walks ``_children`` recursively so that real
        differences in nested content trigger a rebuild, and equivalent
        content with the same nesting is correctly skipped — even for
        block types whose XHTML converter ignores nested children.
        """
        toggle_id = self.find_toggle_by_text(page_id, heading_text)
        if not toggle_id:
            log.warning("Toggle '%s' not found on page %s", heading_text, page_id)
            return False

        # Read existing children with nested grandchildren attached so
        # the fingerprint sees the same shape as new_children may have.
        existing = self.get_block_children(toggle_id)
        for child in existing:
            if child.get("has_children"):
                try:
                    child["_children"] = self._fetch_children_recursive(
                        child["id"]
                    )
                except Exception as e:
                    # Don't fail the whole operation; just skip the guard.
                    log.debug(
                        "Failed to fetch nested children for %s: %s",
                        child.get("id"), e,
                    )

        # Idempotency guard: compare structural fingerprints. We use a
        # tree-walking fingerprint (not the XHTML converter) because the
        # converter ignores nested children for some block types and
        # would yield false matches/diverges for nested content.
        try:
            existing_fp = self._block_tree_fingerprint(existing)
            new_fp = self._block_tree_fingerprint(new_children)
            if existing_fp == new_fp:
                log.debug(
                    "Toggle '%s' content unchanged (fp=%s), skipping write",
                    heading_text, existing_fp,
                )
                return True
        except Exception as e:
            log.debug("Fingerprint comparison failed, proceeding with write: %s", e)

        # Delete existing children
        for child in existing:
            self.delete_block(child["id"])

        # Append new children
        if new_children:
            return self.append_children(toggle_id, new_children)
        return True

    # ---- To-do / Plan section API ----

    def get_todo_children(
        self, block_id: str
    ) -> List[Dict[str, Any]]:
        """Get to_do blocks that are children of a toggle heading.

        Returns: [{"id": str, "text": str, "checked": bool}, ...]
        """
        children = self.get_block_children(block_id)
        todos = []
        for child in children:
            if child.get("type") != "to_do":
                continue
            td = child.get("to_do", {})
            rt = td.get("rich_text", [])
            text = "".join(r.get("plain_text", "") for r in rt).strip()
            if text:
                todos.append({
                    "id": child["id"],
                    "text": text,
                    "checked": td.get("checked", False),
                })
        return todos

    def update_todo_checked(self, block_id: str, checked: bool) -> bool:
        """Update the checked state of a to_do block."""
        url = f"{self.API_URL}/blocks/{block_id}"
        payload = {"to_do": {"checked": checked}}
        # PATCH block-by-id with absolute checked value — idempotent.
        resp = self._request("patch", url, json=payload, idempotent=True)
        if resp.status_code == 200:
            return True
        log.error(
            "Failed to update to_do %s: %s %s",
            block_id, resp.status_code, resp.text,
        )
        return False

    def create_todo_block(
        self, parent_id: str, text: str, checked: bool = False
    ) -> Optional[str]:
        """Create a to_do block inside a parent (toggle heading).

        Returns: block_id of created block, or None.

        WARNING: append children is NOT idempotent. Default retry policy
        (POST/PATCH default ``idempotent=False``) skips 5xx/network retries.
        """
        children = [
            {
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [
                        {"type": "text", "text": {"content": text}}
                    ],
                    "checked": checked,
                },
            }
        ]
        url = f"{self.API_URL}/blocks/{parent_id}/children"
        # Default policy — non-idempotent append.
        resp = self._request("patch", url, json={"children": children})
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            return results[0]["id"] if results else None
        log.error(
            "Failed to create to_do in %s: %s %s",
            parent_id, resp.status_code, resp.text,
        )
        return None

    def update_links_callout(
        self,
        page_id: str,
        jira_key: str,
        jira_url: str,
        confluence_url: Optional[str] = None,
    ) -> bool:
        """Find and update (or create) the 🔗 links callout with hyperlinks."""
        # Build rich_text with hyperlinks
        rich_text: List[Dict[str, Any]] = [
            {"type": "text", "text": {"content": "Jira: "}, "annotations": {"bold": True}},
            {"type": "text", "text": {"content": jira_key, "link": {"url": jira_url}}},
        ]
        if confluence_url:
            rich_text.append({"type": "text", "text": {"content": " | "}})
            rich_text.append(
                {"type": "text", "text": {"content": "Confluence: "}, "annotations": {"bold": True}}
            )
            rich_text.append(
                {"type": "text", "text": {"content": "ТЗ", "link": {"url": confluence_url}}}
            )

        # Try to find existing callout
        blocks = self.get_block_children(page_id)
        for block in blocks:
            if block.get("type") != "callout":
                continue
            icon = block.get("callout", {}).get("icon", {})
            if icon.get("emoji") == "🔗":
                return self.update_block_text(block["id"], "callout", rich_text)

        # Not found — create new callout at the top of the page
        callout_block = {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": rich_text,
                "icon": {"type": "emoji", "emoji": "🔗"},
                "color": "purple_background",
            },
        }
        # Also add divider after callout
        divider_block = {"object": "block", "type": "divider", "divider": {}}
        return self.append_children(page_id, [callout_block, divider_block])

    def get_tz_content(self, page_id: str) -> Optional[str]:
        """Extract text from the 'ТЗ' or 'Техническое задание' toggle on a Notion page.

        Returns plain text or None if toggle not found.
        """
        heading_id = self.find_toggle_by_text(page_id, "ТЗ")
        if not heading_id:
            heading_id = self.find_toggle_by_text(page_id, "Техническое задание")
        if not heading_id:
            return None

        children = self.get_block_children(heading_id)
        parts = []
        for child in children:
            child_type = child.get("type", "")
            type_data = child.get(child_type, {})
            rt = type_data.get("rich_text", [])
            text = "".join(r.get("plain_text", "") for r in rt).strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts) if parts else None

    def add_plan_section(
        self, page_id: str, items: List[Dict[str, str]]
    ) -> Optional[str]:
        """Add a 'План выполнения' toggle heading with to_do blocks to a page.

        Args:
            page_id: Notion page ID.
            items: [{"title": "...", "checked": False}, ...] — to_do items.

        Returns: heading block_id or None.
        """
        todo_blocks = [
            {
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [
                        {"type": "text", "text": {"content": item["title"]}}
                    ],
                    "checked": item.get("checked", False),
                },
            }
            for item in items
        ]

        divider_block = {"object": "block", "type": "divider", "divider": {}}

        heading_block = {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "План выполнения"}}
                ],
                "is_toggleable": True,
            },
        }

        # Append divider + heading, then add children to heading
        url = f"{self.API_URL}/blocks/{page_id}/children"
        resp = self._request(
            "patch", url, json={"children": [divider_block, heading_block]}
        )
        if resp.status_code != 200:
            log.error(
                "Failed to add plan heading to %s: %s %s",
                page_id, resp.status_code, resp.text,
            )
            return None

        # results[0] = divider, results[1] = heading
        results = resp.json().get("results", [])
        heading_id = results[1].get("id") if len(results) > 1 else None
        if not heading_id:
            return None

        # Add to_do blocks as children of the toggle heading
        if todo_blocks:
            self.append_children(heading_id, todo_blocks)

        return heading_id
