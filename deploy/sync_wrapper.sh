#!/bin/bash
# Cron wrapper for jira_notion_sync.py
# Usage: sync_wrapper.sh [--full] [--bidirectional] [--with-progress] etc.
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"
exec venv/bin/python3 jira_notion_sync.py "$@"
