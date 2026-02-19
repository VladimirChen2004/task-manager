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

        resp = requests.post(url, headers=self.headers, json=payload, timeout=30)
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

            resp = requests.post(
                url, headers=self.headers, json=payload, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            all_pages.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
            time.sleep(0.4)

        return all_pages

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

    # ---- Page Property Updates ----

    def update_page_status(self, page_id: str, status_name: str) -> bool:
        """Update the Status property of a Notion page."""
        url = f"{self.API_URL}/pages/{page_id}"
        payload = {
            "properties": {"Status": {"status": {"name": status_name}}}
        }

        resp = requests.patch(
            url, headers=self.headers, json=payload, timeout=30
        )
        if resp.status_code == 200:
            return True
        log.error(
            "Failed to update page %s: %s %s",
            page_id,
            resp.status_code,
            resp.text,
        )
        return False

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

            resp = requests.get(
                url, headers=self.headers, params=params, timeout=30
            )
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

        resp = requests.patch(
            url, headers=self.headers, json=payload, timeout=30
        )
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
        """Append child blocks to a parent block."""
        url = f"{self.API_URL}/blocks/{block_id}/children"
        payload = {"children": children}

        resp = requests.patch(
            url, headers=self.headers, json=payload, timeout=30
        )
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
        resp = requests.delete(url, headers=self.headers, timeout=30)
        return resp.status_code == 200
