#!/usr/bin/env python3
"""
Lightweight Jira client for VCHEN project.

Manages personal/team tasks with subtask-based progress tracking.

Usage:
    python jira_vchen.py create --title "Task" --priority Medium
    python jira_vchen.py create-subtasks VCHEN-42 --subtasks '[{"title":"..."}]'
    python jira_vchen.py get VCHEN-42
    python jira_vchen.py list-active
    python jira_vchen.py discover-statuses
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from jira import JIRA
    from jira.exceptions import JIRAError
except ImportError:
    print("Error: jira package required. Install: pip install jira", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Load .env from scripts/ directory or project root
_script_dir = Path(__file__).parent
for env_path in [_script_dir / ".env", _script_dir.parent / ".env"]:
    if env_path.exists() and load_dotenv:
        load_dotenv(env_path)
        break


# Status mapping: Notion -> Jira
NOTION_TO_JIRA_STATUS = {
    "Not started": "To Do",
    "Idea": "Backlog",
    "In progress": "In Progress",
    "Hold": "On Hold",
    "Done": "Done",
}

# Reverse mapping: Jira -> Notion
JIRA_TO_NOTION_STATUS = {v: k for k, v in NOTION_TO_JIRA_STATUS.items()}

# Priority mapping: Notion label -> Jira priority name
NOTION_TO_JIRA_PRIORITY = {
    "Наивысшая срочность": "Highest",
    "Срочно": "High",
    "Средняя срочность": "Medium",
    "Не срочно": "Low",
    "Бессрочно": "Lowest",
}


class JiraVCHEN:
    """Lightweight Jira client for VCHEN project."""

    PROJECT = "VCHEN"

    def __init__(
        self,
        server: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        self.server = server or os.environ.get("JIRA_VCHEN_URL", "https://vchen.atlassian.net")
        self.email = email or os.environ.get("JIRA_VCHEN_EMAIL")
        self.api_token = api_token or os.environ.get("JIRA_VCHEN_API_TOKEN")

        if not all([self.server, self.email, self.api_token]):
            raise ValueError(
                "Jira credentials required.\n"
                "Set in .env:\n"
                "  JIRA_VCHEN_URL=https://vchen.atlassian.net\n"
                "  JIRA_VCHEN_EMAIL=your-email\n"
                "  JIRA_VCHEN_API_TOKEN=your-token"
            )

        self.jira = JIRA(server=self.server, basic_auth=(self.email, self.api_token))

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
        parent = self.jira.issue(parent_key)
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
        jql = f'project = {self.PROJECT} AND status != Done ORDER BY updated DESC'
        issues = self.jira.search_issues(jql, maxResults=100)
        results = []
        for issue in issues:
            d = self._issue_to_dict(issue)
            d["progress"] = self._subtask_progress(issue)
            results.append(d)
        return results

    def get_recently_updated(self, since_minutes: int = 10) -> List[Dict[str, Any]]:
        """Get issues updated in last N minutes."""
        jql = (
            f'project = {self.PROJECT} '
            f'AND updated >= "-{since_minutes}m" '
            f'ORDER BY updated DESC'
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

    def _subtask_progress(self, issue) -> Dict[str, Any]:
        """Calculate subtask progress for an issue."""
        subtasks = getattr(issue.fields, "subtasks", None) or []
        if not subtasks:
            return {"done": 0, "total": 0, "percentage": 0.0}

        total = len(subtasks)
        done = sum(
            1 for st in subtasks
            if str(st.fields.status).lower() in ("done", "closed", "resolved")
        )
        pct = round(done / total * 100, 1) if total > 0 else 0.0
        return {"done": done, "total": total, "percentage": pct}

    # ---- Status Discovery ----

    def discover_statuses(self) -> List[str]:
        """Discover available statuses in VCHEN project workflow."""
        statuses = self.jira.statuses()
        project_statuses = []
        for s in statuses:
            project_statuses.append(f"{s.name} (id={s.id}, category={s.statusCategory.name})")
        return sorted(project_statuses)

    def discover_issue_types(self) -> List[str]:
        """Discover available issue types in VCHEN."""
        project = self.jira.project(self.PROJECT)
        return [it.name for it in project.issueTypes]

    # ---- Helpers ----

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


# ---- CLI ----


def main():
    parser = argparse.ArgumentParser(description="Jira VCHEN client")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # create
    p_create = subparsers.add_parser("create", help="Create issue")
    p_create.add_argument("--title", required=True, help="Issue title")
    p_create.add_argument("--description", default="", help="Description")
    p_create.add_argument("--priority", default="Medium",
                          choices=["Highest", "High", "Medium", "Low", "Lowest"])
    p_create.add_argument("--labels", default="", help="Comma-separated labels")
    p_create.add_argument("--notion-url", default=None, help="Notion page URL")
    p_create.add_argument("--due-date", default=None, help="Due date (YYYY-MM-DD)")

    # create-subtasks
    p_sub = subparsers.add_parser("create-subtasks", help="Create subtasks")
    p_sub.add_argument("parent_key", help="Parent issue key (e.g., VCHEN-42)")
    p_sub.add_argument("--subtasks", required=True,
                       help='JSON array: [{"title":"..."},{"title":"..."}]')

    # get
    p_get = subparsers.add_parser("get", help="Get issue details")
    p_get.add_argument("issue_key", help="Issue key (e.g., VCHEN-42)")

    # list-active
    subparsers.add_parser("list-active", help="List active issues")

    # recently-updated
    p_recent = subparsers.add_parser("recently-updated", help="Recently updated issues")
    p_recent.add_argument("--minutes", type=int, default=10, help="Minutes back")

    # discover-statuses
    subparsers.add_parser("discover-statuses", help="Discover available statuses")

    # discover-issue-types
    subparsers.add_parser("discover-issue-types", help="Discover issue types")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        client = JiraVCHEN()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.command == "create":
        labels = [l.strip() for l in args.labels.split(",") if l.strip()] if args.labels else None
        result = client.create_issue(
            title=args.title,
            description=args.description,
            priority=args.priority,
            labels=labels,
            notion_url=args.notion_url,
            due_date=args.due_date,
        )
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "create-subtasks":
        subtasks = json.loads(args.subtasks)
        result = client.create_subtasks(args.parent_key, subtasks)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "get":
        result = client.get_issue(args.issue_key)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "list-active":
        results = client.get_all_active()
        for r in results:
            prog = r["progress"]
            prog_str = f" [{prog['done']}/{prog['total']}]" if prog["total"] > 0 else ""
            print(f"  {r['key']:12s} {r['status']:15s}{prog_str}  {r['summary']}")

    elif args.command == "recently-updated":
        results = client.get_recently_updated(since_minutes=args.minutes)
        for r in results:
            print(f"  {r['key']:12s} {r['status']:15s}  {r['summary']}")

    elif args.command == "discover-statuses":
        statuses = client.discover_statuses()
        print("Available statuses:")
        for s in statuses:
            print(f"  {s}")

    elif args.command == "discover-issue-types":
        types = client.discover_issue_types()
        print("Available issue types:")
        for t in types:
            print(f"  {t}")


if __name__ == "__main__":
    main()
