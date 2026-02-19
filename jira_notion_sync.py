#!/usr/bin/env python3
"""Backward-compatible CLI entry point for Jira ↔ Notion sync.
Delegates to taskautomation package.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from taskautomation.cli import main_sync  # noqa: E402

if __name__ == "__main__":
    main_sync()
