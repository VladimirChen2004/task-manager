#!/usr/bin/env python3
"""CLI entry point for bidirectional sync daemon.
Delegates to taskautomation package.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from taskautomation.cli import main_daemon  # noqa: E402

if __name__ == "__main__":
    main_daemon()
