#!/usr/bin/env python3
"""Fix duplicate tasks: restore original Jira Keys, delete duplicates."""
import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv("/Users/nfware/Documents/my_prjcts/task-automation/.env")

from taskautomation.notion_client import NotionClient
from taskautomation.jira_client import JiraVCHEN

# Init clients
nc = NotionClient(os.environ["NOTION_API_TOKEN"], os.environ["NOTION_DATABASE_ID"])
jira = JiraVCHEN(os.environ["JIRA_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])

token = os.environ["NOTION_API_TOKEN"]
db_id = os.environ["NOTION_DATABASE_ID"]

DRY_RUN = "--dry-run" in sys.argv

def get_all_pages():
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        headers=headers, json={"page_size": 100}, timeout=30,
    )
    return resp.json().get("results", [])

def main():
    pages = get_all_pages()
    print(f"Total pages: {len(pages)}")

    # Separate original pages (now with wrong keys VC-99..VC-110)
    # from duplicate pages (with correct keys VC-47..VC-91 but wrong content)
    originals = {}  # title -> page info
    duplicates = {}  # title -> page info

    for p in pages:
        title = nc.get_page_title(p) or "(no title)"
        jira_key = nc.get_jira_key(p) or "(none)"
        created = p.get("created_time", "")

        if jira_key == "(none)":
            continue

        key_num = int(jira_key.split("-")[1])
        entry = {
            "page_id": p["id"],
            "jira_key": jira_key,
            "created": created[:16],
            "title": title,
        }

        if key_num >= 99:
            originals[title] = entry
        else:
            duplicates[title] = entry

    # Build restore mapping
    print("\n=== RESTORE MAPPING ===")
    restore_map = []  # (orig_page_id, wrong_key, correct_key, dup_page_id)

    for title in sorted(originals.keys()):
        orig = originals.get(title)
        dup = duplicates.get(title)
        if dup:
            print(f"  {orig['jira_key']:8s} -> {dup['jira_key']:8s} | {title[:50]}")
            restore_map.append({
                "orig_page_id": orig["page_id"],
                "wrong_key": orig["jira_key"],
                "correct_key": dup["jira_key"],
                "dup_page_id": dup["page_id"],
                "title": title,
            })

    if DRY_RUN:
        print("\n[DRY-RUN] Would restore", len(restore_map), "Jira Keys")
        print("[DRY-RUN] Would delete", len(restore_map), "duplicate Notion pages")
        print("[DRY-RUN] Would delete Jira issues:",
              ", ".join(r["wrong_key"] for r in restore_map))
        return

    # Step 1: Restore Jira Keys on original Notion pages
    print("\n=== STEP 1: Restoring Jira Keys on original pages ===")
    for item in restore_map:
        ok = nc.update_page_jira_key(item["orig_page_id"], item["correct_key"])
        status = "OK" if ok else "FAIL"
        print(f"  {item['wrong_key']} -> {item['correct_key']}: {status}")
        time.sleep(0.3)

    # Step 2: Archive duplicate Notion pages
    print("\n=== STEP 2: Archiving duplicate Notion pages ===")
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    for item in restore_map:
        resp = requests.patch(
            f"https://api.notion.com/v1/pages/{item['dup_page_id']}",
            headers=headers, json={"archived": True}, timeout=10,
        )
        status = "OK" if resp.status_code == 200 else f"FAIL ({resp.status_code})"
        print(f"  Archive {item['correct_key']} dup page: {status}")
        time.sleep(0.3)

    # Step 3: Delete duplicate Jira issues (VC-99..VC-110) and their subtasks
    print("\n=== STEP 3: Deleting duplicate Jira issues ===")
    auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])

    for item in restore_map:
        wrong_key = item["wrong_key"]
        # First delete subtasks
        subs = jira.get_subtask_details(wrong_key)
        for sub in subs:
            resp = requests.delete(
                f"{os.environ['JIRA_URL']}/rest/api/3/issue/{sub['key']}",
                auth=auth, timeout=10,
            )
            status = "OK" if resp.status_code in (200, 204) else f"FAIL ({resp.status_code})"
            print(f"  Delete subtask {sub['key']}: {status}")
            time.sleep(0.2)

        # Then delete the issue itself
        resp = requests.delete(
            f"{os.environ['JIRA_URL']}/rest/api/3/issue/{wrong_key}",
            auth=auth, timeout=10,
        )
        status = "OK" if resp.status_code in (200, 204) else f"FAIL ({resp.status_code})"
        print(f"  Delete {wrong_key}: {status}")
        time.sleep(0.3)

    # Step 4: Check for duplicate Confluence pages
    print("\n=== STEP 4: Checking Confluence duplicates ===")
    from taskautomation.confluence_client import ConfluenceClient
    confluence = ConfluenceClient(
        os.environ["CONFLUENCE_URL"],
        os.environ["CONFLUENCE_EMAIL"],
        os.environ["CONFLUENCE_TOKEN"],
    )
    for item in restore_map:
        wrong_key = item["wrong_key"]
        page = confluence.find_page_by_jira_key(wrong_key)
        if page:
            print(f"  Found Confluence page for {wrong_key}: {page['title']} (id={page['id']})")
            # Delete it
            resp = requests.delete(
                f"{os.environ['CONFLUENCE_URL']}/rest/api/content/{page['id']}",
                auth=(os.environ["CONFLUENCE_EMAIL"], os.environ["CONFLUENCE_TOKEN"]),
                timeout=10,
            )
            status = "OK" if resp.status_code in (200, 204) else f"FAIL ({resp.status_code})"
            print(f"    Deleted: {status}")
        else:
            print(f"  No Confluence page for {wrong_key}")
        time.sleep(0.3)

    print("\n=== DONE ===")
    # Verify
    print("\nVerification:")
    pages = get_all_pages()
    count = 0
    for p in pages:
        jira_key = nc.get_jira_key(p) or "(none)"
        title = nc.get_page_title(p) or "(no title)"
        if jira_key != "(none)":
            count += 1
            print(f"  {jira_key:8s} | {title[:50]}")
    print(f"Total active pages with Jira Key: {count}")


if __name__ == "__main__":
    main()
