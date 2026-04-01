"""Daemon: continuous bidirectional sync loop."""

import logging
import signal
import time
from datetime import datetime
from typing import Any, Dict, Optional

from .config import DAEMON_INTERVAL_SECONDS, ConfluenceConfig
from .confluence_client import ConfluenceClient
from .jira_client import JiraVCHEN
from .notion_client import NotionClient
from .sync import (
    BidirectionalSync,
    ConfluenceSync,
    JiraToNotionCreator,
    NotionToJiraCreator,
    NotionToJiraSync,
    SectionSync,
    SubtaskTodoSync,
    _load_state,
    _save_state,
)

log = logging.getLogger("taskautomation.daemon")


class SyncDaemon:
    """Main daemon that runs all sync operations in a loop."""

    def __init__(
        self,
        interval: int = DAEMON_INTERVAL_SECONDS,
        with_progress: bool = True,
        with_creation: bool = True,
        dry_run: bool = False,
    ):
        self.interval = interval
        self.with_progress = with_progress
        self.with_creation = with_creation
        self.dry_run = dry_run
        self._running = True
        self._cycle_count = 0

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        log.info("Received signal %d, stopping after current cycle...", signum)
        self._running = False

    def run(self):
        """Main daemon loop."""
        log.info(
            "Daemon starting: interval=%ds, progress=%s, creation=%s, dry_run=%s",
            self.interval, self.with_progress, self.with_creation, self.dry_run,
        )

        try:
            jira = JiraVCHEN()
            notion = NotionClient()
        except Exception as e:
            log.error("Failed to initialize clients: %s", e)
            return

        # Confluence — optional, graceful fallback
        confluence: Optional[ConfluenceClient] = None
        try:
            cfg = ConfluenceConfig()
            if cfg.email and cfg.api_token:
                confluence = ConfluenceClient()
                log.info("Confluence client initialized (space=%s)", confluence.space_key)
            else:
                log.info("Confluence credentials not set, skipping Confluence sync")
        except Exception as e:
            log.warning("Confluence init failed (will skip): %s", e)

        log.info("Clients initialized successfully")
        consecutive_errors = 0

        while self._running:
            self._cycle_count += 1
            cycle_start = time.time()
            log.info(
                "=== Cycle %d at %s ===",
                self._cycle_count, datetime.now().strftime("%H:%M:%S"),
            )

            try:
                self._run_cycle(jira, notion, confluence)
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                backoff = min(300, self.interval * consecutive_errors)
                log.error(
                    "Cycle failed (%d consecutive), backing off %ds: %s",
                    consecutive_errors, backoff, e, exc_info=True,
                )
                self._sleep(backoff)
                continue

            elapsed = time.time() - cycle_start
            log.info("Cycle %d completed in %.1fs", self._cycle_count, elapsed)

            # Save cycle metadata
            state = _load_state()
            state["daemon_last_cycle"] = datetime.now().isoformat()
            state["daemon_cycle_count"] = self._cycle_count
            state["daemon_last_cycle_seconds"] = round(elapsed, 1)
            _save_state(state)

            # Sleep until next cycle
            sleep_time = max(0, self.interval - elapsed)
            if sleep_time > 0 and self._running:
                log.info("Sleeping %.0fs...", sleep_time)
                self._sleep(sleep_time)

        log.info("Daemon stopped after %d cycles", self._cycle_count)

    def _sleep(self, seconds: float):
        """Sleep in 1s chunks for responsive signal handling."""
        for _ in range(int(seconds)):
            if not self._running:
                break
            time.sleep(1)

    def _run_cycle(self, jira: JiraVCHEN, notion: NotionClient,
                    confluence: Optional[ConfluenceClient] = None):
        """Single sync cycle."""
        # Phase 1: Create Jira issues for new Notion tasks
        if self.with_creation:
            NotionToJiraCreator(
                jira=jira, notion=notion, confluence=confluence,
                dry_run=self.dry_run,
            ).run()

        # Phase 2: Create Notion pages for new Jira issues
        if self.with_creation:
            JiraToNotionCreator(
                jira=jira, notion=notion, confluence=confluence,
                dry_run=self.dry_run,
            ).run()

        # Phase 3: Bidirectional status/priority sync (delta + timestamp)
        BidirectionalSync(
            jira=jira, notion=notion,
            dry_run=self.dry_run, with_progress=self.with_progress,
        ).run_full()

        # Phase 4: Detect deleted Notion pages → archive Jira
        NotionToJiraSync(
            jira=jira, notion=notion, dry_run=self.dry_run
        ).run()

        # Phase 5: Sync to-do checkboxes ↔ Jira subtasks
        SubtaskTodoSync(
            jira=jira, notion=notion, dry_run=self.dry_run
        ).run()

        # Phase 6: Confluence sync (create pages, update progress)
        if confluence:
            ConfluenceSync(
                jira=jira, notion=notion, confluence=confluence,
                dry_run=self.dry_run,
            ).run()

        # Phase 7: Bidirectional section content sync (Notion ↔ Confluence)
        if confluence:
            SectionSync(
                jira=jira, notion=notion, confluence=confluence,
                dry_run=self.dry_run,
            ).run()
