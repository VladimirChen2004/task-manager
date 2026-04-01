"""Unified CLI for task-automation."""

import argparse
import json
import logging
import sys


def main_jira():
    """CLI entry point for Jira operations (backward-compatible with jira_vchen.py)."""
    from .jira_client import JiraVCHEN

    parser = argparse.ArgumentParser(description="Jira VCHEN client")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # create
    p_create = subparsers.add_parser("create", help="Create issue")
    p_create.add_argument("--title", required=True, help="Issue title")
    p_create.add_argument("--description", default="", help="Description")
    p_create.add_argument(
        "--priority",
        default="Medium",
        choices=["Highest", "High", "Medium", "Low", "Lowest"],
    )
    p_create.add_argument(
        "--labels", default="", help="Comma-separated labels"
    )
    p_create.add_argument(
        "--notion-url", default=None, help="Notion page URL"
    )
    p_create.add_argument(
        "--due-date", default=None, help="Due date (YYYY-MM-DD)"
    )

    # create-subtasks
    p_sub = subparsers.add_parser("create-subtasks", help="Create subtasks")
    p_sub.add_argument(
        "parent_key", help="Parent issue key (e.g., VCHEN-42)"
    )
    p_sub.add_argument(
        "--subtasks",
        required=True,
        help='JSON array: [{"title":"..."},{"title":"..."}]',
    )

    # get
    p_get = subparsers.add_parser("get", help="Get issue details")
    p_get.add_argument("issue_key", help="Issue key (e.g., VCHEN-42)")

    # list-active
    subparsers.add_parser("list-active", help="List active issues")

    # recently-updated
    p_recent = subparsers.add_parser(
        "recently-updated", help="Recently updated issues"
    )
    p_recent.add_argument(
        "--minutes", type=int, default=10, help="Minutes back"
    )

    # discover-statuses
    subparsers.add_parser(
        "discover-statuses", help="Discover available statuses"
    )

    # discover-issue-types
    subparsers.add_parser(
        "discover-issue-types", help="Discover issue types"
    )

    # transition
    p_trans = subparsers.add_parser(
        "transition", help="Transition issue to new status"
    )
    p_trans.add_argument(
        "issue_key", help="Issue key (e.g., VCHEN-42)"
    )
    p_trans.add_argument(
        "--status", required=True, help="Target status (e.g., 'In Progress')"
    )

    # transitions (list available)
    p_trans_list = subparsers.add_parser(
        "transitions", help="List available transitions for issue"
    )
    p_trans_list.add_argument(
        "issue_key", help="Issue key (e.g., VCHEN-42)"
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        client = JiraVCHEN()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.command == "create":
        labels = (
            [l.strip() for l in args.labels.split(",") if l.strip()]
            if args.labels
            else None
        )
        result = client.create_issue(
            title=args.title,
            description=args.description,
            priority=args.priority,
            labels=labels,
            notion_url=args.notion_url,
            due_date=args.due_date,
        )
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "create-subtasks":
        subtasks = json.loads(args.subtasks)
        result = client.create_subtasks(args.parent_key, subtasks)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "get":
        result = client.get_issue(args.issue_key)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "list-active":
        results = client.get_all_active()
        for r in results:
            prog = r["progress"]
            prog_str = (
                f" [{prog['done']}/{prog['total']}]"
                if prog["total"] > 0
                else ""
            )
            print(
                f"  {r['key']:12s} {r['status']:15s}{prog_str}  {r['summary']}"
            )

    elif args.command == "recently-updated":
        results = client.get_recently_updated(since_minutes=args.minutes)
        for r in results:
            print(
                f"  {r['key']:12s} {r['status']:15s}  {r['summary']}"
            )

    elif args.command == "discover-statuses":
        statuses = client.discover_statuses()
        print("Available statuses:")
        for s in statuses:
            print(f"  {s}")

    elif args.command == "discover-issue-types":
        types = client.discover_issue_types()
        print("Available issue types:")
        for t in types:
            print(f"  {t}")

    elif args.command == "transition":
        success = client.transition_issue(args.issue_key, args.status)
        if success:
            print(f"OK: {args.issue_key} transitioned to '{args.status}'")
        else:
            print(
                f"FAILED: could not transition {args.issue_key} to '{args.status}'",
                file=sys.stderr,
            )
            # Show available transitions for debugging
            transitions = client.get_available_transitions(args.issue_key)
            if transitions:
                print("Available transitions:", file=sys.stderr)
                for t in transitions:
                    print(
                        f"  {t['name']} → {t['to']}",
                        file=sys.stderr,
                    )
            sys.exit(1)

    elif args.command == "transitions":
        transitions = client.get_available_transitions(args.issue_key)
        print(f"Available transitions for {args.issue_key}:")
        for t in transitions:
            print(f"  {t['name']} → {t['to']} (id={t['id']})")


def main_sync():
    """CLI entry point for sync operations (backward-compatible with jira_notion_sync.py)."""
    from .sync import run_sync

    parser = argparse.ArgumentParser(description="Jira ↔ Notion sync")
    parser.add_argument(
        "--full", action="store_true", help="Full sync (all active issues)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=15,
        help="Time window for incremental sync",
    )
    parser.add_argument(
        "--with-progress",
        action="store_true",
        help="Also sync subtask progress to page content",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Sync Notion → Jira only",
    )
    parser.add_argument(
        "--bidirectional",
        action="store_true",
        help="Sync both directions (Jira → Notion, then Notion → Jira)",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="One-time: create Jira issues for all Notion tasks without Jira Key",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    success = run_sync(
        full=args.full,
        dry_run=args.dry_run,
        minutes=args.minutes,
        with_progress=args.with_progress,
        reverse=args.reverse,
        bidirectional=args.bidirectional,
        migrate=args.migrate,
    )
    if not success:
        sys.exit(1)


def main_daemon():
    """CLI entry point for sync daemon."""
    from .config import DAEMON_INTERVAL_SECONDS
    from .daemon import SyncDaemon

    parser = argparse.ArgumentParser(description="Bidirectional sync daemon")
    parser.add_argument(
        "--interval", type=int, default=DAEMON_INTERVAL_SECONDS,
        help=f"Sync interval in seconds (default: {DAEMON_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Disable subtask progress sync",
    )
    parser.add_argument(
        "--no-creation", action="store_true",
        help="Disable auto-creation of issues/pages",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview mode",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    daemon = SyncDaemon(
        interval=args.interval,
        with_progress=not args.no_progress,
        with_creation=not args.no_creation,
        dry_run=args.dry_run,
    )
    daemon.run()
