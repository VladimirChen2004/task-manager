#!/usr/bin/env python3
"""Backward-compatible CLI entry point for Jira VCHEN operations.
Delegates to taskautomation package. Skills reference this path.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from taskautomation.cli import main_jira  # noqa: E402

# Re-export for backward compatibility (other scripts imported these)
from taskautomation.jira_client import JiraVCHEN  # noqa: E402, F401
from taskautomation.config import (  # noqa: E402, F401
    JIRA_TO_NOTION_STATUS,
    NOTION_TO_JIRA_STATUS,
    NOTION_TO_JIRA_PRIORITY,
)

if __name__ == "__main__":
    main_jira()
