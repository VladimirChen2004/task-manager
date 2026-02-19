"""Sync logic: Jira <-> Notion status and progress synchronization."""

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import (
    JIRA_TO_NOTION_STATUS,
    NOTION_TO_JIRA_STATUS,
    STATE_FILE,
    get_progress_emoji,
)
from .jira_client import JiraVCHEN
from .notion_client import NotionClient

log = logging.getLogger("taskautomation.sync")


def _load_state() -> Dict[str, Any]:
    """Load sync state from file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: Dict[str, Any]):
    """Save sync state to file."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    except OSError as e:
        log.warning("Could not save state: %s", e)


# ---- Progress helpers ----


def build_progress_rich_text(emoji: str, percentage: float) -> List[Dict]:
    """Build Notion rich_text array for progress line."""
    return [
        {
            "type": "text",
            "text": {"content": "Прогресс: "},
            "annotations": {"bold": True},
        },
        {
            "type": "text",
            "text": {"content": f"{emoji} {int(percentage)}%"},
        },
    ]


# ---- Jira → Notion Sync ----


class JiraToNotionSync:
    """Synchronizes Jira VCHEN statuses (and optionally progress) to Notion."""

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        dry_run: bool = False,
        with_progress: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.dry_run = dry_run
        self.with_progress = with_progress
        self.stats = {"checked": 0, "updated": 0, "skipped": 0, "errors": 0}

    def run_incremental(self, since_minutes: int = 15):
        """Sync only recently updated Jira issues."""
        log.info(
            "Jira→Notion incremental sync: last %d minutes", since_minutes
        )
        issues = self.jira.get_recently_updated(since_minutes=since_minutes)
        log.info("Found %d updated issues", len(issues))
        self._sync_issues(issues)
        self._log_stats()

    def run_full(self):
        """Full sync: all active Jira issues."""
        log.info("Jira→Notion full sync: all active issues")
        issues = self.jira.get_all_active()
        log.info("Found %d active issues", len(issues))
        self._sync_issues(issues)
        self._log_stats()

    def _sync_issues(self, issues: List[Dict[str, Any]]):
        for issue in issues:
            self.stats["checked"] += 1
            try:
                self._sync_one(issue)
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Error syncing %s: %s", issue.get("key", "?"), e)
            time.sleep(0.4)

    def _sync_one(self, issue: Dict[str, Any]):
        jira_key = issue["key"]
        jira_status = issue["status"]

        page = self.notion.find_page_by_jira_key(jira_key)
        if not page:
            log.debug("No Notion page for %s, skipping", jira_key)
            self.stats["skipped"] += 1
            return

        page_id = page["id"]
        notion_status = self.notion.get_page_status(page)

        # Map and update status
        expected = JIRA_TO_NOTION_STATUS.get(jira_status)
        if not expected:
            log.debug(
                "%s: Jira status '%s' has no mapping, skipping",
                jira_key,
                jira_status,
            )
            self.stats["skipped"] += 1
            return

        if notion_status != expected:
            if self.dry_run:
                log.info(
                    "[DRY-RUN] %s: would update '%s' → '%s'",
                    jira_key,
                    notion_status,
                    expected,
                )
            else:
                success = self.notion.update_page_status(page_id, expected)
                if success:
                    log.info(
                        "%s: status '%s' → '%s'",
                        jira_key,
                        notion_status,
                        expected,
                    )
                    self.stats["updated"] += 1
                else:
                    self.stats["errors"] += 1
                    return
        else:
            self.stats["skipped"] += 1

        # Sync progress if enabled
        if self.with_progress:
            self._sync_progress(issue, page_id)

    def _sync_progress(self, issue: Dict[str, Any], page_id: str):
        """Sync Jira subtask progress into Notion page content."""
        progress = issue.get("progress", {})
        total = progress.get("total", 0)
        if total == 0:
            return

        percentage = progress["percentage"]
        emoji = get_progress_emoji(percentage)

        result = self.notion.find_progress_block(page_id)
        if not result:
            log.debug(
                "%s: progress block not found on page", issue["key"]
            )
            return

        block_id, current_text = result
        expected_text = f"{emoji} {int(percentage)}%"
        if expected_text in current_text:
            return  # already up to date

        if self.dry_run:
            log.info(
                "[DRY-RUN] %s: would update progress to %s",
                issue["key"],
                expected_text,
            )
            return

        # Determine block type from the find result
        # The block could be a paragraph inside callout or the callout itself
        # Try paragraph first (most common), fallback to callout
        rich_text = build_progress_rich_text(emoji, percentage)
        success = self.notion.update_block_text(
            block_id, "paragraph", rich_text
        )
        if not success:
            # Might be a callout block with inline text
            success = self.notion.update_block_text(
                block_id, "callout", rich_text
            )

        if success:
            log.info(
                "%s: progress updated to %s %d%%",
                issue["key"],
                emoji,
                int(percentage),
            )
        else:
            log.error("%s: failed to update progress block", issue["key"])

    def _log_stats(self):
        s = self.stats
        log.info(
            "Jira→Notion: checked=%d, updated=%d, skipped=%d, errors=%d",
            s["checked"],
            s["updated"],
            s["skipped"],
            s["errors"],
        )


# ---- Notion → Jira Sync ----


class NotionToJiraSync:
    """Sync Notion status changes to Jira."""

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        dry_run: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.dry_run = dry_run
        self.stats = {"checked": 0, "updated": 0, "skipped": 0, "errors": 0}
        self._state = _load_state()
        self._known = self._state.get("known_notion_statuses", {})

    def run(self):
        """Query all Notion pages with Jira Key, sync changes to Jira."""
        log.info("Notion→Jira sync: checking all pages with Jira Key")
        pages = self.notion.query_all_pages_with_jira_key()
        log.info("Found %d pages with Jira Key", len(pages))

        for page in pages:
            self.stats["checked"] += 1
            try:
                self._sync_one(page)
            except Exception as e:
                self.stats["errors"] += 1
                jira_key = NotionClient.get_jira_key(page) or "?"
                log.error("Error syncing %s: %s", jira_key, e)
            time.sleep(0.4)

        self._save()
        self._log_stats()

    def _sync_one(self, page: Dict[str, Any]):
        jira_key = NotionClient.get_jira_key(page)
        if not jira_key:
            self.stats["skipped"] += 1
            return

        current_status = self.notion.get_page_status(page)
        known_status = self._known.get(jira_key)

        if known_status and current_status != known_status:
            # Status changed in Notion
            target_jira = NOTION_TO_JIRA_STATUS.get(current_status)
            if not target_jira:
                log.debug(
                    "%s: Notion status '%s' has no Jira mapping",
                    jira_key,
                    current_status,
                )
                self.stats["skipped"] += 1
            elif self.dry_run:
                log.info(
                    "[DRY-RUN] %s: would transition Jira to '%s' (Notion: '%s' → '%s')",
                    jira_key,
                    target_jira,
                    known_status,
                    current_status,
                )
            else:
                success = self.jira.transition_issue(jira_key, target_jira)
                if success:
                    log.info(
                        "%s: Jira transitioned to '%s' (Notion: '%s')",
                        jira_key,
                        target_jira,
                        current_status,
                    )
                    self.stats["updated"] += 1
                else:
                    log.warning(
                        "%s: failed to transition Jira to '%s'",
                        jira_key,
                        target_jira,
                    )
                    self.stats["errors"] += 1
        else:
            self.stats["skipped"] += 1

        # Always update known status
        self._known[jira_key] = current_status

    def _save(self):
        self._state["last_notion_to_jira_sync"] = datetime.now().isoformat()
        self._state["known_notion_statuses"] = self._known
        _save_state(self._state)

    def _log_stats(self):
        s = self.stats
        log.info(
            "Notion→Jira: checked=%d, updated=%d, skipped=%d, errors=%d",
            s["checked"],
            s["updated"],
            s["skipped"],
            s["errors"],
        )


# ---- Orchestrator ----


def run_sync(
    full: bool = False,
    dry_run: bool = False,
    minutes: int = 15,
    with_progress: bool = False,
    reverse: bool = False,
    bidirectional: bool = False,
):
    """Main sync entry point."""
    import os

    notion_token = os.environ.get("NOTION_API_TOKEN")
    if not notion_token:
        log.error(
            "NOTION_API_TOKEN not set.\n"
            "Create integration at https://www.notion.so/my-integrations\n"
            "Share 'Tasks 2026' database with the integration.\n"
            "Set NOTION_API_TOKEN in .env file."
        )
        return False

    try:
        jira = JiraVCHEN()
    except ValueError as e:
        log.error("Jira init failed: %s", e)
        return False

    notion = NotionClient()

    if reverse:
        # Only Notion → Jira
        sync = NotionToJiraSync(jira=jira, notion=notion, dry_run=dry_run)
        sync.run()
    elif bidirectional:
        # Jira → Notion first (Jira wins conflicts)
        j2n = JiraToNotionSync(
            jira=jira,
            notion=notion,
            dry_run=dry_run,
            with_progress=with_progress,
        )
        if full:
            j2n.run_full()
        else:
            j2n.run_incremental(since_minutes=minutes)

        # Save state after Jira→Notion
        state = _load_state()
        state["last_jira_to_notion_sync"] = datetime.now().isoformat()
        state["stats_jira_to_notion"] = j2n.stats
        _save_state(state)

        # Then Notion → Jira
        n2j = NotionToJiraSync(jira=jira, notion=notion, dry_run=dry_run)
        n2j.run()
    else:
        # Default: Jira → Notion only
        sync = JiraToNotionSync(
            jira=jira,
            notion=notion,
            dry_run=dry_run,
            with_progress=with_progress,
        )
        if full:
            sync.run_full()
        else:
            sync.run_incremental(since_minutes=minutes)

        state = _load_state()
        state["last_jira_to_notion_sync"] = datetime.now().isoformat()
        state["stats"] = sync.stats
        _save_state(state)

    return True
