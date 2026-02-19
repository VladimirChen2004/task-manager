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

from .config import JiraConfig, NOTION_TO_JIRA_STATUS, NOTION_TO_JIRA_PRIORITY


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

    SEARCH_FIELDS = "summary,status,priority,labels,created,updated,duedate,description,subtasks"

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

    def create_issue(
        self,
        title: str,
        description: str = "",
        priority: str = "Medium",
        labels: Optional[List[str]] = None,
        notion_url: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> Dict[str, str]:
        """Create issue in VC project.

        Returns: {"key": "VC-42", "url": "https://..."}
        """
        # Build ADF description for API v3
        desc_text = description
        if notion_url:
            desc_text += f"\n\n---\nNotion: {notion_url}"

        adf_description = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": desc_text}] if desc_text else [],
                }
            ],
        }

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
        """Create subtasks under a parent issue."""
        # Validate parent exists
        self.jira.issue(parent_key)
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
            results.append({"key": data["key"], "title": st["title"]})

        return results

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
        """Calculate progress from subtasks."""
        url = f"{self.server}/rest/api/3/issue/{issue_key}"
        resp = http_requests.get(url, auth=self._auth, timeout=30)
        resp.raise_for_status()
        return self._raw_subtask_progress(resp.json())

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
