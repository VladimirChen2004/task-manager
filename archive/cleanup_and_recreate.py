#!/usr/bin/env python3
"""Migration script: delete old Jira issues and reset Notion pages.

After running this, the daemon will recreate all issues with the new template
(plan section first, hyperlinks, Confluence pages).

Usage:
    python cleanup_and_recreate.py --dry-run   # Preview
    python cleanup_and_recreate.py              # Execute
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from taskautomation.config import STATE_FILE
from taskautomation.jira_client import JiraVCHEN
from taskautomation.notion_client import NotionClient


def delete_jira_issues(jira: JiraVCHEN, dry_run: bool):
    """Delete all parent issues in VC project (subtasks deleted automatically)."""
    print("\n=== Phase 1: Delete Jira issues ===")
    issues = jira.get_all_issues(max_results=200)

    # Only parent issues — subtasks are deleted with deleteSubtasks=true
    parent_issues = [i for i in issues if not i.get("is_subtask")]
    print(f"Found {len(parent_issues)} parent issues to delete")

    import requests as http_requests

    for issue in parent_issues:
        key = issue["key"]
        if dry_run:
            print(f"  [DRY-RUN] Would delete {key}: {issue['summary'][:60]}")
            continue

        url = f"{jira.server}/rest/api/3/issue/{key}?deleteSubtasks=true"
        resp = http_requests.delete(url, auth=jira._auth, timeout=30)
        if resp.status_code == 204:
            print(f"  Deleted {key}: {issue['summary'][:60]}")
        else:
            print(f"  FAILED to delete {key}: {resp.status_code} {resp.text[:100]}")
        time.sleep(0.3)


def clean_notion_pages(notion: NotionClient, dry_run: bool):
    """Clear Jira Key from all Notion pages and remove old content blocks."""
    print("\n=== Phase 2: Clean Notion pages ===")
    pages = notion.query_all_pages_with_jira_key()
    print(f"Found {len(pages)} pages with Jira Key")

    for page in pages:
        page_id = page["id"]
        jira_key = NotionClient.get_jira_key(page)
        title = NotionClient.get_page_title(page) or "?"

        if dry_run:
            print(f"  [DRY-RUN] Would clean '{title}' (Jira Key: {jira_key})")
            continue

        # 1. Clear Jira Key property
        notion.update_page_jira_key(page_id, "")
        print(f"  Cleared Jira Key on '{title}' (was {jira_key})")

        # 2. Remove auto-generated content blocks (callouts, divider, headings)
        blocks = notion.get_block_children(page_id)
        for block in blocks:
            block_type = block.get("type", "")
            should_delete = False

            # Delete callouts (🔗 links, 🤖 auto-created)
            if block_type == "callout":
                icon = block.get("callout", {}).get("icon", {})
                emoji = icon.get("emoji", "")
                if emoji in ("🔗", "🤖"):
                    should_delete = True

            # Delete dividers
            if block_type == "divider":
                should_delete = True

            # Delete auto-generated headings (План выполнения, Описание задачи, ТЗ)
            if "heading" in block_type:
                rt = block.get(block_type, {}).get("rich_text", [])
                text = "".join(r.get("plain_text", "") for r in rt)
                if text in ("План выполнения", "Описание задачи", "ТЗ"):
                    should_delete = True

            if should_delete:
                notion.delete_block(block["id"])
                print(f"    Deleted {block_type} block from '{title}'")
                time.sleep(0.35)

        time.sleep(0.4)


def reset_state():
    """Reset sync state file."""
    print("\n=== Phase 3: Reset sync state ===")
    if STATE_FILE.exists():
        STATE_FILE.write_text("{}")
        print(f"  Reset {STATE_FILE}")
    else:
        print(f"  No state file found at {STATE_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Migration: cleanup and prepare for recreation")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    args = parser.parse_args()

    print("=" * 60)
    print("Migration: Delete old Jira issues + clean Notion pages")
    print("=" * 60)
    if args.dry_run:
        print("MODE: DRY-RUN (no changes will be made)\n")
    else:
        print("MODE: LIVE — changes will be permanent!\n")
        resp = input("Continue? (yes/no): ")
        if resp.strip().lower() != "yes":
            print("Aborted.")
            return

    jira = JiraVCHEN()
    notion = NotionClient()

    delete_jira_issues(jira, args.dry_run)
    clean_notion_pages(notion, args.dry_run)

    if not args.dry_run:
        reset_state()

    print("\n" + "=" * 60)
    if args.dry_run:
        print("DRY-RUN complete. Run without --dry-run to execute.")
    else:
        print("Migration complete!")
        print("Next steps:")
        print("  1. make deploy")
        print("  2. Restart daemon on server")
        print("  3. Daemon will recreate Jira issues + Confluence pages")
    print("=" * 60)


if __name__ == "__main__":
    main()
