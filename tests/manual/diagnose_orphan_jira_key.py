#!/usr/bin/env python3
"""Read-only diagnostics for an orphan Jira key.

NOT part of CI. Use this when the daemon repeatedly logs an error
like "Confluence sync error for VC-XYZ: 404 ... /rest/api/3/issue/VC-XYZ"
to find out where the key still lives and which phase pulls it back
into the active sync loop.

Performs ONLY GET requests against Jira / Notion / Confluence and
prints what it finds. Never writes anything.

Usage:
    cd /path/to/task-automation
    venv/bin/python3 tests/manual/diagnose_orphan_jira_key.py VC-114

Sample interpretation:
    state.subtask_todos[VC-114]      → Jira key still in active state
    notion: 1 page                   → Notion page still has Jira Key
    confluence: 1 page (cql title)   → Confluence page still on disk
    jira: 404                        → Jira issue truly deleted
    subproject: N labelled subtasks  → orphan VCSUB issues remain
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / ".sync_state.json"


def header(s: str) -> None:
    print(f"\n=== {s} ===")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    jira_key = argv[1].strip()
    if not jira_key:
        print("ERROR: empty jira_key", file=sys.stderr)
        return 2

    sys.path.insert(0, str(REPO_ROOT))
    from taskautomation.config import SUBTASK_PROJECT
    from taskautomation.confluence_client import ConfluenceClient
    from taskautomation.jira_client import JiraVCHEN
    from taskautomation.notion_client import NotionClient

    print(f"Diagnosing orphan key: {jira_key}")
    print(f"State file: {STATE_PATH}")

    # ── 1. Local state file ────────────────────────────────────
    header("1. .sync_state.json")
    if not STATE_PATH.exists():
        print("  state file missing — first run? skipping")
    else:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        buckets = {
            "subtask_todos":           state.get("subtask_todos", {}),
            "known_notion_statuses":   state.get("known_notion_statuses", {}),
            "known_notion_priorities": state.get("known_notion_priorities", {}),
            "template_backfilled":     state.get("template_backfilled", []),
            "confluence_linked_keys":  state.get("confluence_linked_keys", []),
        }
        any_hit = False
        for name, bucket in buckets.items():
            if isinstance(bucket, dict):
                hit = jira_key in bucket
            else:
                hit = jira_key in (bucket or [])
            mark = "✓" if hit else " "
            print(f"  [{mark}] {name}")
            if hit:
                any_hit = True
        if not any_hit:
            print(f"  {jira_key} not found in any state bucket")

    # ── 2. Notion ──────────────────────────────────────────────
    header("2. Notion pages with this Jira Key")
    try:
        notion = NotionClient()
        # One quick query, plus a wider scan in case the index lags.
        page = notion.find_page_by_jira_key(jira_key)
        if page:
            title = NotionClient.get_page_title(page) or "?"
            url = page.get("url", "?")
            last_edited = page.get("last_edited_time", "?")
            print(f"  ✓ found: title={title!r}")
            print(f"    page_id={page.get('id')}")
            print(f"    url={url}")
            print(f"    last_edited={last_edited}")
        else:
            print(f"  ✗ no Notion page has Jira Key = {jira_key}")
    except Exception as e:
        print(f"  ERROR querying Notion: {type(e).__name__}: {e}")

    # ── 3. Confluence ──────────────────────────────────────────
    header("3. Confluence pages whose title contains the key")
    try:
        confluence = ConfluenceClient()
        cpage = confluence.find_page_by_jira_key(jira_key)
        if cpage:
            print(f"  ✓ found: title={cpage.get('title')!r}")
            print(f"    page_id={cpage.get('id')}")
            ver = cpage.get("version", {})
            print(f"    version={ver.get('number')}, when={ver.get('when')}")
        else:
            print(f"  ✗ no Confluence page found by jira_key={jira_key}")
    except Exception as e:
        print(f"  ERROR querying Confluence: {type(e).__name__}: {e}")

    # ── 4. Jira parent issue ───────────────────────────────────
    header("4. Jira parent issue")
    try:
        jira = JiraVCHEN()
        issue = jira.get_issue(jira_key)
        if issue:
            print(f"  ✓ found: status={issue.get('status')!r} "
                  f"summary={issue.get('summary', '?')[:60]!r}")
        else:
            print(f"  ✗ Jira returned no issue for {jira_key}")
    except Exception as e:
        # 404 ends up here; print cleanly without traceback
        msg = str(e)
        first_line = msg.splitlines()[0] if msg else type(e).__name__
        print(f"  ✗ {first_line}")

    # ── 5. Subtask project (VCSUB) labelled subtasks ───────────
    header("5. Labelled orphan subtasks (parent-<key>)")
    if SUBTASK_PROJECT:
        try:
            subs = jira.get_subtask_details(jira_key)
            if subs:
                print(f"  ✓ {len(subs)} labelled subtask(s) in {SUBTASK_PROJECT}:")
                for st in subs:
                    print(f"    - {st['key']:<10} {st.get('status', '?'):<12} "
                          f"{st.get('summary', '?')[:60]!r}")
            else:
                print("  ✗ no labelled subtasks found")
        except Exception as e:
            print(f"  ERROR querying subtasks: {type(e).__name__}: {e}")
    else:
        print("  (SUBTASK_PROJECT not configured — skipping)")

    # ── 6. Which sync phase still pulls this key back? ─────────
    header("6. Re-entry analysis")
    print("  ConfluenceSync.run() iterates Notion pages with non-empty")
    print("  'Jira Key' property and calls jira.get_issue(jira_key) for")
    print("  each. As long as the Notion page exists with this key, the")
    print("  daemon will retry every cycle and log the 404.")
    print()
    print("  To stop the noise WITHOUT silently dropping the orphan:")
    print("    1. Decide what the orphan really is (manually deleted?")
    print("       intentional? leftover from migration?).")
    print("    2. Then either:")
    print("       a) Clear 'Jira Key' on the Notion page (un-links the")
    print("          orphan from Jira; Confluence page stays).")
    print("       b) Add a tombstone to .sync_state.json so the daemon")
    print("          skips this key but keeps the link visible.")
    print("       c) Recreate the Jira issue if deletion was unintended.")
    print("  Do NOT auto-delete based on 404 alone.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
