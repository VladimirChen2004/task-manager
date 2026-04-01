"""Confluence REST API client for VC project task specifications."""

import logging
import re
from typing import Any, Dict, List, Optional

import requests

from .config import ConfluenceConfig

log = logging.getLogger("taskautomation.confluence")


class ConfluenceClient:
    """Confluence REST API v1 client (storage format / XHTML)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        space_key: Optional[str] = None,
        parent_page_id: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        cfg = ConfluenceConfig()
        self.base_url = (base_url or cfg.base_url).rstrip("/")
        self.space_key = space_key or cfg.space_key
        self.parent_page_id = parent_page_id or cfg.parent_page_id
        self.email = email or cfg.email
        self.api_token = api_token or cfg.api_token
        self._auth = (self.email, self.api_token)

        if not all([self.email, self.api_token]):
            raise ValueError("Confluence credentials required (JIRA_EMAIL + JIRA_API_TOKEN)")

    # ---- HTTP helper ----

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("headers", {"Content-Type": "application/json"})
        return getattr(requests, method)(url, auth=self._auth, **kwargs)

    # ---- Page CRUD ----

    def find_page_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Find a page by exact title in the configured space."""
        url = f"{self.base_url}/rest/api/content"
        params = {
            "spaceKey": self.space_key,
            "title": title,
            "expand": "version,body.storage",
        }
        resp = self._request("get", url, params=params)
        if resp.status_code != 200:
            log.error("Search failed: %s %s", resp.status_code, resp.text[:200])
            return None
        results = resp.json().get("results", [])
        return results[0] if results else None

    def find_page_by_jira_key(self, jira_key: str) -> Optional[Dict[str, Any]]:
        """Find a Confluence page whose title starts with a Jira key."""
        url = f"{self.base_url}/rest/api/content/search"
        params = {
            "cql": f'space = "{self.space_key}" AND title ~ "{jira_key}"',
            "expand": "version",
            "limit": 5,
        }
        resp = self._request("get", url, params=params)
        if resp.status_code != 200:
            log.error("CQL search failed: %s %s", resp.status_code, resp.text[:200])
            return None
        for page in resp.json().get("results", []):
            if page.get("title", "").startswith(jira_key):
                return page
        return None

    def create_page(
        self,
        title: str,
        body_html: str,
        parent_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new Confluence page under the parent."""
        pid = parent_id or self.parent_page_id
        url = f"{self.base_url}/rest/api/content"
        payload: Dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": self.space_key},
            "body": {
                "storage": {
                    "value": body_html,
                    "representation": "storage",
                }
            },
        }
        if pid:
            payload["ancestors"] = [{"id": int(pid)}]

        resp = self._request("post", url, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            log.info("Created Confluence page: %s (id=%s)", title, data.get("id"))
            return data
        log.error("Failed to create page '%s': %s %s", title, resp.status_code, resp.text[:300])
        return None

    def find_or_create_page(
        self,
        jira_key: str,
        title: str,
        body_html: str,
    ) -> Optional[Dict[str, Any]]:
        """Find existing page by Jira key or create new one.

        Jira Automation may already create pages — this avoids duplicates.
        If found, updates body with our template.
        """
        existing = self.find_page_by_jira_key(jira_key)
        if existing:
            log.info("Found existing Confluence page for %s (id=%s)", jira_key, existing.get("id"))
            # Update with our template
            full = self.get_page(existing["id"])
            if full:
                version = full["version"]["number"]
                self.update_page(existing["id"], existing["title"], body_html, version)
            return existing

        # Try to create
        page = self.create_page(title, body_html)
        if page:
            return page

        # If creation failed (title conflict from Jira Automation race), try find again
        existing = self.find_page_by_jira_key(jira_key)
        if existing:
            log.info("Found page after create conflict for %s", jira_key)
            full = self.get_page(existing["id"])
            if full:
                version = full["version"]["number"]
                self.update_page(existing["id"], existing["title"], body_html, version)
            return existing

        return None

    def update_page(
        self,
        page_id: str,
        title: str,
        body_html: str,
        version: int,
    ) -> bool:
        """Update an existing Confluence page body."""
        url = f"{self.base_url}/rest/api/content/{page_id}"
        payload = {
            "type": "page",
            "title": title,
            "body": {
                "storage": {
                    "value": body_html,
                    "representation": "storage",
                }
            },
            "version": {"number": version + 1},
        }
        resp = self._request("put", url, json=payload)
        if resp.status_code == 200:
            return True
        log.error("Failed to update page %s: %s %s", page_id, resp.status_code, resp.text[:300])
        return False

    def get_page(self, page_id: str) -> Optional[Dict[str, Any]]:
        """Get page with body and version info."""
        url = f"{self.base_url}/rest/api/content/{page_id}"
        params = {"expand": "body.storage,version"}
        resp = self._request("get", url, params=params)
        if resp.status_code == 200:
            return resp.json()
        log.error("Failed to get page %s: %s", page_id, resp.status_code)
        return None

    def get_page_url(self, page: Dict[str, Any]) -> str:
        """Build a full web URL for a Confluence page."""
        links = page.get("_links", {})
        base_url = self.base_url.rstrip("/")  # https://nfware.atlassian.net/wiki
        webui = links.get("webui", "")
        if webui:
            return f"{base_url}{webui}"
        page_id = page.get("id", "")
        return f"{base_url}/spaces/{self.space_key}/pages/{page_id}"

    # ---- Page template builder ----

    def build_task_page_html(
        self,
        jira_key: str,
        jira_url: str,
        notion_url: str,
        summary: str = "",
        subtasks: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Build XHTML body for a task specification page."""
        parts = []

        # 1. Links
        parts.append('<h2>Ссылки</h2>')
        links = []
        if jira_url:
            links.append(f'<a href="{jira_url}">{jira_key} (Jira)</a>')
        if notion_url:
            links.append(f'<a href="{notion_url}">Notion</a>')
        if links:
            parts.append(f'<p>{" | ".join(links)}</p>')
        parts.append("<hr/>")

        # 2. Description
        if summary:
            parts.append('<h2>Описание задачи</h2>')
            parts.append(f'<p>{self._escape_html(summary)}</p>')

        # 3. MVP
        parts.append('<h2>Минимальный функционал (MVP)</h2>')
        parts.append(
            '<ul>'
            '<li><em>Описать минимально необходимый результат</em></li>'
            '</ul>'
        )

        # 4. Progress
        parts.append('<h2>Прогресс</h2>')
        parts.append(
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">NOT STARTED</ac:parameter>'
            '<ac:parameter ac:name="colour">Grey</ac:parameter>'
            '</ac:structured-macro>'
        )

        # 9. Plan
        if subtasks:
            parts.append('<h2>План выполнения</h2>')
            parts.append('<ac:task-list>')
            for st in subtasks:
                status = "complete" if st.get("is_done") else "incomplete"
                parts.append(
                    f'<ac:task><ac:task-status>{status}</ac:task-status>'
                    f'<ac:task-body>{self._escape_html(st.get("summary", st.get("title", "")))}'
                    f'</ac:task-body></ac:task>'
                )
            parts.append('</ac:task-list>')

        # 10. Result
        parts.append('<h2>Результат</h2>')
        parts.append('<p><em>Описание выполненной работы (заполняется по итогу).</em></p>')

        # 11. Notes / Log
        parts.append('<h2>Заметки / Лог</h2>')
        parts.append('<p><em>Заметки по ходу работы над задачей.</em></p>')

        return "\n".join(parts)

    def update_progress_status(
        self,
        page_id: str,
        title: str,
        version: int,
        body_html: str,
        done: int,
        total: int,
    ) -> bool:
        """Update the status macro in a page to reflect progress."""
        if total == 0:
            return False

        pct = round(done / total * 100)
        if pct >= 100:
            color, label = "Green", "DONE"
        elif pct >= 50:
            color, label = "Blue", f"IN PROGRESS ({pct}%)"
        elif pct > 0:
            color, label = "Yellow", f"IN PROGRESS ({pct}%)"
        else:
            color, label = "Grey", "NOT STARTED"

        # Replace status macro
        new_macro = (
            f'<ac:structured-macro ac:name="status">'
            f'<ac:parameter ac:name="title">{label}</ac:parameter>'
            f'<ac:parameter ac:name="colour">{color}</ac:parameter>'
            f'</ac:structured-macro>'
        )
        updated = re.sub(
            r'<ac:structured-macro ac:name="status">.*?</ac:structured-macro>',
            new_macro,
            body_html,
            flags=re.DOTALL,
        )

        if updated == body_html:
            return False  # No change

        return self.update_page(page_id, title, updated, version)

    # ---- Section helpers ----

    # Regex: match <h2>heading</h2> + everything until next <h2> or end
    _SECTION_RE = re.compile(
        r'(<h2>(?P<title>[^<]+)</h2>)(?P<content>.*?)(?=<h2>|\Z)',
        re.DOTALL,
    )

    @staticmethod
    def extract_section(body_html: str, heading: str) -> Optional[str]:
        """Extract content between <h2>heading</h2> and the next <h2>.

        Returns inner content string or None if section not found.
        """
        for m in ConfluenceClient._SECTION_RE.finditer(body_html):
            if m.group("title").strip() == heading:
                return m.group("content").strip()
        return None

    @staticmethod
    def replace_section(body_html: str, heading: str, new_content: str) -> str:
        """Replace content of a named <h2> section, keeping the heading."""
        def _replacer(m: re.Match) -> str:
            if m.group("title").strip() == heading:
                return f"{m.group(1)}\n{new_content}\n"
            return m.group(0)
        return ConfluenceClient._SECTION_RE.sub(_replacer, body_html)

    @staticmethod
    def remove_section(body_html: str, heading: str) -> str:
        """Remove an entire section (heading + content) from body HTML."""
        def _replacer(m: re.Match) -> str:
            if m.group("title").strip() == heading:
                return ""
            return m.group(0)
        return ConfluenceClient._SECTION_RE.sub(_replacer, body_html)

    def get_page_with_version(self, page_id: str) -> Optional[Dict[str, Any]]:
        """Get page with body, version, and lastUpdated timestamp."""
        url = f"{self.base_url}/rest/api/content/{page_id}"
        params = {"expand": "body.storage,version"}
        resp = self._request("get", url, params=params)
        if resp.status_code == 200:
            return resp.json()
        log.error("Failed to get page %s: %s", page_id, resp.status_code)
        return None

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
