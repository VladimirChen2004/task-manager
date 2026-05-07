#!/usr/bin/env python3
"""Manual snapshot tool — NOT part of the CI test suite.

Fetches a real Confluence page and saves its <ac:task-list> section as
a new fixture under tests/fixtures/confluence_task_lists/. Use this
when you find a real page where parse_task_list misbehaves: snapshot
the page, drop the file in fixtures, write a regression test against
it, then fix.

Why this is not a regular test:
  - it requires live Confluence credentials and a working network;
  - the snapshot at time T may differ from T+1 if someone edits;
  - CI must stay deterministic and offline.

Usage:
    cd /path/to/task-automation
    venv/bin/python3 tests/manual/snapshot_task_list.py <page_id> <fixture_name>

Example:
    venv/bin/python3 tests/manual/snapshot_task_list.py 4349132807 v7_real_page.xml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "confluence_task_lists"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    page_id, fixture_name = argv[1], argv[2]

    # Import here so importing the module without a configured .env
    # (e.g. when reading the docstring) doesn't crash.
    sys.path.insert(0, str(REPO_ROOT))
    from taskautomation.confluence_client import ConfluenceClient

    client = ConfluenceClient()
    page = client.get_page(page_id)
    if not page:
        print(f"ERROR: page {page_id} not found or inaccessible", file=sys.stderr)
        return 1

    body = page.get("body", {}).get("storage", {}).get("value", "") or ""
    m = re.search(r"<ac:task-list>.*?</ac:task-list>", body, re.DOTALL)
    if not m:
        print(f"ERROR: no <ac:task-list> on page {page_id}", file=sys.stderr)
        return 1

    snippet = m.group(0)
    out_path = FIXTURE_DIR / fixture_name
    if out_path.exists():
        print(f"ERROR: {out_path} already exists; pick a new name",
              file=sys.stderr)
        return 1

    out_path.write_text(snippet + "\n", encoding="utf-8")
    print(f"Wrote {len(snippet)} bytes → {out_path}")

    # Smoke-check: can the parser read it back?
    parsed = ConfluenceClient.parse_task_list(snippet)
    print(f"parse_task_list returned {len(parsed)} tasks")
    for t in parsed:
        text = t.get("text", "")
        preview = text[:60] + ("…" if len(text) > 60 else "")
        print(f"  id={t.get('task_id')!s:>4}  uuid={t.get('uuid')!s:<12}  "
              f"checked={t.get('checked')!s:<5}  text={preview!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
