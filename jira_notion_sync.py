#!/usr/bin/env python3
"""
Jira → Notion sync script.

Periodically checks Jira VCHEN for status/progress changes and updates
corresponding Notion pages in Tasks 2026 database.

Uses Notion REST API directly (not MCP) for server-side cron execution.

Setup:
    1. Create Notion Internal Integration: https://www.notion.so/my-integrations
    2. Share "Tasks 2026" database with the integration
    3. Set env vars: NOTION_API_TOKEN, NOTION_DATABASE_ID, JIRA_VCHEN_*

Usage:
    python scripts/jira_notion_sync.py               # incremental sync (last 15 min)
    python scripts/jira_notion_sync.py --full         # full sync (all active issues)
    python scripts/jira_notion_sync.py --dry-run      # preview changes without applying
    python scripts/jira_notion_sync.py --minutes 30   # custom time window
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# Add parent dir to path so we can import jira_vchen
sys.path.insert(0, str(Path(__file__).parent))
from jira_vchen import JiraVCHEN, JIRA_TO_NOTION_STATUS

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Load .env
_script_dir = Path(__file__).parent
for env_path in [_script_dir / ".env", _script_dir.parent / ".env"]:
    if env_path.exists() and load_dotenv:
        load_dotenv(env_path)
        break

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jira_notion_sync")

# State file for tracking last sync time
STATE_FILE = _script_dir / ".sync_state.json"


class NotionClient:
    """Minimal Notion API client for sync purposes."""

    API_URL = "https://api.notion.com/v1"
    VERSION = "2022-06-28"

    def __init__(self, api_token: str, database_id: str):
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Notion-Version": self.VERSION,
            "Content-Type": "application/json",
        }

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

    def get_page_status(self, page: Dict[str, Any]) -> Optional[str]:
        """Extract current Status from a Notion page dict."""
        status_prop = page.get("properties", {}).get("Status", {})
        status_obj = status_prop.get("status")
        if status_obj:
            return status_obj.get("name")
        return None

    def update_page_status(self, page_id: str, status_name: str) -> bool:
        """Update the Status property of a Notion page."""
        url = f"{self.API_URL}/pages/{page_id}"
        payload = {
            "properties": {
                "Status": {
                    "status": {"name": status_name}
                }
            }
        }

        resp = requests.patch(url, headers=self.headers, json=payload, timeout=30)
        if resp.status_code == 200:
            return True
        log.error("Failed to update page %s: %s %s", page_id, resp.status_code, resp.text)
        return False

    def get_page_blocks(self, page_id: str) -> List[Dict[str, Any]]:
        """Get child blocks of a page (for future progress sync)."""
        url = f"{self.API_URL}/blocks/{page_id}/children"
        params = {"page_size": 100}

        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("results", [])


class JiraNotionSync:
    """Synchronizes Jira VCHEN statuses to Notion Tasks 2026."""

    def __init__(self, jira: JiraVCHEN, notion: NotionClient, dry_run: bool = False):
        self.jira = jira
        self.notion = notion
        self.dry_run = dry_run
        self.stats = {"checked": 0, "updated": 0, "skipped": 0, "errors": 0}

    def run_incremental(self, since_minutes: int = 15):
        """Sync only recently updated Jira issues."""
        log.info("Incremental sync: checking issues updated in last %d minutes", since_minutes)
        issues = self.jira.get_recently_updated(since_minutes=since_minutes)
        log.info("Found %d updated issues", len(issues))
        self._sync_issues(issues)
        self._log_stats()
        self._save_state()

    def run_full(self):
        """Full sync: all active Jira issues."""
        log.info("Full sync: checking all active issues")
        issues = self.jira.get_all_active()
        log.info("Found %d active issues", len(issues))
        self._sync_issues(issues)
        self._log_stats()
        self._save_state()

    def _sync_issues(self, issues: List[Dict[str, Any]]):
        """Process a list of Jira issues."""
        for issue in issues:
            self.stats["checked"] += 1
            try:
                self._sync_one(issue)
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Error syncing %s: %s", issue.get("key", "?"), e)
            # Small delay to respect Notion rate limits (3 req/s)
            time.sleep(0.4)

    def _sync_one(self, issue: Dict[str, Any]):
        """Sync a single Jira issue to Notion."""
        jira_key = issue["key"]
        jira_status = issue["status"]

        # Find corresponding Notion page
        page = self.notion.find_page_by_jira_key(jira_key)
        if not page:
            log.debug("No Notion page for %s, skipping", jira_key)
            self.stats["skipped"] += 1
            return

        page_id = page["id"]
        notion_status = self.notion.get_page_status(page)

        # Map Jira status to Notion status
        expected_notion_status = JIRA_TO_NOTION_STATUS.get(jira_status)
        if not expected_notion_status:
            log.debug("%s: Jira status '%s' has no Notion mapping, skipping", jira_key, jira_status)
            self.stats["skipped"] += 1
            return

        # Check if update is needed
        if notion_status == expected_notion_status:
            log.debug("%s: status already in sync (%s)", jira_key, notion_status)
            self.stats["skipped"] += 1
            return

        # Update Notion
        if self.dry_run:
            log.info("[DRY-RUN] %s: would update Notion status '%s' → '%s'",
                     jira_key, notion_status, expected_notion_status)
        else:
            success = self.notion.update_page_status(page_id, expected_notion_status)
            if success:
                log.info("%s: updated Notion status '%s' → '%s'",
                         jira_key, notion_status, expected_notion_status)
                self.stats["updated"] += 1
            else:
                self.stats["errors"] += 1
                return

        # Log progress info
        progress = issue.get("progress", {})
        if progress.get("total", 0) > 0:
            log.info("%s: subtask progress %d/%d (%.0f%%)",
                     jira_key, progress["done"], progress["total"], progress["percentage"])

    def _log_stats(self):
        """Log sync statistics."""
        s = self.stats
        log.info(
            "Sync complete: checked=%d, updated=%d, skipped=%d, errors=%d",
            s["checked"], s["updated"], s["skipped"], s["errors"],
        )

    def _save_state(self):
        """Save last sync timestamp."""
        state = {
            "last_sync": datetime.now().isoformat(),
            "stats": self.stats,
        }
        try:
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except OSError as e:
            log.warning("Could not save state: %s", e)


def main():
    parser = argparse.ArgumentParser(description="Jira → Notion sync")
    parser.add_argument("--full", action="store_true", help="Full sync (all active issues)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    parser.add_argument("--minutes", type=int, default=15, help="Time window for incremental sync")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Check Notion credentials
    notion_token = os.environ.get("NOTION_API_TOKEN")
    notion_db_id = os.environ.get("NOTION_DATABASE_ID", "3050c57fd84181a7bb22ee1b23b37c6e")

    if not notion_token:
        log.error(
            "NOTION_API_TOKEN not set.\n"
            "Create integration at https://www.notion.so/my-integrations\n"
            "Share 'Tasks 2026' database with the integration.\n"
            "Set NOTION_API_TOKEN in .env file."
        )
        sys.exit(1)

    # Initialize clients
    try:
        jira = JiraVCHEN()
    except ValueError as e:
        log.error("Jira init failed: %s", e)
        sys.exit(1)

    notion = NotionClient(api_token=notion_token, database_id=notion_db_id)

    # Run sync
    sync = JiraNotionSync(jira=jira, notion=notion, dry_run=args.dry_run)

    if args.full:
        sync.run_full()
    else:
        sync.run_incremental(since_minutes=args.minutes)


if __name__ == "__main__":
    main()
