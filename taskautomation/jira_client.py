"""Jira client for VCHEN project."""

import sys
from typing import Any, Dict, List, Optional

try:
    from jira import JIRA
    from jira.exceptions import JIRAError
except ImportError:
    print("Error: jira package required. Install: pip install jira", file=sys.stderr)
    sys.exit(1)

from .config import JiraConfig, NOTION_TO_JIRA_STATUS, NOTION_TO_JIRA_PRIORITY


class JiraVCHEN:
    """Lightweight Jira client for VCHEN project."""

    PROJECT = "VCHEN"

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
                "  JIRA_VCHEN_URL=https://vchen.atlassian.net\n"
                "  JIRA_VCHEN_EMAIL=your-email\n"
                "  JIRA_VCHEN_API_TOKEN=your-token"
            )

        self.jira = JIRA(
            server=self.server, basic_auth=(self.email, self.api_token)
        )

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
        """Create issue in VCHEN project.

        Returns: {"key": "VCHEN-42", "url": "https://..."}
        """
        full_description = description
        if notion_url:
            full_description += f"\n\n---\n*Notion:* {notion_url}"

        fields = {
            "project": {"key": self.PROJECT},
            "summary": title,
            "issuetype": {"name": "Task"},
            "description": full_description,
            "priority": {"name": priority},
        }

        if labels:
            fields["labels"] = labels

        if due_date:
            fields["duedate"] = due_date

        issue = self.jira.create_issue(fields=fields)
        return {
            "key": issue.key,
            "url": f"{self.server}/browse/{issue.key}",
        }

    def create_subtasks(
        self,
        parent_key: str,
        subtasks: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        """Create subtasks under a parent issue.

        Args:
            parent_key: Parent issue key (e.g., VCHEN-42)
            subtasks: List of {"title": "...", "description": "..."} dicts

        Returns: List of {"key": "VCHEN-43", "title": "..."} dicts
        """
        self.jira.issue(parent_key)  # validate parent exists
        results = []

        for st in subtasks:
            fields = {
                "project": {"key": self.PROJECT},
                "summary": st["title"],
                "issuetype": {"name": "Sub-task"},
                "parent": {"key": parent_key},
                "description": st.get("description", ""),
            }
            issue = self.jira.create_issue(fields=fields)
            results.append({"key": issue.key, "title": st["title"]})

        return results

    # ---- Issue Queries ----

    def get_issue(self, issue_key: str) -> Dict[str, Any]:
        """Get issue details with subtask progress."""
        issue = self.jira.issue(issue_key)
        result = self._issue_to_dict(issue)
        result["progress"] = self.calculate_progress(issue_key)
        return result

    def get_all_active(self) -> List[Dict[str, Any]]:
        """Get all non-Done issues in VCHEN."""
        jql = f"project = {self.PROJECT} AND status != Done ORDER BY updated DESC"
        issues = self.jira.search_issues(jql, maxResults=100)
        results = []
        for issue in issues:
            d = self._issue_to_dict(issue)
            d["progress"] = self._subtask_progress(issue)
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
        issues = self.jira.search_issues(jql, maxResults=50)
        results = []
        for issue in issues:
            d = self._issue_to_dict(issue)
            d["progress"] = self._subtask_progress(issue)
            results.append(d)
        return results

    def calculate_progress(self, issue_key: str) -> Dict[str, Any]:
        """Calculate progress from subtasks.

        Returns: {"done": 2, "total": 5, "percentage": 40.0}
        """
        issue = self.jira.issue(issue_key)
        return self._subtask_progress(issue)

    # ---- Status Transitions ----

    def transition_issue(self, issue_key: str, target_status: str) -> bool:
        """Transition a Jira issue to a new status.

        Uses Jira workflow transitions. Finds the transition ID
        that leads to the target status.

        Returns: True if successful.
        """
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
        """Discover available statuses in VCHEN project workflow."""
        statuses = self.jira.statuses()
        project_statuses = []
        for s in statuses:
            project_statuses.append(
                f"{s.name} (id={s.id}, category={s.statusCategory.name})"
            )
        return sorted(project_statuses)

    def discover_issue_types(self) -> List[str]:
        """Discover available issue types in VCHEN."""
        project = self.jira.project(self.PROJECT)
        return [it.name for it in project.issueTypes]

    # ---- Helpers ----

    def _subtask_progress(self, issue) -> Dict[str, Any]:
        """Calculate subtask progress for an issue."""
        subtasks = getattr(issue.fields, "subtasks", None) or []
        if not subtasks:
            return {"done": 0, "total": 0, "percentage": 0.0}

        total = len(subtasks)
        done = sum(
            1
            for st in subtasks
            if str(st.fields.status).lower() in ("done", "closed", "resolved")
        )
        pct = round(done / total * 100, 1) if total > 0 else 0.0
        return {"done": done, "total": total, "percentage": pct}

    def _issue_to_dict(self, issue) -> Dict[str, Any]:
        """Convert Jira issue to a simple dict."""
        fields = issue.fields
        subtasks = getattr(fields, "subtasks", None) or []

        return {
            "key": issue.key,
            "url": f"{self.server}/browse/{issue.key}",
            "summary": fields.summary,
            "status": str(fields.status),
            "priority": str(fields.priority) if fields.priority else "Medium",
            "labels": fields.labels or [],
            "created": str(fields.created),
            "updated": str(fields.updated),
            "duedate": str(fields.duedate) if fields.duedate else None,
            "description": fields.description or "",
            "subtasks": [
                {
                    "key": st.key,
                    "summary": st.fields.summary,
                    "status": str(st.fields.status),
                }
                for st in subtasks
            ],
        }
