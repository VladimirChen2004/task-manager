"""Sync logic: Jira <-> Notion status and progress synchronization."""

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import (
    DEFAULT_SUBTASKS,
    JIRA_TO_NOTION_PRIORITY,
    JIRA_TO_NOTION_STATUS,
    NOTION_TO_JIRA_PRIORITY,
    NOTION_TO_JIRA_STATUS,
    STATE_FILE,
    get_progress_emoji,
)
from dateutil.parser import isoparse

from .confluence_client import ConfluenceClient
from .jira_client import JiraVCHEN
from .notion_client import NotionClient
from .orphan_keys import OrphanResolver

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


# ---- Timestamp helpers ----


def _parse_timestamp(ts: str) -> Optional[datetime]:
    """Parse ISO timestamp from Jira or Notion into timezone-aware datetime."""
    if not ts:
        return None
    try:
        return isoparse(ts)
    except (ValueError, TypeError):
        return None


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


class BidirectionalSync:
    """Bidirectional Jira ↔ Notion sync for status and priority.

    Detection logic (per task):
    1. Load known state (last synced snapshot of Notion status/priority)
    2. Compare current Jira status (mapped to Notion) and current Notion status with known
    3. Determine what changed:
       - Only Notion changed → push to Jira
       - Only Jira changed  → push to Notion
       - Both changed       → compare timestamps, latest wins
       - Neither changed    → skip
    4. Save new known state
    """

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
        self.stats = {
            "checked": 0, "jira_to_notion": 0, "notion_to_jira": 0,
            "conflicts": 0, "skipped": 0, "errors": 0,
        }
        self._state = _load_state()
        # Migrate from old flat format {"VC-1": "In progress"}
        # to new dual format {"VC-1": {"notion": "In progress", "jira": "In progress"}}
        raw_statuses = self._state.get("known_notion_statuses", {})
        self._known = {}
        for k, v in raw_statuses.items():
            if isinstance(v, dict):
                self._known[k] = v
            else:
                # Old format: single value = Notion status; assume Jira matched
                self._known[k] = {"notion": v, "jira": v}
        raw_priorities = self._state.get("known_notion_priorities", {})
        self._known_priorities = {}
        for k, v in raw_priorities.items():
            if isinstance(v, dict):
                self._known_priorities[k] = v
            else:
                self._known_priorities[k] = {"notion": v, "jira": v}

    def run_full(self):
        """Full bidirectional sync: all active Jira issues.

        Note: orphan-key handling is not needed here. The list of
        issues comes FROM Jira (``get_all_active``), so a deleted
        issue cannot enter this loop in the first place.
        """
        log.info("Bidirectional sync: all active issues")
        issues = self.jira.get_all_active()
        log.info("Found %d active issues", len(issues))
        for issue in issues:
            self.stats["checked"] += 1
            try:
                self._sync_one(issue)
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Error syncing %s: %s", issue.get("key", "?"), e)
            time.sleep(0.4)
        self._save()
        self._log_stats()

    def run_incremental(self, since_minutes: int = 15):
        """Sync only recently updated Jira issues."""
        log.info("Bidirectional incremental sync: last %d minutes", since_minutes)
        issues = self.jira.get_recently_updated(since_minutes=since_minutes)
        log.info("Found %d updated issues", len(issues))
        for issue in issues:
            self.stats["checked"] += 1
            try:
                self._sync_one(issue)
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Error syncing %s: %s", issue.get("key", "?"), e)
            time.sleep(0.4)
        self._save()
        self._log_stats()

    def _sync_one(self, issue: Dict[str, Any]):
        jira_key = issue["key"]
        jira_status = issue["status"]
        jira_priority = issue.get("priority", "Medium")

        page = self.notion.find_page_by_jira_key(jira_key)
        if not page:
            log.debug("No Notion page for %s, skipping", jira_key)
            self.stats["skipped"] += 1
            return

        page_id = page["id"]
        notion_status = self.notion.get_page_status(page)
        notion_priority = NotionClient.get_page_priority(page) or "Medium"

        # Don't override Archived — terminal Notion status
        if notion_status == "Archived":
            self.stats["skipped"] += 1
            return

        # Map values
        jira_as_notion_status = JIRA_TO_NOTION_STATUS.get(jira_status)
        jira_as_notion_priority = JIRA_TO_NOTION_PRIORITY.get(jira_priority, "Medium")

        # --- Status sync ---
        self._sync_status(
            issue, page, page_id, jira_key,
            jira_status, notion_status, jira_as_notion_status,
        )

        # --- Priority sync ---
        self._sync_priority(
            issue, page, page_id, jira_key,
            notion_priority, jira_as_notion_priority,
        )

        # --- Progress sync ---
        if self.with_progress:
            self._sync_progress(issue, page_id)

    def _sync_status(
        self, issue, page, page_id, jira_key,
        jira_status, notion_status, jira_as_notion_status,
    ):
        if not jira_as_notion_status:
            return

        known = self._known.get(jira_key)

        if notion_status == jira_as_notion_status:
            # Already in sync — just record both sides
            self._known[jira_key] = {"notion": notion_status, "jira": jira_as_notion_status}
            return

        if known is None:
            # First time seeing this task — record both sides, don't sync
            self._known[jira_key] = {"notion": notion_status, "jira": jira_as_notion_status}
            return

        # Determine who changed relative to their own last-known value
        notion_changed = (notion_status != known.get("notion"))
        jira_changed = (jira_as_notion_status != known.get("jira"))

        if notion_changed and not jira_changed:
            # Only Notion changed → push to Jira
            self._push_status_to_jira(jira_key, notion_status)
            self._known[jira_key] = {"notion": notion_status, "jira": notion_status}

        elif jira_changed and not notion_changed:
            # Only Jira changed → push to Notion
            self._push_status_to_notion(page_id, jira_key, jira_as_notion_status)
            self._known[jira_key] = {"notion": jira_as_notion_status, "jira": jira_as_notion_status}

        elif notion_changed and jira_changed:
            # Both changed — resolve by timestamp
            self.stats["conflicts"] += 1
            jira_ts = _parse_timestamp(issue.get("updated", ""))
            notion_ts = _parse_timestamp(page.get("last_edited_time", ""))

            if jira_ts and notion_ts and jira_ts > notion_ts:
                winner = jira_as_notion_status
                log.info(
                    "%s: CONFLICT status — Jira wins (jira=%s > notion=%s)",
                    jira_key, jira_ts.isoformat(), notion_ts.isoformat(),
                )
                self._push_status_to_notion(page_id, jira_key, winner)
            else:
                winner = notion_status
                log.info(
                    "%s: CONFLICT status — Notion wins (notion=%s >= jira=%s)",
                    jira_key,
                    notion_ts.isoformat() if notion_ts else "?",
                    jira_ts.isoformat() if jira_ts else "?",
                )
                self._push_status_to_jira(jira_key, winner)
            self._known[jira_key] = {"notion": winner, "jira": winner}

        # else: neither changed (existing discrepancy) — don't touch

    def _sync_priority(
        self, issue, page, page_id, jira_key,
        notion_priority, jira_as_notion_priority,
    ):
        known = self._known_priorities.get(jira_key)

        if notion_priority == jira_as_notion_priority:
            # In sync — record both sides
            self._known_priorities[jira_key] = {"notion": notion_priority, "jira": jira_as_notion_priority}
            return

        if known is None:
            # First time — record both sides, don't sync
            self._known_priorities[jira_key] = {"notion": notion_priority, "jira": jira_as_notion_priority}
            return

        notion_changed = (notion_priority != known.get("notion"))
        jira_changed = (jira_as_notion_priority != known.get("jira"))

        if notion_changed and not jira_changed:
            notion_as_jira = NOTION_TO_JIRA_PRIORITY.get(notion_priority, "Medium")
            if self.dry_run:
                log.info("[DRY-RUN] %s: Notion → Jira priority '%s'", jira_key, notion_as_jira)
            else:
                self.jira.update_priority(jira_key, notion_as_jira)
                log.info("%s: Notion → Jira priority '%s'", jira_key, notion_as_jira)
            self._known_priorities[jira_key] = {"notion": notion_priority, "jira": notion_priority}

        elif jira_changed and not notion_changed:
            if self.dry_run:
                log.info("[DRY-RUN] %s: Jira → Notion priority '%s'", jira_key, jira_as_notion_priority)
            else:
                self.notion.update_page_properties(
                    page_id, {"Priority": {"select": {"name": jira_as_notion_priority}}},
                )
                log.info("%s: Jira → Notion priority '%s'", jira_key, jira_as_notion_priority)
            self._known_priorities[jira_key] = {"notion": jira_as_notion_priority, "jira": jira_as_notion_priority}

        elif notion_changed and jira_changed:
            jira_ts = _parse_timestamp(issue.get("updated", ""))
            notion_ts = _parse_timestamp(page.get("last_edited_time", ""))
            if jira_ts and notion_ts and jira_ts > notion_ts:
                winner = jira_as_notion_priority
                if self.dry_run:
                    log.info("[DRY-RUN] %s: CONFLICT priority — Jira wins '%s'", jira_key, winner)
                else:
                    self.notion.update_page_properties(
                        page_id, {"Priority": {"select": {"name": winner}}},
                    )
                    log.info("%s: CONFLICT priority — Jira wins '%s'", jira_key, winner)
            else:
                winner = notion_priority
                notion_as_jira = NOTION_TO_JIRA_PRIORITY.get(winner, "Medium")
                if self.dry_run:
                    log.info("[DRY-RUN] %s: CONFLICT priority — Notion wins '%s'", jira_key, winner)
                else:
                    self.jira.update_priority(jira_key, notion_as_jira)
                    log.info("%s: CONFLICT priority — Notion wins '%s'", jira_key, winner)
            self._known_priorities[jira_key] = {"notion": winner, "jira": winner}

        # else: neither changed (existing discrepancy) — don't touch

    def _push_status_to_jira(self, jira_key: str, notion_status: str):
        target_jira = NOTION_TO_JIRA_STATUS.get(notion_status)
        if not target_jira:
            log.debug("%s: Notion status '%s' has no Jira mapping", jira_key, notion_status)
            return
        if self.dry_run:
            log.info("[DRY-RUN] %s: Notion → Jira status '%s'", jira_key, target_jira)
        else:
            success = self.jira.transition_issue(jira_key, target_jira)
            if success:
                log.info("%s: Notion → Jira status '%s'", jira_key, target_jira)
                self.stats["notion_to_jira"] += 1
            else:
                log.warning("%s: failed to transition Jira to '%s'", jira_key, target_jira)
                self.stats["errors"] += 1

    def _push_status_to_notion(self, page_id: str, jira_key: str, notion_status: str):
        if self.dry_run:
            log.info("[DRY-RUN] %s: Jira → Notion status '%s'", jira_key, notion_status)
        else:
            success = self.notion.update_page_properties(
                page_id, {"Status": {"status": {"name": notion_status}}},
            )
            if success:
                log.info("%s: Jira → Notion status '%s'", jira_key, notion_status)
                self.stats["jira_to_notion"] += 1
            else:
                self.stats["errors"] += 1

    def _sync_progress(self, issue: Dict[str, Any], page_id: str):
        """Sync Jira subtask progress into Notion page content and Jira delivery progress field."""
        progress = issue.get("progress", {})
        total = progress.get("total", 0)
        if total == 0:
            return

        percentage = progress["percentage"]
        emoji = get_progress_emoji(percentage)

        # Update Jira "Прогресс поставки" field (customfield_11342)
        if not self.dry_run:
            self.jira.update_delivery_progress_field(issue["key"], progress)
            log.debug("%s: delivery progress field updated to %d%%", issue["key"], int(percentage))

        result = self.notion.find_progress_block(page_id)
        if not result:
            log.debug("%s: progress block not found on page", issue["key"])
            return

        block_id, current_text = result
        expected_text = f"{emoji} {int(percentage)}%"
        if expected_text in current_text:
            return

        if self.dry_run:
            log.info("[DRY-RUN] %s: would update progress to %s", issue["key"], expected_text)
            return

        rich_text = build_progress_rich_text(emoji, percentage)
        success = self.notion.update_block_text(block_id, "paragraph", rich_text)
        if not success:
            success = self.notion.update_block_text(block_id, "callout", rich_text)

        if success:
            log.info("%s: progress updated to %s %d%%", issue["key"], emoji, int(percentage))
        else:
            log.error("%s: failed to update progress block", issue["key"])

    def _save(self):
        if self.dry_run:
            return  # Don't persist state from dry-run — mutations weren't applied
        # Read-modify-write to avoid clobbering other phases' state
        state = _load_state()
        state["known_notion_statuses"] = self._known
        state["known_notion_priorities"] = self._known_priorities
        _save_state(state)

    def _log_stats(self):
        s = self.stats
        log.info(
            "Bidirectional: checked=%d, J→N=%d, N→J=%d, conflicts=%d, skipped=%d, errors=%d",
            s["checked"], s["jira_to_notion"], s["notion_to_jira"],
            s["conflicts"], s["skipped"], s["errors"],
        )


# Backward-compatible aliases
JiraToNotionSync = BidirectionalSync


# ---- Notion → Jira Sync (deleted page detection) ----


class NotionToJiraSync:
    """Detects deleted Notion pages and archives corresponding Jira issues.

    Status/priority sync is handled by BidirectionalSync.
    This class only handles page deletion detection and known-status bookkeeping
    for pages not covered by BidirectionalSync (e.g., inactive Jira issues).
    """

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        dry_run: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.dry_run = dry_run
        self.stats = {"checked": 0, "updated": 0, "skipped": 0,
                      "skipped_orphan": 0, "errors": 0}
        self._state = _load_state()
        self._known = self._state.get("known_notion_statuses", {})
        self._orphans = OrphanResolver(STATE_FILE)

    def run(self):
        """Query all Notion pages — detect deletions, update known statuses."""
        log.info("NotionToJiraSync: checking for deleted pages")
        pages = self.notion.query_all_pages_with_jira_key()
        log.info("Found %d pages with Jira Key", len(pages))

        current_keys = set()
        for page in pages:
            self.stats["checked"] += 1
            jira_key = NotionClient.get_jira_key(page)
            if jira_key:
                current_keys.add(jira_key)
                # Update known status for pages not covered by BidirectionalSync
                # (e.g., tasks whose Jira issue is inactive/closed)
                current_status = self.notion.get_page_status(page)
                existing = self._known.get(jira_key)
                if isinstance(existing, dict):
                    # New dual format — update notion side only
                    existing["notion"] = current_status
                else:
                    # Old flat format or missing — store as dual
                    self._known[jira_key] = {"notion": current_status, "jira": current_status}
            time.sleep(0.2)

        # Backfill template sections for pages missing them
        if not self.dry_run:
            self._backfill_templates(pages)

        # Detect deleted pages and archive their Jira issues
        self._handle_deleted_pages(current_keys)

        self._save()
        self._log_stats()

    def _backfill_templates(self, pages: List[Dict[str, Any]]):
        """Add missing template sections to existing pages (one-time catch-up).

        Orphan policy: skip pages whose Jira key has just become 404.
        We use ``probe_and_resolve`` (not ``is_orphaned``) because
        NotionToJiraSync runs in Phase 4 — before Subtask↔Todo (5),
        Confluence (6), and SectionSync (7) — so on the FIRST cycle
        when a key becomes orphaned, no tombstone exists yet. Without
        a fresh probe, backfill could call ``_add_template_sections``
        (which appends Notion children) on an orphaned page, violating
        the "leave Notion/Confluence unchanged" invariant.

        The probe costs one Jira GET per backfill candidate; the set
        of candidates is bounded by the one-time `template_backfilled`
        cache, so this is at most a one-shot extra hit per new key
        and zero per cycle once everything is backfilled.
        """
        backfilled = self._state.get("template_backfilled", set())
        if isinstance(backfilled, list):
            backfilled = set(backfilled)

        for page in pages:
            jira_key = NotionClient.get_jira_key(page)
            if not jira_key or jira_key in backfilled:
                continue

            # Probe Jira so a freshly-orphaned (and not-yet-tombstoned)
            # key is caught here, in the first phase that would write
            # to Notion. probe_and_resolve marks/clears the tombstone
            # for downstream phases the same cycle.
            if self._orphans.probe_and_resolve(jira_key, self.jira):
                self.stats["skipped_orphan"] += 1
                continue

            page_id = page["id"]
            # Quick check: if MVP toggle exists, assume all sections present
            if self.notion.find_toggle_by_text(page_id, "Минимальный функционал"):
                backfilled.add(jira_key)
                continue

            # Add missing sections
            _add_template_sections(self.notion, page_id, jira_key)
            backfilled.add(jira_key)
            time.sleep(0.4)

        self._state["template_backfilled"] = list(backfilled)

    def _handle_deleted_pages(self, current_keys: set):
        """Detect pages deleted from Notion and archive their Jira issues.

        Uses a grace period: a key must be missing for 2 consecutive cycles
        before deleting, to avoid false positives from API/pagination errors.
        """
        missing = self._state.get("missing_keys", {})

        # Find keys that were known but are now missing
        for jira_key in list(self._known.keys()):
            if jira_key in current_keys:
                # Page is present — clear any missing counter
                missing.pop(jira_key, None)
                continue

            # Page is missing — track how many cycles
            miss_count = missing.get(jira_key, 0) + 1
            missing[jira_key] = miss_count

            if miss_count < 2:
                log.info(
                    "%s: Notion page missing (cycle %d/2), waiting...",
                    jira_key, miss_count,
                )
                continue

            # Missing for 2+ cycles — delete from Jira entirely.
            # First check the orphan tombstone: if Jira already lost the
            # issue (e.g. user deleted it manually), there's nothing to
            # delete — skip with a WARNING and clean local bookkeeping.
            if self._orphans.is_orphaned(jira_key):
                log.warning(
                    "%s: Notion page gone and Jira issue is already "
                    "orphaned — nothing to delete; cleaning local state",
                    jira_key,
                )
                self.stats["skipped_orphan"] += 1
                self._known.pop(jira_key, None)
                missing.pop(jira_key, None)
                continue

            if self.dry_run:
                log.info(
                    "[DRY-RUN] %s: would delete from Jira (page deleted from Notion)",
                    jira_key,
                )
            else:
                # Probe Jira directly — only call delete_issue if the
                # issue truly exists. issue_exists() returns:
                #   True  → call delete_issue
                #   False → already gone; mark orphan, clean local state
                #   None  → unknown (transient); skip this cycle, retry later
                exists = self.jira.issue_exists(jira_key)
                if exists is False:
                    self._orphans.mark_orphaned(jira_key, source="jira_404")
                    self.stats["skipped_orphan"] += 1
                    self._known.pop(jira_key, None)
                    missing.pop(jira_key, None)
                    continue
                if exists is None:
                    log.warning(
                        "%s: Jira existence probe returned None "
                        "(transient); will retry next cycle",
                        jira_key,
                    )
                    continue

                success = self.jira.delete_issue(jira_key)
                if success:
                    log.info(
                        "%s: deleted from Jira (page deleted from Notion)",
                        jira_key,
                    )
                    self.stats["updated"] += 1
                else:
                    log.warning("%s: failed to delete from Jira", jira_key)
                    self.stats["errors"] += 1

            # Clean up from known state
            self._known.pop(jira_key, None)
            missing.pop(jira_key, None)

        self._state["missing_keys"] = missing

    def _save(self):
        # Read-modify-write to avoid clobbering other phases' state
        state = _load_state()
        state["last_notion_to_jira_sync"] = datetime.now().isoformat()
        state["known_notion_statuses"] = self._known
        state["missing_keys"] = self._state.get("missing_keys", {})
        state["template_backfilled"] = self._state.get("template_backfilled", [])
        _save_state(state)

    def _log_stats(self):
        s = self.stats
        log.info(
            "Notion→Jira: checked=%d, updated=%d, skipped=%d, "
            "skipped_orphan=%d, errors=%d",
            s["checked"],
            s["updated"],
            s["skipped"],
            s.get("skipped_orphan", 0),
            s["errors"],
        )


# ---- Notion → Jira Creation ----


class NotionToJiraCreator:
    """Creates Jira issues for Notion pages that have no Jira Key."""

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        confluence: Optional[ConfluenceClient] = None,
        dry_run: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.confluence = confluence
        self.dry_run = dry_run
        self.stats = {"found": 0, "created": 0, "skipped": 0, "errors": 0}

    def run(self):
        log.info("Notion→Jira creation: finding pages without Jira Key")
        pages = self.notion.query_pages_without_jira_key()
        self.stats["found"] = len(pages)
        log.info("Found %d pages without Jira Key", len(pages))

        # Pre-fetch existing Jira titles to prevent duplicate creation
        existing_issues = self.jira.get_all_issues()
        self._existing_jira_titles = {
            i["summary"].strip().lower() for i in existing_issues
        }

        for page in pages:
            try:
                self._process_one(page)
            except Exception as e:
                self.stats["errors"] += 1
                title = NotionClient.get_page_title(page) or "?"
                log.error("Error creating Jira for '%s': %s", title, e)
            time.sleep(0.4)

        self._log_stats()

    def _process_one(self, page: Dict[str, Any]):
        title = NotionClient.get_page_title(page)
        if not title:
            self.stats["skipped"] += 1
            return

        status = self.notion.get_page_status(page)
        if status == "Archived":
            log.debug("Skipping archived: %s", title)
            self.stats["skipped"] += 1
            return

        # Guard: skip if Jira already has an issue with the same title
        if title.strip().lower() in self._existing_jira_titles:
            log.warning(
                "Skipping '%s': Jira issue with same title already exists",
                title,
            )
            self.stats["skipped"] += 1
            return

        summary = NotionClient.get_page_summary(page) or ""
        priority = NotionClient.get_page_priority(page) or "Medium"
        jira_priority = NOTION_TO_JIRA_PRIORITY.get(priority, "Medium")

        page_id = page["id"].replace("-", "")
        notion_url = f"https://notion.so/{page_id}"

        if self.dry_run:
            log.info(
                "[DRY-RUN] Would create Jira for: '%s' (priority=%s)",
                title, jira_priority,
            )
            return

        # 1. Create Jira issue (initially without confluence_url)
        result = self.jira.create_issue(
            title=title,
            description=summary,
            priority=jira_priority,
            notion_url=notion_url,
        )
        jira_key = result["key"]
        jira_url = result["url"]
        log.info("Created %s for '%s'", jira_key, title)

        # 2. Transition to match Notion status
        if status and status != "Not started":
            jira_status = NOTION_TO_JIRA_STATUS.get(status)
            if jira_status:
                ok = self.jira.transition_issue(jira_key, jira_status)
                if ok:
                    log.info("Transitioned %s to '%s'", jira_key, jira_status)
                else:
                    log.warning("Could not transition %s to '%s'", jira_key, jira_status)

        # 3. Create default subtasks
        subtask_items = DEFAULT_SUBTASKS
        if subtask_items:
            try:
                created = self.jira.create_subtasks(jira_key, subtask_items)
                log.info(
                    "Created %d subtasks for %s: %s",
                    len(created), jira_key,
                    ", ".join(s["key"] for s in created),
                )
            except Exception as e:
                log.warning("Could not create subtasks for %s: %s", jira_key, e)

        # 4. Create Confluence page (if client available)
        confluence_url = ""
        if self.confluence:
            try:
                conf_title = f"{jira_key} — {title}"
                conf_body = self.confluence.build_task_page_html(
                    jira_key=jira_key,
                    jira_url=jira_url,
                    notion_url=notion_url,
                    summary=summary,
                    subtasks=[{"title": s["title"]} for s in subtask_items] if subtask_items else None,
                )
                conf_page = self.confluence.find_or_create_page(
                    jira_key, conf_title, conf_body
                )
                if conf_page:
                    confluence_url = self.confluence.get_page_url(conf_page)
                    log.info("Confluence page for %s: %s", jira_key, confluence_url)
                    self.jira.update_description(
                        jira_key,
                        description=summary,
                        notion_url=notion_url,
                        confluence_url=confluence_url,
                    )
                    self.jira.update_confluence_url(jira_key, confluence_url)
            except Exception as e:
                log.warning("Could not create Confluence page for %s: %s", jira_key, e)

        # 5. Update Notion page — set Jira Key + update content
        success = self.notion.update_page_jira_key(page["id"], jira_key)
        if success:
            log.info("Updated Notion page with Jira Key %s", jira_key)
            self.stats["created"] += 1

            # Create or update 🔗 callout with links
            self.notion.update_links_callout(
                page["id"], jira_key, jira_url, confluence_url or None
            )

            # Add plan section if not present
            if subtask_items:
                heading_id = self.notion.find_toggle_by_text(
                    page["id"], "План выполнения"
                )
                if not heading_id:
                    self.notion.add_plan_section(page["id"], subtask_items)
                    log.info("Added plan section to Notion page for %s", jira_key)

            # Add missing template sections (MVP, Результат, Заметки, 🤖)
            self._add_template_sections(
                page["id"], jira_key, description=summary,
            )

            # Record known status (dual format for BidirectionalSync)
            if status:
                state = _load_state()
                known = state.get("known_notion_statuses", {})
                known[jira_key] = {"notion": status, "jira": status}
                state["known_notion_statuses"] = known
                _save_state(state)
        else:
            self.stats["errors"] += 1

    def _add_template_sections(self, page_id, jira_key, description=""):
        _add_template_sections(self.notion, page_id, jira_key, description)

    def _log_stats(self):
        s = self.stats
        log.info(
            "Notion→Jira creation: found=%d, created=%d, skipped=%d, errors=%d",
            s["found"], s["created"], s["skipped"], s["errors"],
        )


def _add_template_sections(
    notion: NotionClient,
    page_id: str,
    jira_key: str,
    description: str = "",
):
    """Add missing template toggle sections to an existing Notion page.

    Checks for MVP, Результат, Заметки/Лог, Описание, and 🤖 callout.
    Only adds sections that don't already exist.
    """
    # Order: Описание задачи → MVP → Заметки / Лог → Результат (снизу)
    template_toggles = [
        ("Минимальный функционал (MVP)", {
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text",
                "text": {"content": "Описать минимально необходимый результат"},
                "annotations": {"italic": True}}]},
        }),
        ("Заметки / Лог", {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text",
                "text": {"content": "Заметки по ходу работы над задачей."},
                "annotations": {"italic": True}}]},
        }),
        ("Результат", {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text",
                "text": {"content": "Описание выполненной работы (заполняется по итогу)."},
                "annotations": {"italic": True}}]},
        }),
    ]

    blocks_to_add: List[Dict[str, Any]] = []

    # Описание задачи — всегда первым
    if description:
        if not notion.find_toggle_by_text(page_id, "Описание задачи"):
            blocks_to_add.append({
                "object": "block", "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Описание задачи"}}],
                    "is_toggleable": True,
                    "children": [{"object": "block", "type": "paragraph",
                        "paragraph": {"rich_text": [
                            {"type": "text", "text": {"content": description[:2000]}}
                        ]}}],
                },
            })

    for heading_text, placeholder_child in template_toggles:
        if notion.find_toggle_by_text(page_id, heading_text):
            continue
        blocks_to_add.append({
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": heading_text}}],
                "is_toggleable": True,
                "children": [placeholder_child],
            },
        })

    page_blocks = notion.get_block_children(page_id)
    has_robot = any(
        b.get("type") == "callout"
        and b.get("callout", {}).get("icon", {}).get("emoji") == "🤖"
        for b in page_blocks
    )
    if not has_robot:
        blocks_to_add.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text",
                    "text": {"content": "Страница создана автоматически из Jira."},
                    "annotations": {"italic": True}}],
                "icon": {"type": "emoji", "emoji": "🤖"},
                "color": "gray_background",
            },
        })

    if blocks_to_add:
        ok = notion.append_children(page_id, blocks_to_add)
        if ok:
            log.info("Added %d template sections for %s", len(blocks_to_add), jira_key)
        else:
            log.warning("Failed to add template sections for %s", jira_key)

    def _log_stats(self):
        s = self.stats
        log.info(
            "Notion→Jira creation: found=%d, created=%d, skipped=%d, errors=%d",
            s["found"], s["created"], s["skipped"], s["errors"],
        )


# ---- Jira → Notion Creation ----


class JiraToNotionCreator:
    """Creates Notion pages for Jira issues that have no matching Notion page."""

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        confluence: Optional[ConfluenceClient] = None,
        dry_run: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.confluence = confluence
        self.dry_run = dry_run
        self.stats = {"checked": 0, "created": 0, "skipped": 0, "errors": 0}

    def run(self):
        # Note: orphan-key handling is not needed here. The new-issue
        # candidate list comes FROM Jira (``get_all_active``), so a
        # deleted issue can never become a new Notion page. Existing
        # Notion pages are only used to filter OUT keys we already
        # have; they never trigger a Jira fetch by orphaned key.
        log.info("Jira→Notion creation: checking for issues without Notion pages")
        existing_pages = self.notion.query_all_pages_with_jira_key()
        known_keys = set()
        known_titles = set()
        for page in existing_pages:
            key = NotionClient.get_jira_key(page)
            if key:
                known_keys.add(key)
            title = NotionClient.get_page_title(page)
            if title:
                known_titles.add(title.strip().lower())
        log.info("Notion has %d pages with Jira Key", len(known_keys))

        # Keys that were previously synced — if a Notion page was deleted,
        # the key will still be in known_statuses. Don't recreate such pages;
        # NotionToJiraSync._handle_deleted_pages will archive the Jira issue.
        previously_synced = set(_load_state().get("known_notion_statuses", {}).keys())

        issues = self.jira.get_all_active()
        log.info("Jira has %d active issues", len(issues))

        for issue in issues:
            self.stats["checked"] += 1
            if issue.get("is_subtask"):
                self.stats["skipped"] += 1
                continue
            if issue["key"] in known_keys:
                self.stats["skipped"] += 1
                continue
            # Guard: don't recreate pages that were deliberately deleted
            if issue["key"] in previously_synced:
                log.info(
                    "Skipping %s: was previously synced, Notion page likely deleted",
                    issue["key"],
                )
                self.stats["skipped"] += 1
                continue
            # Guard: skip if Notion already has a page with the same title
            if issue["summary"].strip().lower() in known_titles:
                log.warning(
                    "Skipping %s: Notion page with title '%s' already exists",
                    issue["key"], issue["summary"],
                )
                self.stats["skipped"] += 1
                continue
            try:
                self._create_notion_page(issue)
            except Exception as e:
                self.stats["errors"] += 1
                log.error(
                    "Error creating Notion for %s: %s", issue.get("key", "?"), e
                )
            time.sleep(0.4)

        self._log_stats()

    def _create_notion_page(self, issue: Dict[str, Any]):
        jira_key = issue["key"]
        title = issue["summary"]
        jira_status = issue["status"]
        notion_status = JIRA_TO_NOTION_STATUS.get(jira_status, "Not started")
        jira_priority = issue.get("priority", "Medium")
        notion_priority = JIRA_TO_NOTION_PRIORITY.get(jira_priority, "Medium")
        description = issue.get("description", "")

        if self.dry_run:
            log.info(
                "[DRY-RUN] Would create Notion page for %s: '%s'",
                jira_key, title,
            )
            return

        # Create Confluence page first (to have URL for the template)
        confluence_url = ""
        if self.confluence:
            try:
                page_id_clean = ""  # No Notion page yet
                conf_title = f"{jira_key} — {title}"
                conf_body = self.confluence.build_task_page_html(
                    jira_key=jira_key,
                    jira_url=issue.get("url", ""),
                    notion_url="",  # Will be updated later
                    summary=description,
                    subtasks=issue.get("subtasks"),
                )
                conf_page = self.confluence.find_or_create_page(
                    jira_key, conf_title, conf_body
                )
                if conf_page:
                    confluence_url = self.confluence.get_page_url(conf_page)
                    log.info("Confluence page for %s: %s", jira_key, confluence_url)
            except Exception as e:
                log.warning("Could not create Confluence page for %s: %s", jira_key, e)

        children = self._build_content(issue, confluence_url=confluence_url)
        page = self.notion.create_page(
            title=title,
            status=notion_status,
            summary=description[:200] if description else "",
            jira_key=jira_key,
            priority=notion_priority,
            jira_url=issue.get("url", ""),
            children=children,
        )
        if page:
            log.info("Created Notion page for %s: '%s'", jira_key, title)
            self.stats["created"] += 1

            # Update Confluence with Notion URL
            if self.confluence and confluence_url:
                notion_page_id = page["id"].replace("-", "")
                notion_url = f"https://notion.so/{notion_page_id}"
                try:
                    conf_page = self.confluence.find_page_by_jira_key(jira_key)
                    if conf_page:
                        ver = conf_page.get("version", {}).get("number", 1)
                        full = self.confluence.get_page(conf_page["id"])
                        if full:
                            body = full["body"]["storage"]["value"]
                            body = body.replace(
                                '>Notion</a>',
                                f' href="{notion_url}">Notion</a>',
                            )
                            self.confluence.update_page(
                                conf_page["id"], conf_page["title"], body, ver
                            )
                except Exception as e:
                    log.warning("Could not update Confluence Notion link for %s: %s", jira_key, e)

                # Update Jira description with Confluence link
                self.jira.update_description(
                    jira_key,
                    description=description,
                    notion_url=f"https://notion.so/{page['id'].replace('-', '')}",
                    confluence_url=confluence_url,
                )
                self.jira.update_confluence_url(jira_key, confluence_url)

            state = _load_state()
            known = state.get("known_notion_statuses", {})
            known[jira_key] = {"notion": notion_status, "jira": notion_status}
            state["known_notion_statuses"] = known
            _save_state(state)
        else:
            self.stats["errors"] += 1

    @staticmethod
    def _build_content(
        issue: Dict[str, Any],
        confluence_url: str = "",
    ) -> List[Dict]:
        """Build Notion page content with new template order.

        Order:
        1. Toggle "План выполнения" with to-do checkboxes (FIRST)
        2. Callout 🔗 with Jira + Confluence hyperlinks
        3. Divider
        4. Toggle "Минимальный функционал (MVP)"
        5. Toggle "Результат"
        6. Toggle "Заметки / Лог"
        7. Toggle "Описание задачи" (if description present)
        8. Callout 🤖 "Создано автоматически"
        """
        jira_key = issue["key"]
        jira_url = issue.get("url", "")
        description = issue.get("description", "")
        subtasks = issue.get("subtasks", [])

        blocks: List[Dict[str, Any]] = []

        # 1. Plan section (FIRST for quick access)
        plan_items = subtasks if subtasks else DEFAULT_SUBTASKS
        todo_children = []
        for item in plan_items:
            item_title = item.get("summary", item.get("title", ""))
            item_done = item.get("status", "").lower() in ("done", "готово", "closed", "resolved")
            todo_children.append({
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [
                        {"type": "text", "text": {"content": item_title}}
                    ],
                    "checked": item_done,
                },
            })

        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "План выполнения"}}
                ],
                "is_toggleable": True,
                "children": todo_children,
            },
        })

        # 2. Links callout with hyperlinks
        links_rt: List[Dict[str, Any]] = [
            {"type": "text", "text": {"content": "Jira: "}, "annotations": {"bold": True}},
            {"type": "text", "text": {"content": jira_key, "link": {"url": jira_url} if jira_url else None}},
        ]
        if confluence_url:
            links_rt.append({"type": "text", "text": {"content": " | "}})
            links_rt.append(
                {"type": "text", "text": {"content": "Confluence: "}, "annotations": {"bold": True}}
            )
            links_rt.append(
                {"type": "text", "text": {"content": "ТЗ", "link": {"url": confluence_url}}}
            )

        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": links_rt,
                "icon": {"type": "emoji", "emoji": "🔗"},
                "color": "purple_background",
            },
        })

        # 3. Divider
        blocks.append({"object": "block", "type": "divider", "divider": {}})

        # 4. MVP section
        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "Минимальный функционал (MVP)"}}
                ],
                "is_toggleable": True,
                "children": [{
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": "Описать минимально необходимый результат"},
                            "annotations": {"italic": True},
                        }],
                    },
                }],
            },
        })

        # 5. Result section
        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "Результат"}}
                ],
                "is_toggleable": True,
                "children": [{
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": "Описание выполненной работы (заполняется по итогу)."},
                            "annotations": {"italic": True},
                        }],
                    },
                }],
            },
        })

        # 6. Notes / Log section
        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "Заметки / Лог"}}
                ],
                "is_toggleable": True,
                "children": [{
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": "Заметки по ходу работы над задачей."},
                            "annotations": {"italic": True},
                        }],
                    },
                }],
            },
        })

        # 7. Description (if present)
        if description:
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Описание задачи"}}
                    ],
                    "is_toggleable": True,
                    "children": [{
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"type": "text", "text": {"content": description[:2000]}}
                            ],
                        },
                    }],
                },
            })

        # 6. Auto-created note
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": "Страница создана автоматически из Jira."},
                    "annotations": {"italic": True},
                }],
                "icon": {"type": "emoji", "emoji": "🤖"},
                "color": "gray_background",
            },
        })

        return blocks

    def _log_stats(self):
        s = self.stats
        log.info(
            "Jira→Notion creation: checked=%d, created=%d, skipped=%d, errors=%d",
            s["checked"], s["created"], s["skipped"], s["errors"],
        )


# ---- Subtask ↔ Todo Sync ----


class SubtaskTodoSync:
    """True three-way sync: Notion to-dos ↔ Jira subtasks ↔ Confluence tasks.

    All three sources are equal peers. On each cycle:
    1. Read current state from all three
    2. Build unified item list (matched by title)
    3. For each item: detect who changed (delta vs known state),
       resolve conflicts by last-edit timestamp, propagate to others
    4. All creates + updates happen in a single cycle
    """

    PLAN_HEADING = "План выполнения"
    DONE_STATUSES = frozenset({"done", "готово", "closed", "resolved"})

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        confluence: Optional["ConfluenceClient"] = None,
        dry_run: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.confluence = confluence
        self.dry_run = dry_run
        self.stats = {
            "pages_checked": 0, "todos_synced": 0,
            "subtasks_created": 0, "subtasks_deleted": 0,
            "todos_created": 0,
            "checked_updated": 0, "skipped_orphan": 0, "errors": 0,
        }
        self._state = _load_state()
        self._known = self._state.get("subtask_todos", {})
        self._orphans = OrphanResolver(STATE_FILE)

    def run(self):
        log.info("Subtask↔Todo sync: starting (three-way)")
        pages = self.notion.query_all_pages_with_jira_key()
        log.info("Found %d pages with Jira Key", len(pages))

        for page in pages:
            jira_key = NotionClient.get_jira_key(page)
            if not jira_key:
                continue
            self.stats["pages_checked"] += 1

            # Skip orphaned keys before any Jira fetch — avoids the
            # 404 → exception → ERROR-log cycle. Notion / Confluence
            # are left untouched; status reconciliation is up to the
            # operator (see tests/manual/diagnose_orphan_jira_key.py).
            if self._orphans.probe_and_resolve(jira_key, self.jira):
                self.stats["skipped_orphan"] += 1
                continue

            try:
                self._sync_page(page, jira_key)
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Subtask sync error for %s: %s", jira_key, e, exc_info=True)
            time.sleep(0.4)

        self._save()
        self._log_stats()

    # ---- Main sync logic (per page) ----

    def _sync_page(self, page: Dict[str, Any], jira_key: str):
        page_id = page["id"]
        last_edited = page.get("last_edited_time", "")
        known_page = self._known.get(jira_key, {})

        # Migrate old known state format → treat as empty
        if "todos" in known_page and "items" not in known_page:
            known_page = {}

        # ── Step 1: Read all 3 sources ──────────────────────────

        subtasks = self.jira.get_subtask_details(jira_key)

        conf_tasks: List[Dict] = []
        conf_page: Optional[Dict] = None
        conf_body: Optional[str] = None
        conf_version: Optional[int] = None
        conf_when: str = ""
        if self.confluence:
            conf_page = self.confluence.find_page_by_jira_key(jira_key)
            if conf_page:
                full = self.confluence.get_page_with_version(conf_page["id"])
                if full:
                    conf_body = full["body"]["storage"]["value"]
                    conf_version = full["version"]["number"]
                    conf_when = full["version"].get("when", "")
                    plan_html = ConfluenceClient.extract_section(
                        conf_body, self.PLAN_HEADING
                    )
                    if plan_html:
                        conf_tasks = ConfluenceClient.parse_task_list(plan_html)

        # Nothing anywhere → skip
        if not subtasks and not conf_tasks:
            return

        # ── Step 1b: Early exit if nothing changed ──────────────

        jira_changed = self._jira_subtasks_changed(jira_key, subtasks, known_page)
        conf_changed = self._confluence_tasks_changed(jira_key, conf_tasks, known_page)
        notion_changed = last_edited != known_page.get("page_last_edited", "")

        if not jira_changed and not conf_changed and not notion_changed:
            return

        # ── Step 2: Ensure plan heading exists in Notion ────────

        heading_id = self.notion.find_toggle_by_text(page_id, self.PLAN_HEADING)

        if not heading_id:
            # Bootstrap: create plan section from whatever exists
            source_items = subtasks or [
                {"summary": ct["text"], "is_done": ct["checked"]}
                for ct in conf_tasks
            ]
            if source_items:
                self._add_plan_section(page_id, jira_key, source_items)
            # Record baseline state, sync on next cycle
            self._save_known_state(
                jira_key, last_edited, conf_when, subtasks, [], conf_tasks
            )
            return

        # Read Notion to-dos
        todos = self.notion.get_todo_children(heading_id)

        # ── Step 3: Build unified items + resolve ───────────────

        unified = self._build_unified_items(todos, subtasks, conf_tasks)
        known_items = known_page.get("items", {})
        timestamps = {
            "notion": last_edited,
            "conf": conf_when,
        }

        actions: List[tuple] = []
        for item in unified:
            item_actions = self._resolve_item(
                jira_key, item, known_items, timestamps, heading_id,
            )
            actions.extend(item_actions)

        # ── Step 4: Execute all actions ─────────────────────────

        if actions:
            self._execute_actions(
                jira_key, actions, heading_id,
                conf_page, conf_body, conf_version, unified,
            )
        # No elif: if no actions, don't touch Confluence — avoids
        # unnecessary page versions and hidden Jira priority fallback.

        # Update progress bar
        if not self.dry_run:
            self.jira.update_delivery_progress_field(jira_key)

        # ── Step 5: Save known state ────────────────────────────

        # Re-read all sources to get fresh state after actions.
        # Without this, _save_known_state records stale pre-action data,
        # causing false deltas on the next cycle.
        if actions:
            subtasks = self.jira.get_subtask_details(jira_key)
            todos = self.notion.get_todo_children(heading_id)
            if self.confluence and conf_page:
                full = self.confluence.get_page_with_version(conf_page["id"])
                if full:
                    conf_body_fresh = full["body"]["storage"]["value"]
                    conf_when = full["version"].get("when", "")
                    plan_html = ConfluenceClient.extract_section(
                        conf_body_fresh, self.PLAN_HEADING,
                    )
                    if plan_html:
                        conf_tasks = ConfluenceClient.parse_task_list(plan_html)

            # Refresh Notion baseline only when this item actually had
            # Notion-mutating actions. Otherwise the page wasn't touched
            # and the prior last_edited is already correct — no need to
            # spend an extra GET /pages/{id}.
            if self._actions_mutate_notion(actions):
                last_edited = self._refresh_notion_last_edited(
                    page_id, last_edited,
                )

        self._save_known_state(
            jira_key, last_edited, conf_when, subtasks, todos, conf_tasks,
        )

    # ---- Unified item builder ----

    @staticmethod
    def _build_unified_items(
        todos: List[Dict],
        subtasks: List[Dict],
        conf_tasks: List[Dict],
    ) -> List[Dict]:
        """Match items across all 3 sources by title (case-insensitive).

        Returns: [{"title", "match_key", "notion", "jira", "conf"}, ...]
        """
        from collections import OrderedDict
        items: Dict[str, Dict] = OrderedDict()

        # Jira subtasks first (most stable identifiers)
        for st in subtasks:
            mk = st["summary"].strip().lower()
            items[mk] = {
                "title": st["summary"].strip(),
                "match_key": mk,
                "notion": None,
                "jira": st,
                "conf": None,
            }

        # Notion to-dos
        for todo in todos:
            mk = todo["text"].strip().lower()
            if mk in items:
                items[mk]["notion"] = todo
            else:
                items[mk] = {
                    "title": todo["text"].strip(),
                    "match_key": mk,
                    "notion": todo,
                    "jira": None,
                    "conf": None,
                }

        # Confluence tasks
        for ct in conf_tasks:
            mk = ct["text"].strip().lower()
            if mk in items:
                items[mk]["conf"] = ct
            else:
                items[mk] = {
                    "title": ct["text"].strip(),
                    "match_key": mk,
                    "notion": None,
                    "jira": None,
                    "conf": ct,
                }

        return list(items.values())

    # ---- Per-item three-way resolution ----

    def _resolve_item(
        self,
        jira_key: str,
        item: Dict,
        known_items: Dict,
        timestamps: Dict,
        heading_id: str,
    ) -> List[tuple]:
        """Determine actions for a single unified item.

        Returns list of action tuples to execute.
        """
        mk = item["match_key"]
        known = known_items.get(mk)  # Previous cycle state, or None

        notion = item["notion"]
        jira = item["jira"]
        conf = item["conf"]

        actions: List[tuple] = []

        # ── New item (no known state) ───────────────────────────
        # First time seeing this item: ONLY create in missing sources.
        # NEVER change existing checked states — we don't know who is
        # "right" without a previous baseline to compare against.
        # The current state becomes the baseline for next cycle.

        if not known:
            # Determine checked from whichever source(s) already exist
            checked = self._pick_initial_checked(notion, jira, conf)

            if not jira:
                actions.append(("create_subtask", item["title"], checked))
            if not notion:
                actions.append(("create_todo", item["title"], checked, heading_id))
            if not conf:
                actions.append(("set_conf_item", item["title"], checked))

            # DO NOT align existing sources — just record baseline
            return actions

        # ── Known item — check for missing sources ──────────────

        # Item disappeared from Jira (deleted subtask) → don't recreate,
        # just clean from known state
        if not jira and known.get("jira_checked") is not None:
            return [("remove_from_known", mk)]

        # Create in missing sources
        if not jira:
            checked = self._current_checked(notion, conf)
            actions.append(("create_subtask", item["title"], checked))
        if not notion:
            checked = self._current_checked(jira, conf)
            actions.append(("create_todo", item["title"], checked, heading_id))
        if not conf:
            checked = self._current_checked(notion, jira)
            actions.append(("set_conf_item", item["title"], checked))

        # ── Checkbox state resolution ───────────────────────────

        current = {}
        if notion:
            current["notion"] = notion["checked"]
        if jira:
            current["jira"] = jira["is_done"]
        if conf:
            current["conf"] = conf["checked"]

        # All agree → in sync
        values = set(current.values())
        if len(values) <= 1:
            self.stats["todos_synced"] += 1
            return actions

        # Build previous-cycle values
        prev = {}
        if known.get("notion_checked") is not None and notion:
            prev["notion"] = known["notion_checked"]
        if known.get("jira_checked") is not None and jira:
            prev["jira"] = known["jira_checked"]
        if known.get("conf_checked") is not None and conf:
            prev["conf"] = known["conf_checked"]

        # Which sources changed vs known?
        changed = {
            src for src in current
            if src in prev and current[src] != prev[src]
        }

        # If no source shows a delta — don't touch anything.
        # This happens when prev is incomplete (migration, new source added)
        # or when values diverge but nothing changed vs last known.
        # Safe default: record current state, resolve on next cycle.
        if not changed:
            return actions
        else:
            winner_value = self._resolve_conflict(
                jira_key, item["title"], changed, current, timestamps,
                jira_updated=jira.get("updated", "") if jira else "",
            )

        # Propagate winner to all sources that disagree
        if notion and notion["checked"] != winner_value:
            if winner_value:
                actions.append(("check_todo", notion))
            else:
                actions.append(("uncheck_todo", notion))

        if jira and jira["is_done"] != winner_value:
            if winner_value:
                actions.append(("close_subtask", jira))
            else:
                actions.append(("reopen_subtask", jira))

        if conf and conf["checked"] != winner_value:
            actions.append(("set_conf_item", item["title"], winner_value))

        return actions

    def _resolve_conflict(
        self,
        jira_key: str,
        title: str,
        changed: set,
        current: Dict[str, bool],
        timestamps: Dict,
        jira_updated: str,
    ) -> bool:
        """Resolve which checked value wins.

        Rules:
        1. Single source changed → it wins
        2. Multiple changed to same value → that value wins
        3. Multiple changed to different values → latest timestamp wins
        """
        if len(changed) == 1:
            src = next(iter(changed))
            log.info("%s: '%s' — %s changed → propagating", jira_key, title, src)
            return current[src]

        changed_values = {current[src] for src in changed}
        if len(changed_values) == 1:
            val = changed_values.pop()
            log.info(
                "%s: '%s' — %s all changed to %s",
                jira_key, title, "+".join(changed), val,
            )
            return val

        # True conflict — timestamp wins
        ts_map: Dict[str, Optional[datetime]] = {}
        for src in changed:
            if src == "jira" and jira_updated:
                ts_map[src] = _parse_timestamp(jira_updated)
            elif src == "notion" and timestamps.get("notion"):
                ts_map[src] = _parse_timestamp(timestamps["notion"])
            elif src == "conf" and timestamps.get("conf"):
                ts_map[src] = _parse_timestamp(timestamps["conf"])

        winner_src = None
        winner_ts = None
        for src, ts in ts_map.items():
            if ts and (winner_ts is None or ts > winner_ts):
                winner_ts = ts
                winner_src = src

        if winner_src:
            log.info(
                "%s: CONFLICT '%s' — %s wins (ts=%s)",
                jira_key, title, winner_src,
                winner_ts.isoformat() if winner_ts else "?",
            )
            return current[winner_src]

        # Fallback: Jira wins (most reliable timestamps)
        if "jira" in changed:
            return current["jira"]
        return current[next(iter(changed))]

    @staticmethod
    def _pick_initial_checked(
        notion: Optional[Dict],
        jira: Optional[Dict],
        conf: Optional[Dict],
    ) -> bool:
        """Pick checked state for brand-new item from available sources."""
        vals = []
        if jira is not None:
            vals.append(("jira", jira["is_done"]))
        if notion is not None:
            vals.append(("notion", notion["checked"]))
        if conf is not None:
            vals.append(("conf", conf["checked"]))

        if not vals:
            return False
        if len(vals) == 1:
            return vals[0][1]

        checked_vals = {v[1] for v in vals}
        if len(checked_vals) == 1:
            return checked_vals.pop()

        # Disagreement → Jira wins
        for src, val in vals:
            if src == "jira":
                return val
        return vals[0][1]

    @staticmethod
    def _current_checked(
        *sources: Optional[Dict],
    ) -> bool:
        """Get checked state from the first available source."""
        for s in sources:
            if s is None:
                continue
            if "is_done" in s:
                return s["is_done"]
            if "checked" in s:
                return s["checked"]
        return False

    # ---- Action execution ----

    def _execute_actions(
        self,
        jira_key: str,
        actions: List[tuple],
        heading_id: str,
        conf_page: Optional[Dict],
        conf_body: Optional[str],
        conf_version: Optional[int],
        unified: List[Dict],
    ):
        """Execute collected actions. Confluence updates are batched."""
        conf_overrides: Dict[str, bool] = {}  # title → checked

        for action in actions:
            op = action[0]

            if op == "create_subtask":
                _, title, checked = action
                self._do_create_subtask(jira_key, title, checked)

            elif op == "create_todo":
                _, title, checked, parent_id = action
                self._do_create_todo(parent_id, jira_key, title, checked)

            elif op == "set_conf_item":
                _, title, checked = action
                conf_overrides[title] = checked

            elif op == "check_todo":
                _, todo = action
                self._check_todo(todo, jira_key)

            elif op == "uncheck_todo":
                _, todo = action
                self._uncheck_todo(todo, jira_key)

            elif op == "close_subtask":
                _, subtask = action
                self._close_subtask(jira_key, subtask)

            elif op == "reopen_subtask":
                _, subtask = action
                self._reopen_subtask(jira_key, subtask)

            elif op == "remove_from_known":
                pass  # Handled in _save_known_state

        # Batch Confluence update — only if there are actual Confluence changes
        if conf_overrides and self.confluence and conf_page and conf_body is not None:
            self._update_confluence_from_unified(
                jira_key, conf_page, conf_body, conf_version,
                unified, conf_overrides,
            )

    # ---- Confluence batch update ----

    def _update_confluence_from_unified(
        self,
        jira_key: str,
        conf_page: Dict,
        conf_body: str,
        conf_version: int,
        unified: List[Dict],
        overrides: Dict[str, bool],
    ):
        """Rebuild Confluence task-list from unified state + overrides."""
        if self.dry_run:
            return

        # Build items: prefer override (resolved action), then keep current
        # Confluence state, then fall back to jira/notion for new items.
        # Preserve existing UUIDs to avoid spurious page versions.
        items = []
        for u in unified:
            title = u["title"]
            if title in overrides:
                checked = overrides[title]
            elif u["conf"]:
                checked = u["conf"]["checked"]
            elif u["jira"]:
                checked = u["jira"]["is_done"]
            elif u["notion"]:
                checked = u["notion"]["checked"]
            else:
                checked = False
            item_dict = {"text": title, "checked": checked}
            # Preserve UUID from existing Confluence task to avoid regenerating
            if u["conf"] and u["conf"].get("uuid"):
                item_dict["uuid"] = u["conf"]["uuid"]
            items.append(item_dict)

        if not items:
            return

        new_task_html = ConfluenceClient.build_task_list_html(items)
        new_body = ConfluenceClient.replace_section(
            conf_body, self.PLAN_HEADING, new_task_html,
        )
        if new_body != conf_body:
            self.confluence.update_page(
                conf_page["id"], conf_page["title"], new_body, conf_version,
            )
            log.info("%s: updated Confluence plan (%d tasks)", jira_key, len(items))

    # ---- Change detection ----

    @staticmethod
    def _jira_subtasks_changed(
        jira_key: str, subtasks: List[Dict], known_page: Dict,
    ) -> bool:
        """Check if Jira subtask statuses changed since last cycle."""
        known_items = known_page.get("items", {})
        if not known_items:
            return bool(subtasks)
        for st in subtasks:
            mk = st["summary"].strip().lower()
            ki = known_items.get(mk, {})
            if ki.get("jira_checked") != st["is_done"]:
                return True
        # New subtasks added?
        known_jira_count = sum(
            1 for v in known_items.values() if v.get("jira_checked") is not None
        )
        if len(subtasks) != known_jira_count:
            return True
        return False

    @staticmethod
    def _confluence_tasks_changed(
        jira_key: str, conf_tasks: List[Dict], known_page: Dict,
    ) -> bool:
        """Check if Confluence task-list changed since last cycle."""
        if not conf_tasks:
            return False
        known_items = known_page.get("items", {})
        if not known_items:
            return True
        for ct in conf_tasks:
            mk = ct["text"].strip().lower()
            ki = known_items.get(mk, {})
            if ki.get("conf_checked") != ct["checked"]:
                return True
        known_conf_count = sum(
            1 for v in known_items.values() if v.get("conf_checked") is not None
        )
        if len(conf_tasks) != known_conf_count:
            return True
        return False

    # ---- Known state persistence ----

    # Action ops that mutate Notion (and bump page.last_edited_time
    # on the server). After any of these, the saved baseline must be
    # refreshed via _refresh_notion_last_edited so the next cycle
    # doesn't see a false notion_changed delta.
    _NOTION_MUTATING_OPS = {"create_todo", "check_todo", "uncheck_todo"}

    @classmethod
    def _actions_mutate_notion(cls, actions: List[tuple]) -> bool:
        """Return True if any action in the list mutates Notion."""
        return any(
            (a and a[0] in cls._NOTION_MUTATING_OPS) for a in actions
        )

    def _refresh_notion_last_edited(
        self, page_id: str, previous_last_edited: str,
    ) -> str:
        """Refetch page metadata to get the server-side last_edited_time.

        Called after own writes to Notion so the saved baseline
        reflects the timestamp the server recorded for our PATCH.
        Without this, next cycle reads page.last_edited_time
        (already advanced by us), compares to a stale baseline, and
        falsely concludes "Notion changed".

        On any failure (network error, 404, missing field) returns the
        previous timestamp and logs a warning — at most one noisy
        delta on the next cycle, never a sync crash.
        """
        try:
            page = self.notion.get_page(page_id)
        except Exception as e:
            log.warning(
                "Failed to refresh Notion baseline for %s: %s — "
                "keeping old timestamp",
                page_id, e,
            )
            return previous_last_edited

        if not page:
            log.warning(
                "Notion get_page returned no page for %s — "
                "keeping old timestamp",
                page_id,
            )
            return previous_last_edited

        fresh = page.get("last_edited_time")
        if not fresh:
            log.warning(
                "Notion page %s has no last_edited_time — "
                "keeping old timestamp",
                page_id,
            )
            return previous_last_edited

        return fresh

    def _save_known_state(
        self,
        jira_key: str,
        last_edited: str,
        conf_when: str,
        subtasks: List[Dict],
        todos: List[Dict],
        conf_tasks: List[Dict],
    ):
        """Save resolved state for next cycle's delta detection."""
        # Build match-key index for each source
        jira_by_mk = {st["summary"].strip().lower(): st for st in subtasks}
        todo_by_mk = {t["text"].strip().lower(): t for t in todos}
        conf_by_mk = {ct["text"].strip().lower(): ct for ct in conf_tasks}

        # Collect all match keys
        all_keys = set(jira_by_mk) | set(todo_by_mk) | set(conf_by_mk)

        items = {}
        for mk in all_keys:
            jira_st = jira_by_mk.get(mk)
            todo = todo_by_mk.get(mk)
            conf = conf_by_mk.get(mk)
            items[mk] = {
                "notion_checked": todo["checked"] if todo else None,
                "jira_checked": jira_st["is_done"] if jira_st else None,
                "conf_checked": conf["checked"] if conf else None,
                "jira_key": jira_st["key"] if jira_st else None,
            }

        self._known[jira_key] = {
            "page_last_edited": last_edited,
            "conf_version_when": conf_when,
            "items": items,
        }

    # ---- Action helpers ----

    def _add_plan_section(
        self, page_id: str, jira_key: str, subtasks: List[Dict]
    ):
        """Add plan section to page from existing Jira subtasks."""
        items = [
            {
                "title": st.get("summary", st.get("text", "")),
                "checked": st.get("is_done", st.get("checked", False)),
            }
            for st in subtasks
        ]
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would add plan section to %s with %d items",
                jira_key, len(items),
            )
            return

        heading_id = self.notion.add_plan_section(page_id, items)
        if heading_id:
            log.info(
                "Added plan section to %s with %d items", jira_key, len(items)
            )
            self.stats["todos_created"] += len(items)
        else:
            self.stats["errors"] += 1

    def _do_create_subtask(self, jira_key: str, title: str, checked: bool):
        """Create a Jira subtask from title + checked state."""
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would create subtask '%s' for %s", title, jira_key,
            )
            return
        try:
            created = self.jira.create_subtasks(
                jira_key, [{"title": title}]
            )
            if created:
                log.info(
                    "%s: created subtask %s for '%s'",
                    jira_key, created[0]["key"], title,
                )
                self.stats["subtasks_created"] += 1
                if checked:
                    self.jira.transition_issue(created[0]["key"], "Готово")
        except Exception as e:
            log.warning(
                "%s: could not create subtask for '%s': %s",
                jira_key, title, e,
            )
            self.stats["errors"] += 1

    def _do_create_todo(
        self, heading_id: str, jira_key: str, title: str, checked: bool,
    ):
        """Create a Notion to-do from title + checked state."""
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would create to-do '%s' for %s", title, jira_key,
            )
            return
        block_id = self.notion.create_todo_block(
            heading_id, title, checked=checked,
        )
        if block_id:
            log.info("%s: created to-do '%s'", jira_key, title)
            self.stats["todos_created"] += 1
        else:
            self.stats["errors"] += 1

    def _close_subtask(self, jira_key: str, subtask: Dict):
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would close %s (%s)",
                subtask["key"], subtask["summary"],
            )
            return
        ok = self.jira.transition_issue(subtask["key"], "Готово")
        if ok:
            log.info("%s: closed subtask %s", jira_key, subtask["key"])
            self.stats["checked_updated"] += 1
        else:
            log.warning(
                "%s: could not close subtask %s", jira_key, subtask["key"]
            )
            self.stats["errors"] += 1

    def _reopen_subtask(self, jira_key: str, subtask: Dict):
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would reopen %s (%s)",
                subtask["key"], subtask["summary"],
            )
            return
        ok = self.jira.transition_issue(subtask["key"], "Новое")
        if ok:
            log.info("%s: reopened subtask %s", jira_key, subtask["key"])
            self.stats["checked_updated"] += 1
        else:
            log.warning(
                "%s: could not reopen subtask %s", jira_key, subtask["key"]
            )
            self.stats["errors"] += 1

    def _check_todo(self, todo: Dict, jira_key: str):
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would check '%s' for %s", todo["text"], jira_key,
            )
            return
        if self.notion.update_todo_checked(todo["id"], True):
            log.info("%s: checked '%s'", jira_key, todo["text"])
            self.stats["checked_updated"] += 1
        else:
            self.stats["errors"] += 1

    def _uncheck_todo(self, todo: Dict, jira_key: str):
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would uncheck '%s' for %s", todo["text"], jira_key,
            )
            return
        if self.notion.update_todo_checked(todo["id"], False):
            log.info("%s: unchecked '%s'", jira_key, todo["text"])
            self.stats["checked_updated"] += 1
        else:
            self.stats["errors"] += 1

    def _save(self):
        # Read-modify-write so we don't clobber buckets that other
        # components (OrphanResolver, other phases) wrote during this
        # cycle. The previous version wrote self._state wholesale,
        # erasing tombstones added mid-cycle by orphan_keys.
        state = _load_state()
        state["subtask_todos"] = self._known
        _save_state(state)

    def _log_stats(self):
        s = self.stats
        log.info(
            "Subtask↔Todo: pages=%d, synced=%d, "
            "subtasks_created=%d, todos_created=%d, "
            "checked_updated=%d, skipped_orphan=%d, errors=%d",
            s["pages_checked"], s["todos_synced"],
            s["subtasks_created"],
            s["todos_created"], s["checked_updated"],
            s.get("skipped_orphan", 0), s["errors"],
        )


# ---- Confluence Sync ----


class ConfluenceSync:
    """Creates/updates Confluence pages and syncs progress from Jira subtasks."""

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        confluence: ConfluenceClient,
        dry_run: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.confluence = confluence
        self.dry_run = dry_run
        self.stats = {"checked": 0, "created": 0, "updated": 0, "linked": 0,
                      "skipped": 0, "skipped_orphan": 0, "errors": 0}
        self._state = _load_state()
        self._linked_keys = set(self._state.get("confluence_linked_keys", []))
        self._orphans = OrphanResolver(STATE_FILE)

    def run(self):
        log.info("Confluence sync: starting")
        pages = self.notion.query_all_pages_with_jira_key()
        log.info("Found %d pages with Jira Key", len(pages))

        for page in pages:
            jira_key = NotionClient.get_jira_key(page)
            if not jira_key:
                continue
            self.stats["checked"] += 1

            # Skip orphaned keys (Jira issue gone) — probe once per key
            # per phase, mark/clear tombstone, then read tombstone state.
            # WARN, not ERROR: this is a known reconciliation gap, not
            # a sync failure. Notion / Confluence are left untouched.
            if self._orphans.probe_and_resolve(jira_key, self.jira):
                self.stats["skipped_orphan"] += 1
                continue

            try:
                self._sync_page(page, jira_key)
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Confluence sync error for %s: %s", jira_key, e)
            time.sleep(0.4)

        self._save()
        self._log_stats()

    def _sync_page(self, page: Dict[str, Any], jira_key: str):
        page_id = page["id"]
        title = NotionClient.get_page_title(page) or jira_key
        notion_page_id = page_id.replace("-", "")
        notion_url = f"https://notion.so/{notion_page_id}"

        # Get Jira info
        issue = self.jira.get_issue(jira_key)
        jira_url = issue.get("url", f"{self.jira.server}/browse/{jira_key}")

        # Find or create Confluence page
        conf_page = self.confluence.find_page_by_jira_key(jira_key)

        if not conf_page:
            summary = NotionClient.get_page_summary(page) or ""
            subtasks = self.jira.get_subtask_details(jira_key)

            conf_title = f"{jira_key} — {title}"
            conf_body = self.confluence.build_task_page_html(
                jira_key=jira_key,
                jira_url=jira_url,
                notion_url=notion_url,
                summary=summary,
                subtasks=subtasks if subtasks else None,
            )

            if self.dry_run:
                log.info("[DRY-RUN] Would create Confluence page for %s", jira_key)
                return

            conf_page = self.confluence.find_or_create_page(
                jira_key, conf_title, conf_body
            )
            if not conf_page:
                self.stats["errors"] += 1
                return
            self.stats["created"] += 1

        # Ensure links are set up (Notion callout, Jira description, Confluence body)
        confluence_url = self.confluence.get_page_url(conf_page)

        if jira_key not in self._linked_keys:
            if self.dry_run:
                log.info("[DRY-RUN] Would set up links for %s", jira_key)
            else:
                # Add links section to Confluence page if missing.
                # IMPORTANT: only prepend the links block — never replace
                # the entire body, as user content must be preserved.
                full = self.confluence.get_page(conf_page["id"])
                if full:
                    body = full["body"]["storage"]["value"]
                    if "Ссылки" not in body:
                        links_html = self._build_links_section(
                            jira_key, jira_url, notion_url,
                        )
                        new_body = links_html + "\n" + body
                        version = full["version"]["number"]
                        self.confluence.update_page(
                            conf_page["id"], conf_page["title"], new_body, version
                        )

                # Update Notion callout with Confluence link
                self.notion.update_links_callout(
                    page_id, jira_key, jira_url, confluence_url
                )

                # Update Jira description with links
                # Use Notion summary as description (not current Jira desc which
                # may contain stale link text extracted from ADF).
                summary_text = NotionClient.get_page_summary(page) or ""
                self.jira.update_description(
                    jira_key,
                    description=summary_text,
                    notion_url=notion_url,
                    confluence_url=confluence_url,
                )

                self.jira.update_confluence_url(jira_key, confluence_url)
                self._linked_keys.add(jira_key)
                self.stats["linked"] += 1
                log.info("Set up links for %s (Confluence: %s)", jira_key, confluence_url)
        else:
            # Already linked — just update progress
            full = self.confluence.get_page(conf_page["id"])
            if full:
                progress = self.jira.calculate_progress(jira_key)
                total = progress.get("total", 0)
                if total > 0:
                    body = full["body"]["storage"]["value"]
                    version = full["version"]["number"]
                    updated = self.confluence.update_progress_status(
                        conf_page["id"], conf_page["title"],
                        version, body, progress["done"], total,
                    )
                    if updated:
                        self.stats["updated"] += 1
                        return
            self.stats["skipped"] += 1

    @staticmethod
    def _build_links_section(
        jira_key: str, jira_url: str, notion_url: str,
    ) -> str:
        """Build only the links section XHTML — no full template."""
        parts = ['<h2>Ссылки</h2>']
        links = []
        if jira_url:
            links.append(f'<a href="{jira_url}">{jira_key} (Jira)</a>')
        if notion_url:
            links.append(f'<a href="{notion_url}">Notion</a>')
        if links:
            parts.append(f'<p>{" | ".join(links)}</p>')
        parts.append("<hr/>")
        return "\n".join(parts)

    def _save(self):
        # Read-modify-write so we don't clobber buckets that other
        # components (OrphanResolver, other phases) wrote during this
        # cycle.
        state = _load_state()
        state["confluence_linked_keys"] = list(self._linked_keys)
        _save_state(state)

    def _log_stats(self):
        s = self.stats
        log.info(
            "Confluence: checked=%d, created=%d, linked=%d, updated=%d, "
            "skipped=%d, skipped_orphan=%d, errors=%d",
            s["checked"], s["created"], s["linked"], s["updated"],
            s["skipped"], s.get("skipped_orphan", 0), s["errors"],
        )


# ---- Bidirectional Section Sync ----


class SectionSync:
    """Bidirectional section content sync: Notion toggles <-> Confluence <h2> sections.

    Syncs content of matching sections between platforms using content hashing
    for change detection and last-write-wins for conflict resolution.
    """

    SYNCED_SECTIONS = [
        "Описание задачи",
        "Минимальный функционал (MVP)",
        "Заметки / Лог",
        "Результат",
    ]

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        confluence: ConfluenceClient,
        dry_run: bool = False,
    ):
        self.jira = jira
        self.notion = notion
        self.confluence = confluence
        self.dry_run = dry_run
        self.stats = {
            "checked": 0, "notion_to_conf": 0, "conf_to_notion": 0,
            "conflicts": 0, "skipped": 0, "skipped_orphan": 0, "errors": 0,
        }
        self._state = _load_state()
        self._section_state: Dict[str, Any] = self._state.get("section_sync", {})
        self._orphans = OrphanResolver(STATE_FILE)

    def run(self):
        from .content_converter import (
            compute_content_hash,
            notion_blocks_to_xhtml,
            xhtml_to_notion_blocks,
        )
        self._compute_hash = compute_content_hash
        self._to_xhtml = notion_blocks_to_xhtml
        self._to_blocks = xhtml_to_notion_blocks

        log.info("SectionSync: starting")
        pages = self.notion.query_all_pages_with_jira_key()
        log.info("SectionSync: found %d pages", len(pages))

        for page in pages:
            jira_key = NotionClient.get_jira_key(page)
            if not jira_key:
                continue
            self.stats["checked"] += 1

            # SectionSync runs last in the daemon cycle today, so a
            # tombstone written by SubtaskTodoSync / ConfluenceSync
            # would already be visible. We still call probe_and_resolve
            # here (one Jira GET per orphan) so SectionSync stays
            # correct regardless of phase ordering — the daemon could
            # in principle reorder phases or run SectionSync standalone.
            if self._orphans.probe_and_resolve(jira_key, self.jira):
                self.stats["skipped_orphan"] += 1
                continue

            try:
                self._sync_task(page, jira_key)
            except Exception as e:
                self.stats["errors"] += 1
                log.error("SectionSync error for %s: %s", jira_key, e)
            time.sleep(0.4)

        self._save()
        self._log_stats()

    def _sync_task(self, page: Dict[str, Any], jira_key: str):
        page_id = page["id"]

        # Get Confluence page
        conf_page = self.confluence.find_page_by_jira_key(jira_key)
        if not conf_page:
            return

        full = self.confluence.get_page_with_version(conf_page["id"])
        if not full:
            return

        conf_body = full["body"]["storage"]["value"]
        conf_version = full["version"]["number"]
        conf_when = full["version"].get("when", "")
        notion_edited = page.get("last_edited_time", "")

        task_state = self._section_state.get(jira_key, {})
        body_changed = False

        for section in self.SYNCED_SECTIONS:
            # Read Notion content
            notion_blocks = self.notion.get_toggle_content(page_id, section)
            notion_xhtml = self._to_xhtml(notion_blocks or [])
            notion_hash = self._compute_hash(notion_xhtml)

            # Read Confluence content
            conf_content = ConfluenceClient.extract_section(conf_body, section)
            conf_hash = self._compute_hash(conf_content or "")

            # Compare with saved state
            saved = task_state.get(section, {})
            saved_notion = saved.get("notion_hash", "")
            saved_conf = saved.get("confluence_hash", "")

            notion_changed = (notion_hash != saved_notion)
            conf_changed = (conf_hash != saved_conf)

            if not notion_changed and not conf_changed:
                continue

            if self.dry_run:
                direction = "N→C" if notion_changed else "C→N"
                if notion_changed and conf_changed:
                    direction = "CONFLICT"
                log.info(
                    "[DRY-RUN] %s / %s: %s", jira_key, section, direction,
                )
                continue

            if notion_changed and not conf_changed:
                # Notion → Confluence
                conf_body = ConfluenceClient.replace_section(
                    conf_body, section, notion_xhtml,
                )
                body_changed = True
                conf_hash = self._compute_hash(notion_xhtml)
                self.stats["notion_to_conf"] += 1
                log.info("%s: %s → Confluence", jira_key, section)

            elif conf_changed and not notion_changed:
                # Confluence → Notion
                new_blocks = self._to_blocks(conf_content or "")
                self.notion.replace_toggle_content(page_id, section, new_blocks)
                # Flip-flop guard: save hash of what Notion will produce on
                # next read, not the original Confluence hash. This prevents
                # converter non-idempotency from triggering false changes.
                roundtrip_xhtml = self._to_xhtml(new_blocks)
                notion_hash = self._compute_hash(roundtrip_xhtml)
                self.stats["conf_to_notion"] += 1
                log.info("%s: %s → Notion", jira_key, section)

            else:
                # Both changed — last-write-wins by timestamp
                self.stats["conflicts"] += 1
                if notion_edited >= conf_when:
                    # Notion wins
                    conf_body = ConfluenceClient.replace_section(
                        conf_body, section, notion_xhtml,
                    )
                    body_changed = True
                    conf_hash = self._compute_hash(notion_xhtml)
                    log.info(
                        "%s: %s CONFLICT → Notion wins (edited %s vs %s)",
                        jira_key, section, notion_edited, conf_when,
                    )
                else:
                    # Confluence wins
                    new_blocks = self._to_blocks(conf_content or "")
                    self.notion.replace_toggle_content(
                        page_id, section, new_blocks,
                    )
                    # Flip-flop guard: save roundtrip hash for Notion
                    roundtrip_xhtml = self._to_xhtml(new_blocks)
                    notion_hash = self._compute_hash(roundtrip_xhtml)
                    log.info(
                        "%s: %s CONFLICT → Confluence wins (edited %s vs %s)",
                        jira_key, section, conf_when, notion_edited,
                    )

            # Update state for this section
            task_state[section] = {
                "notion_hash": notion_hash,
                "confluence_hash": conf_hash,
                "last_synced": datetime.now().isoformat(),
            }

        # Single Confluence update for all changed sections
        if body_changed:
            self.confluence.update_page(
                conf_page["id"], conf_page["title"], conf_body, conf_version,
            )

        self._section_state[jira_key] = task_state

    def _save(self):
        # Read-modify-write so we don't clobber buckets that other
        # components (OrphanResolver, other phases) wrote during this
        # cycle.
        state = _load_state()
        state["section_sync"] = self._section_state
        _save_state(state)

    def _log_stats(self):
        s = self.stats
        log.info(
            "SectionSync: checked=%d, N→C=%d, C→N=%d, conflicts=%d, "
            "skipped=%d, skipped_orphan=%d, errors=%d",
            s["checked"], s["notion_to_conf"], s["conf_to_notion"],
            s["conflicts"], s["skipped"],
            s.get("skipped_orphan", 0), s["errors"],
        )


# ---- Orchestrator ----


def run_sync(
    full: bool = False,
    dry_run: bool = False,
    minutes: int = 15,
    with_progress: bool = False,
    reverse: bool = False,
    bidirectional: bool = False,
    migrate: bool = False,
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

    if migrate:
        creator = NotionToJiraCreator(
            jira=jira, notion=notion, dry_run=dry_run
        )
        creator.run()
        return True

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
