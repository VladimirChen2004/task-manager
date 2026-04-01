"""Jira client for VC project (nfware.atlassian.net)."""

import sys
from typing import Any, Dict, List, Optional

import requests as http_requests

try:
    from jira import JIRA
    from jira.exceptions import JIRAError
except ImportError:
    print("Error: jira package required. Install: pip install jira", file=sys.stderr)
    sys.exit(1)

from .config import JiraConfig, NOTION_TO_JIRA_STATUS, NOTION_TO_JIRA_PRIORITY, SUBTASK_PROJECT


class JiraVCHEN:
    """Jira client for VC project."""

    PROJECT = "VC"

    def __init__(
        self,
        server: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        cfg = JiraConfig()
        self.server = server or cfg.server
        self.email = email or cfg.email
        self.api_token = api_token or cfg.api_token

        if not all([self.server, self.email, self.api_token]):
            raise ValueError(
                "Jira credentials required.\n"
                "Set in .env:\n"
                "  JIRA_URL=https://nfware.atlassian.net\n"
                "  JIRA_EMAIL=your-email\n"
                "  JIRA_API_TOKEN=your-token"
            )

        self.jira = JIRA(
            server=self.server, basic_auth=(self.email, self.api_token)
        )
        self._auth = (self.email, self.api_token)

    # ---- JQL Search (v3 /search/jql endpoint) ----

    SEARCH_FIELDS = "summary,status,priority,labels,created,updated,duedate,description,subtasks,issuetype,parent"

    def _search_jql(self, jql: str, max_results: int = 100) -> List[Dict[str, Any]]:
        """Search issues via REST API v3 /search/jql (replaces deprecated /search)."""
        url = f"{self.server}/rest/api/3/search/jql"
        all_issues = []
        start_at = 0

        while True:
            params = {
                "jql": jql,
                "maxResults": min(max_results - len(all_issues), 50),
                "startAt": start_at,
                "fields": self.SEARCH_FIELDS,
            }
            resp = http_requests.get(url, auth=self._auth, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            issues = data.get("issues", [])
            all_issues.extend(issues)

            if data.get("isLast", True) or len(all_issues) >= max_results:
                break
            start_at += len(issues)

        return all_issues

    @staticmethod
    def _raw_issue_to_dict(raw: Dict[str, Any], server: str) -> Dict[str, Any]:
        """Convert raw API JSON issue to our dict format."""
        fields = raw.get("fields", {})
        subtasks = fields.get("subtasks", [])

        # v3 description is ADF (Atlassian Document Format), extract plain text
        desc = fields.get("description")
        if isinstance(desc, dict):
            # Simple extraction: get text from first paragraph
            desc_text = ""
            for block in desc.get("content", []):
                for inline in block.get("content", []):
                    desc_text += inline.get("text", "")
                desc_text += "\n"
            desc = desc_text.strip()
        elif desc is None:
            desc = ""

        status = fields.get("status", {})
        priority = fields.get("priority", {})

        issuetype = fields.get("issuetype", {})
        parent = fields.get("parent")

        return {
            "key": raw["key"],
            "url": f"{server}/browse/{raw['key']}",
            "summary": fields.get("summary", ""),
            "status": status.get("name", "") if isinstance(status, dict) else str(status),
            "priority": priority.get("name", "Medium") if isinstance(priority, dict) else "Medium",
            "labels": fields.get("labels", []),
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "duedate": fields.get("duedate"),
            "description": desc,
            "issuetype": issuetype.get("name", "") if isinstance(issuetype, dict) else str(issuetype),
            "is_subtask": parent is not None,
            "subtasks": [
                {
                    "key": st.get("key", ""),
                    "summary": st.get("fields", {}).get("summary", ""),
                    "status": st.get("fields", {}).get("status", {}).get("name", ""),
                }
                for st in subtasks
            ],
        }

    @staticmethod
    def _raw_subtask_progress(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate subtask progress from raw API issue."""
        subtasks = raw.get("fields", {}).get("subtasks", [])
        if not subtasks:
            return {"done": 0, "total": 0, "percentage": 0.0}

        total = len(subtasks)
        done = sum(
            1
            for st in subtasks
            if st.get("fields", {}).get("status", {}).get("statusCategory", {}).get("key") == "done"
            or st.get("fields", {}).get("status", {}).get("name", "").lower() in ("done", "готово", "closed", "resolved")
        )
        pct = round(done / total * 100, 1) if total > 0 else 0.0
        return {"done": done, "total": total, "percentage": pct}

    # ---- Issue Creation ----

    @staticmethod
    def _build_adf_description(
        description: str = "",
        notion_url: Optional[str] = None,
        confluence_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build ADF description with hyperlinks for Notion/Confluence."""
        content: List[Dict[str, Any]] = []

        if description:
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": description}],
            })

        # Links section with real hyperlinks
        links_inline: List[Dict[str, Any]] = []
        if notion_url:
            links_inline.append({"type": "text", "text": "Notion", "marks": [
                {"type": "link", "attrs": {"href": notion_url}}
            ]})
        if confluence_url:
            if links_inline:
                links_inline.append({"type": "text", "text": " | "})
            links_inline.append({"type": "text", "text": "Confluence (ТЗ)", "marks": [
                {"type": "link", "attrs": {"href": confluence_url}}
            ]})

        if links_inline:
            content.append({"type": "rule"})
            content.append({"type": "paragraph", "content": links_inline})

        if not content:
            content.append({"type": "paragraph", "content": []})

        return {"type": "doc", "version": 1, "content": content}

    def create_issue(
        self,
        title: str,
        description: str = "",
        priority: str = "Medium",
        labels: Optional[List[str]] = None,
        notion_url: Optional[str] = None,
        confluence_url: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> Dict[str, str]:
        """Create issue in VC project.

        Returns: {"key": "VC-42", "url": "https://..."}
        """
        adf_description = self._build_adf_description(
            description, notion_url, confluence_url
        )

        payload = {
            "fields": {
                "project": {"key": self.PROJECT},
                "summary": title,
                "issuetype": {"name": "Задача"},
                "description": adf_description,
                "priority": {"name": priority},
            }
        }

        if labels:
            payload["fields"]["labels"] = labels
        if due_date:
            payload["fields"]["duedate"] = due_date

        url = f"{self.server}/rest/api/3/issue"
        resp = http_requests.post(
            url, auth=self._auth, json=payload,
            headers={"Content-Type": "application/json"}, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "key": data["key"],
            "url": f"{self.server}/browse/{data['key']}",
        }

    def create_subtasks(
        self,
        parent_key: str,
        subtasks: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        """Create subtasks under a parent issue.

        If SUBTASK_PROJECT is set, creates regular tasks in that project
        with a label linking back to the parent. Otherwise, creates native
        Jira subtasks in the same project.
        """
        self.jira.issue(parent_key)  # validate parent exists
        results = []

        for st in subtasks:
            desc_text = st.get("description", "")
            adf_desc = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": desc_text}] if desc_text else [],
                    }
                ],
            }

            if SUBTASK_PROJECT:
                # Create as regular task in separate project with parent label
                payload = {
                    "fields": {
                        "project": {"key": SUBTASK_PROJECT},
                        "summary": st["title"],
                        "issuetype": {"name": "Задача"},
                        "description": adf_desc,
                        "labels": [f"parent-{parent_key}"],
                    }
                }
            else:
                # Native subtask in same project
                payload = {
                    "fields": {
                        "project": {"key": self.PROJECT},
                        "summary": st["title"],
                        "issuetype": {"name": "Подзадача"},
                        "parent": {"key": parent_key},
                        "description": adf_desc,
                    }
                }

            url = f"{self.server}/rest/api/3/issue"
            resp = http_requests.post(
                url, auth=self._auth, json=payload,
                headers={"Content-Type": "application/json"}, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            sub_key = data["key"]
            results.append({"key": sub_key, "title": st["title"]})

            # Link subtask to parent when using separate project
            if SUBTASK_PROJECT:
                self._link_issues(parent_key, sub_key)

        return results

    def _link_issues(self, parent_key: str, child_key: str) -> bool:
        """Create 'Relates' link between two issues."""
        payload = {
            "type": {"name": "Relates"},
            "inwardIssue": {"key": parent_key},
            "outwardIssue": {"key": child_key},
        }
        resp = http_requests.post(
            f"{self.server}/rest/api/3/issueLink",
            auth=self._auth, json=payload,
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        return resp.status_code in (200, 201)

    def update_description(
        self,
        issue_key: str,
        description: str = "",
        notion_url: Optional[str] = None,
        confluence_url: Optional[str] = None,
    ) -> bool:
        """Update an existing issue's description with ADF hyperlinks."""
        adf = self._build_adf_description(description, notion_url, confluence_url)
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        payload = {"fields": {"description": adf}}
        resp = http_requests.put(
            url, auth=self._auth, json=payload,
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        return resp.status_code in (200, 204)

    # ---- Issue Queries ----

    def get_issue(self, issue_key: str) -> Dict[str, Any]:
        """Get issue details with subtask progress."""
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        resp = http_requests.get(url, auth=self._auth, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        result = self._raw_issue_to_dict(raw, self.server)
        result["progress"] = self._raw_subtask_progress(raw)
        return result

    def get_all_issues(self, max_results: int = 200) -> List[Dict[str, Any]]:
        """Get ALL issues in project (including Done)."""
        jql = f'project = {self.PROJECT} ORDER BY created DESC'
        raw_issues = self._search_jql(jql, max_results=max_results)
        results = []
        for raw in raw_issues:
            d = self._raw_issue_to_dict(raw, self.server)
            d["progress"] = self._raw_subtask_progress(raw)
            results.append(d)
        return results

    def get_all_active(self) -> List[Dict[str, Any]]:
        """Get all non-Done issues."""
        jql = f'project = {self.PROJECT} AND statusCategory != Done ORDER BY updated DESC'
        raw_issues = self._search_jql(jql, max_results=100)
        results = []
        for raw in raw_issues:
            d = self._raw_issue_to_dict(raw, self.server)
            d["progress"] = self._raw_subtask_progress(raw)
            results.append(d)
        return results

    def get_recently_updated(
        self, since_minutes: int = 10
    ) -> List[Dict[str, Any]]:
        """Get issues updated in last N minutes."""
        jql = (
            f"project = {self.PROJECT} "
            f'AND updated >= "-{since_minutes}m" '
            f"ORDER BY updated DESC"
        )
        raw_issues = self._search_jql(jql, max_results=50)
        results = []
        for raw in raw_issues:
            d = self._raw_issue_to_dict(raw, self.server)
            d["progress"] = self._raw_subtask_progress(raw)
            results.append(d)
        return results

    def calculate_progress(self, issue_key: str) -> Dict[str, Any]:
        """Calculate progress from subtasks (native or VCSUB-linked)."""
        if SUBTASK_PROJECT:
            details = self._get_linked_subtask_details(issue_key)
            if not details:
                return {"done": 0, "total": 0, "percentage": 0.0}
            total = len(details)
            done = sum(1 for d in details if d["is_done"])
            pct = round(done / total * 100, 1) if total > 0 else 0.0
            return {"done": done, "total": total, "percentage": pct}

        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        resp = http_requests.get(url, auth=self._auth, timeout=30)
        resp.raise_for_status()
        return self._raw_subtask_progress(resp.json())

    def get_subtask_details(self, parent_key: str) -> List[Dict[str, Any]]:
        """Get subtask details for an issue with is_done flag.

        Returns: [{"key": "VC-14", "summary": "...", "status": "...", "is_done": bool}]
        Supports both native subtasks and VCSUB-linked tasks.
        """
        if SUBTASK_PROJECT:
            return self._get_linked_subtask_details(parent_key)

        issue = self.get_issue(parent_key)
        result = []
        for st in issue.get("subtasks", []):
            status = st.get("status", "")
            is_done = status.lower() in ("done", "готово", "closed", "resolved")
            result.append({
                "key": st["key"],
                "summary": st.get("summary", ""),
                "status": status,
                "is_done": is_done,
            })
        return result

    _LINKED_SEARCH_FIELDS = "summary,status,labels"

    def _get_linked_subtask_details(
        self, parent_key: str
    ) -> List[Dict[str, Any]]:
        """Get subtask details from VCSUB project (linked via label)."""
        jql = f'project = {SUBTASK_PROJECT} AND labels = "parent-{parent_key}" ORDER BY created ASC'
        # Use minimal fields — VCSUB may not have all VC fields
        saved = self.SEARCH_FIELDS
        self.SEARCH_FIELDS = self._LINKED_SEARCH_FIELDS
        raw_issues = self._search_jql(jql, max_results=50)
        self.SEARCH_FIELDS = saved
        result = []
        for raw in raw_issues:
            fields = raw.get("fields", {})
            status_name = fields.get("status", {}).get("name", "")
            cat_key = fields.get("status", {}).get("statusCategory", {}).get("key", "")
            is_done = (
                cat_key == "done"
                or status_name.lower() in ("done", "готово", "closed", "resolved")
            )
            result.append({
                "key": raw["key"],
                "summary": fields.get("summary", ""),
                "status": status_name,
                "is_done": is_done,
            })
        return result

    def rename_issue(self, issue_key: str, new_summary: str) -> bool:
        """Rename a Jira issue (update summary)."""
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        payload = {"fields": {"summary": new_summary}}
        resp = http_requests.put(
            url, auth=self._auth, json=payload,
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        return resp.status_code in (200, 204)

    def delete_issue(self, issue_key: str) -> bool:
        """Delete a Jira issue (subtask or linked task)."""
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        resp = http_requests.delete(url, auth=self._auth, timeout=30)
        return resp.status_code in (200, 204)

    # ---- Custom Fields ----

    PROGRESS_FIELD = "customfield_11900"
    CONFLUENCE_URL_FIELD = "customfield_11901"
    PROGRESS_BAR_SIZE = 12

    @staticmethod
    def _build_progress_bar(done: int, total: int, percentage: float) -> str:
        """Build progress bar: █████————————————— 2/7"""
        size = JiraVCHEN.PROGRESS_BAR_SIZE
        filled = round(percentage / 100 * size)
        bar = "█" * filled + "—" * (size - filled)
        return f"{bar} {done}/{total}"

    def update_delivery_progress_field(
        self,
        issue_key: str,
        progress: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update 'Progress bar' field with sword-style bar based on subtask completion."""
        if progress is None:
            progress = self.calculate_progress(issue_key)

        total = progress.get("total", 0)
        if total == 0:
            return False

        bar = self._build_progress_bar(
            progress["done"], total, progress["percentage"]
        )
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        payload = {"fields": {self.PROGRESS_FIELD: bar}}
        resp = http_requests.put(
            url, auth=self._auth, json=payload,
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        return resp.status_code in (200, 204)

    def update_confluence_url(self, issue_key: str, confluence_url: str) -> bool:
        """Update 'Confluence URL' field."""
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        payload = {"fields": {self.CONFLUENCE_URL_FIELD: confluence_url}}
        resp = http_requests.put(
            url, auth=self._auth, json=payload,
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        return resp.status_code in (200, 204)

    # ---- Field Updates ----

    def update_priority(self, issue_key: str, priority_name: str) -> bool:
        """Update the priority of a Jira issue."""
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        resp = http_requests.put(
            url,
            json={"fields": {"priority": {"name": priority_name}}},
            auth=self._auth,
            timeout=30,
        )
        if resp.status_code == 204:
            return True
        log.error("Failed to update priority %s: %s %s", issue_key, resp.status_code, resp.text[:200])
        return False

    # ---- Status Transitions ----

    def transition_issue(self, issue_key: str, target_status: str) -> bool:
        """Transition a Jira issue to a new status."""
        issue = self.jira.issue(issue_key)
        transitions = self.jira.transitions(issue)

        # Exact match first
        for t in transitions:
            if t["to"]["name"].lower() == target_status.lower():
                self.jira.transition_issue(issue, t["id"])
                return True

        # Partial match fallback
        for t in transitions:
            if target_status.lower() in t["to"]["name"].lower():
                self.jira.transition_issue(issue, t["id"])
                return True

        return False

    def get_available_transitions(self, issue_key: str) -> List[Dict[str, str]]:
        """Get available transitions for an issue."""
        issue = self.jira.issue(issue_key)
        return [
            {"id": t["id"], "name": t["name"], "to": t["to"]["name"]}
            for t in self.jira.transitions(issue)
        ]

    # ---- Status Discovery ----

    def discover_statuses(self) -> List[str]:
        """Discover available statuses in project workflow."""
        statuses = self.jira.statuses()
        project_statuses = []
        for s in statuses:
            project_statuses.append(
                f"{s.name} (id={s.id}, category={s.statusCategory.name})"
            )
        return sorted(project_statuses)

    def discover_issue_types(self) -> List[str]:
        """Discover available issue types."""
        project = self.jira.project(self.PROJECT)
        return [it.name for it in project.issueTypes]
