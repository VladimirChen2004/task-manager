"""Centralized configuration: env loading, mappings, constants."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Load .env once from project root
_project_root = Path(__file__).parent.parent
_env_path = _project_root / ".env"
if _env_path.exists() and load_dotenv:
    load_dotenv(_env_path)

# --- State file ---
STATE_FILE = _project_root / ".sync_state.json"

# --- Notion constants ---
NOTION_DATABASE_ID = os.environ.get(
    "NOTION_DATABASE_ID", "3050c57fd84181a7bb22ee1b23b37c6e"
)
NOTION_DATA_SOURCE_ID = "3050c57f-d841-81b5-98c6-000bda09220f"

# --- Status mappings ---
NOTION_TO_JIRA_STATUS: Dict[str, str] = {
    "Not started": "New",
    "Idea": "Idea",
    "In progress": "В работе",
    "Hold": "Hold",
    "Done": "Done",
}

JIRA_TO_NOTION_STATUS: Dict[str, str] = {
    "New": "Not started",
    "Idea": "Idea",
    "В работе": "In progress",
    "Hold": "Hold",
    "Done": "Done",
}

# --- Priority mappings ---
NOTION_TO_JIRA_PRIORITY: Dict[str, str] = {
    "Now": "Highest",
    "High": "High",
    "Medium": "Medium",
    "Low": "Low",
}

# --- Progress emoji ---


def get_progress_emoji(percentage: float) -> str:
    """Return emoji for progress percentage."""
    if percentage <= 0:
        return "⬜"
    elif percentage <= 30:
        return "🟨"
    elif percentage <= 60:
        return "🟧"
    elif percentage < 100:
        return "🟩"
    return "✅"


# --- Config dataclasses ---


@dataclass
class JiraConfig:
    server: str = field(
        default_factory=lambda: os.environ.get(
            "JIRA_URL", "https://nfware.atlassian.net"
        )
    )
    email: str = field(
        default_factory=lambda: os.environ.get("JIRA_EMAIL", "")
    )
    api_token: str = field(
        default_factory=lambda: os.environ.get("JIRA_API_TOKEN", "")
    )


@dataclass
class NotionConfig:
    api_token: str = field(
        default_factory=lambda: os.environ.get("NOTION_API_TOKEN", "")
    )
    database_id: str = field(default_factory=lambda: NOTION_DATABASE_ID)
