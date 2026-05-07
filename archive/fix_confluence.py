#!/usr/bin/env python3
"""One-time script: fix Confluence pages after removing Jira Automation.

1. Clean Jira summaries (remove "— Задача №X")
2. Clean Notion page titles
3. Delete all subtask Confluence pages
4. Fix parent task Confluence pages: title, body (new template), move under VC Tasks
"""

import os
import re
import sys
import time

import requests as http_requests

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from taskautomation.confluence_client import ConfluenceClient
from taskautomation.jira_client import JiraVCHEN
from taskautomation.notion_client import NotionClient

DRY_RUN = "--dry-run" in sys.argv

conf = ConfluenceClient()
jira = JiraVCHEN()
notion = NotionClient()

# Pattern to strip "— Задача №X" suffix
TASK_SUFFIX = re.compile(r"\s*—\s*Задача\s*№\s*\d+\s*$")


def clean_title(title: str) -> str:
    """Remove '— Задача №X' suffix from title."""
    return TASK_SUFFIX.sub("", title).strip()


# --- Step 0: Gather data ---
print("=== Gathering data ===")

# All VC- pages in space
search_url = f"{conf.base_url}/rest/api/content/search"
resp = conf._request("get", search_url, params={
    "cql": f'space = "{conf.space_key}" AND title ~ "VC-"',
    "expand": "version,ancestors",
    "limit": 100,
})
all_pages = resp.json().get("results", [])
print(f"Found {len(all_pages)} Confluence pages with VC-")

# All Jira issues
all_issues = jira.get_all_issues(max_results=200)
subtask_keys = set()
parent_issues = {}
for issue in all_issues:
    if issue["is_subtask"]:
        subtask_keys.add(issue["key"])
    else:
        parent_issues[issue["key"]] = issue

print(f"Jira: {len(parent_issues)} parent tasks, {len(subtask_keys)} subtasks")

# All Notion pages with Jira Key
notion_pages = notion.query_all_pages_with_jira_key()
notion_by_key = {}
for page in notion_pages:
    key = NotionClient.get_jira_key(page)
    if key:
        notion_by_key[key] = page
print(f"Notion: {len(notion_by_key)} pages with Jira Key")

# --- Step 1: Fix Jira summaries ---
print("\n=== Step 1: Fix Jira summaries (remove '— Задача №X') ===")
jira_fixed = 0
for jira_key, issue in sorted(parent_issues.items()):
    original = issue["summary"]
    cleaned = clean_title(original)
    if original == cleaned:
        continue

    if DRY_RUN:
        print(f"  [DRY-RUN] {jira_key}: '{original}' → '{cleaned}'")
    else:
        url = f"{jira.server}/rest/api/3/issue/{jira_key}"
        resp = http_requests.put(
            url, auth=jira._auth,
            json={"fields": {"summary": cleaned}},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code in (200, 204):
            print(f"  {jira_key}: '{original}' → '{cleaned}'")
            issue["summary"] = cleaned  # Update local copy
            jira_fixed += 1
        else:
            print(f"  FAILED {jira_key}: {resp.status_code}")
    time.sleep(0.3)

print(f"Fixed {jira_fixed} Jira summaries")

# --- Step 2: Fix Notion page titles ---
print("\n=== Step 2: Fix Notion page titles ===")
notion_fixed = 0
for jira_key, page in sorted(notion_by_key.items()):
    original = NotionClient.get_page_title(page) or ""
    cleaned = clean_title(original)
    if original == cleaned:
        continue

    page_id = page["id"]
    if DRY_RUN:
        print(f"  [DRY-RUN] {jira_key}: '{original}' → '{cleaned}'")
    else:
        # Update title via Notion API
        url = f"https://api.notion.com/v1/pages/{page_id}"
        headers = {
            "Authorization": f"Bearer {notion.api_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        payload = {
            "properties": {
                "Task name": {
                    "title": [{"text": {"content": cleaned}}]
                }
            }
        }
        resp = http_requests.patch(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            print(f"  {jira_key}: '{original}' → '{cleaned}'")
            notion_fixed += 1
        else:
            print(f"  FAILED {jira_key}: {resp.status_code} {resp.text[:200]}")
    time.sleep(0.3)

print(f"Fixed {notion_fixed} Notion titles")

# --- Step 3: Delete subtask Confluence pages ---
print("\n=== Step 3: Delete subtask Confluence pages ===")
deleted = 0
for page in all_pages:
    title = page["title"]
    match = re.match(r"(VC-\d+)", title)
    if not match:
        continue
    jira_key = match.group(1)
    if jira_key not in subtask_keys:
        continue

    page_id = page["id"]
    if DRY_RUN:
        print(f"  [DRY-RUN] Would delete: {title} (id={page_id})")
    else:
        url = f"{conf.base_url}/rest/api/content/{page_id}"
        resp = conf._request("delete", url)
        if resp.status_code in (200, 204):
            print(f"  Deleted: {title}")
            deleted += 1
        else:
            print(f"  FAILED to delete {title}: {resp.status_code}")
    time.sleep(0.3)

print(f"Deleted {deleted} subtask pages")

# --- Step 4: Fix parent task Confluence pages ---
print("\n=== Step 4: Fix parent task Confluence pages ===")
TARGET_PARENT = conf.parent_page_id  # VC Tasks page ID
fixed = 0

for page in all_pages:
    title = page["title"]
    match = re.match(r"(VC-\d+)", title)
    if not match:
        continue
    jira_key = match.group(1)
    if jira_key in subtask_keys:
        continue  # Already handled

    page_id = page["id"]
    issue = parent_issues.get(jira_key)
    if not issue:
        print(f"  Skip {jira_key}: not found in Jira")
        continue

    # Build correct title from cleaned Jira summary
    correct_title = f"{jira_key} — {issue['summary']}"

    # Get Notion URL
    notion_page = notion_by_key.get(jira_key)
    notion_url = ""
    if notion_page:
        nid = notion_page["id"].replace("-", "")
        notion_url = f"https://notion.so/{nid}"

    # Build new body with new template
    jira_url = issue.get("url", f"{jira.server}/browse/{jira_key}")
    summary = issue.get("description", "")
    if notion_page:
        summary = NotionClient.get_page_summary(notion_page) or summary

    subtasks = jira.get_subtask_details(jira_key)
    new_body = conf.build_task_page_html(
        jira_key=jira_key,
        jira_url=jira_url,
        notion_url=notion_url,
        summary=summary,
        subtasks=subtasks if subtasks else None,
    )

    # Check what needs fixing
    ancestors = page.get("ancestors", [])
    current_parent = ancestors[-1]["id"] if ancestors else None
    needs_move = str(current_parent) != str(TARGET_PARENT)

    version = page.get("version", {}).get("number", 1)

    if DRY_RUN:
        changes = []
        if title != correct_title:
            changes.append(f"title: '{title}' → '{correct_title}'")
        if needs_move:
            changes.append("move to VC Tasks")
        changes.append("update body with new template")
        print(f"  [DRY-RUN] {jira_key}: {', '.join(changes)}")
        continue

    # Update page: new title + body + move
    url = f"{conf.base_url}/rest/api/content/{page_id}"
    payload = {
        "type": "page",
        "title": correct_title,
        "body": {
            "storage": {
                "value": new_body,
                "representation": "storage",
            }
        },
        "version": {"number": version + 1},
    }
    if needs_move:
        payload["ancestors"] = [{"id": int(TARGET_PARENT)}]

    resp = conf._request("put", url, json=payload)
    if resp.status_code == 200:
        parts = []
        if title != correct_title:
            parts.append("title fixed")
        if needs_move:
            parts.append("moved to VC Tasks")
        parts.append("body updated")
        print(f"  {jira_key}: {', '.join(parts)}")
        fixed += 1
    else:
        print(f"  FAILED {jira_key}: {resp.status_code} {resp.text[:200]}")
    time.sleep(0.5)

print(f"\nFixed {fixed} parent task Confluence pages")
print("\nDone!")
