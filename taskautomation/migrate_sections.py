"""One-time migration: update Confluence/Notion templates on existing pages.

Usage:
    python -m taskautomation.migrate_sections --dry-run   # preview
    python -m taskautomation.migrate_sections              # execute
"""

import argparse
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List

from .config import STATE_FILE, ConfluenceConfig
from .confluence_client import ConfluenceClient
from .content_converter import compute_content_hash, notion_blocks_to_xhtml
from .jira_client import JiraVCHEN
from .notion_client import NotionClient

log = logging.getLogger("taskautomation.migrate")

SECTIONS_TO_REMOVE = [
    "Критерии приёмки",
    "Техническое задание",
    "Архитектура / Дизайн решения",
    "Зависимости",
]

SECTIONS_TO_ADD_NOTION = [
    (
        "Минимальный функционал (MVP)",
        {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": "Описать минимально необходимый результат"},
                    "annotations": {"italic": True},
                }],
            },
        },
    ),
    (
        "Результат",
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": "Описание выполненной работы (заполняется по итогу)."},
                    "annotations": {"italic": True},
                }],
            },
        },
    ),
    (
        "Заметки / Лог",
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": "Заметки по ходу работы над задачей."},
                    "annotations": {"italic": True},
                }],
            },
        },
    ),
]

SYNCED_SECTIONS = [
    "Минимальный функционал (MVP)",
    "Результат",
    "Заметки / Лог",
]


def _build_toggle(name: str, child: Dict) -> Dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": name}}],
            "is_toggleable": True,
            "children": [child],
        },
    }


class SectionMigration:

    def __init__(
        self,
        jira: JiraVCHEN,
        notion: NotionClient,
        confluence: ConfluenceClient,
        dry_run: bool = True,
    ):
        self.jira = jira
        self.notion = notion
        self.confluence = confluence
        self.dry_run = dry_run
        self.stats = {
            "conf_updated": 0, "notion_updated": 0,
            "state_init": 0, "errors": 0,
        }

    def run(self):
        pages = self.notion.query_all_pages_with_jira_key()
        log.info("Migration: found %d pages with Jira Key", len(pages))

        state = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        section_sync = state.get("section_sync", {})

        for page in pages:
            jira_key = NotionClient.get_jira_key(page)
            if not jira_key:
                continue

            try:
                self._migrate_confluence(jira_key)
                self._migrate_notion(page)
                task_state = self._init_state(page, jira_key)
                if task_state:
                    section_sync[jira_key] = task_state
                    self.stats["state_init"] += 1
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Migration error for %s: %s", jira_key, e)

            time.sleep(0.4)

        if not self.dry_run:
            state["section_sync"] = section_sync
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            log.info("State saved with %d task entries", len(section_sync))

        s = self.stats
        log.info(
            "Migration done: conf_updated=%d, notion_updated=%d, state_init=%d, errors=%d",
            s["conf_updated"], s["notion_updated"], s["state_init"], s["errors"],
        )

    def _migrate_confluence(self, jira_key: str):
        conf_page = self.confluence.find_page_by_jira_key(jira_key)
        if not conf_page:
            return

        full = self.confluence.get_page(conf_page["id"])
        if not full:
            return

        body = full["body"]["storage"]["value"]
        version = full["version"]["number"]
        original = body

        for section in SECTIONS_TO_REMOVE:
            body = ConfluenceClient.remove_section(body, section)

        if body == original:
            return  # Nothing to remove

        if self.dry_run:
            log.info("[DRY-RUN] Would remove %d sections from Confluence page %s",
                     len(SECTIONS_TO_REMOVE), jira_key)
            return

        ok = self.confluence.update_page(conf_page["id"], conf_page["title"], body, version)
        if ok:
            self.stats["conf_updated"] += 1
            log.info("Removed old sections from Confluence page %s", jira_key)

    def _migrate_notion(self, page: Dict[str, Any]):
        page_id = page["id"]
        jira_key = NotionClient.get_jira_key(page)

        # Remove ТЗ toggle if it exists
        tz_id = self.notion.find_toggle_by_text(page_id, "ТЗ")
        if tz_id:
            if self.dry_run:
                log.info("[DRY-RUN] Would remove 'ТЗ' toggle from %s", jira_key)
            else:
                self.notion.delete_block(tz_id)
                log.info("Removed 'ТЗ' toggle from %s", jira_key)

        # Add new sections (only if not already present)
        added = False
        blocks_to_add = []
        for section_name, child_block in SECTIONS_TO_ADD_NOTION:
            existing = self.notion.find_toggle_by_text(page_id, section_name)
            if not existing:
                blocks_to_add.append(_build_toggle(section_name, child_block))

        if blocks_to_add:
            if self.dry_run:
                names = [b["heading_2"]["rich_text"][0]["text"]["content"] for b in blocks_to_add]
                log.info("[DRY-RUN] Would add sections to %s: %s", jira_key, names)
            else:
                # Insert before "Описание задачи" if it exists, else append at end
                desc_id = self.notion.find_toggle_by_text(page_id, "Описание задачи")
                if desc_id:
                    # Notion API doesn't support "insert before", so append at page level
                    # and rely on template order for new pages.
                    self.notion.append_children(page_id, blocks_to_add)
                else:
                    self.notion.append_children(page_id, blocks_to_add)
                self.stats["notion_updated"] += 1
                log.info("Added %d sections to %s", len(blocks_to_add), jira_key)
                added = True

        if not added and not tz_id:
            return  # Nothing changed

    def _init_state(self, page: Dict[str, Any], jira_key: str) -> Dict:
        """Compute initial hashes for SectionSync state."""
        page_id = page["id"]
        task_state = {}

        conf_page = self.confluence.find_page_by_jira_key(jira_key)
        conf_body = ""
        if conf_page:
            full = self.confluence.get_page(conf_page["id"])
            if full:
                conf_body = full["body"]["storage"]["value"]

        for section in SYNCED_SECTIONS:
            # Notion hash
            notion_blocks = self.notion.get_toggle_content(page_id, section)
            notion_xhtml = notion_blocks_to_xhtml(notion_blocks or [])
            notion_hash = compute_content_hash(notion_xhtml)

            # Confluence hash
            conf_content = ConfluenceClient.extract_section(conf_body, section) if conf_body else ""
            conf_hash = compute_content_hash(conf_content or "")

            task_state[section] = {
                "notion_hash": notion_hash,
                "confluence_hash": conf_hash,
                "last_synced": datetime.now().isoformat(),
            }

        return task_state


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Migrate section templates")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    args = parser.parse_args()

    jira = JiraVCHEN()
    notion = NotionClient()

    try:
        conf_cfg = ConfluenceConfig()
        confluence = ConfluenceClient()
    except Exception:
        log.error("Confluence not configured, skipping Confluence migration")
        confluence = None

    if not confluence:
        log.error("Confluence client required for migration")
        return

    migration = SectionMigration(
        jira=jira, notion=notion, confluence=confluence,
        dry_run=args.dry_run,
    )
    migration.run()


if __name__ == "__main__":
    main()
