PYTHON = venv/bin/python3

.PHONY: sync sync-full sync-dry sync-progress sync-reverse sync-bidi \
       daemon daemon-dry migrate migrate-dry deploy test

sync:
	$(PYTHON) jira_notion_sync.py

sync-full:
	$(PYTHON) jira_notion_sync.py --full

sync-dry:
	$(PYTHON) jira_notion_sync.py --dry-run --full

sync-progress:
	$(PYTHON) jira_notion_sync.py --full --with-progress

sync-reverse:
	$(PYTHON) jira_notion_sync.py --reverse

sync-bidi:
	$(PYTHON) jira_notion_sync.py --bidirectional

daemon:
	$(PYTHON) sync_daemon.py --verbose

daemon-dry:
	$(PYTHON) sync_daemon.py --dry-run --verbose

migrate:
	$(PYTHON) jira_notion_sync.py --migrate --verbose

migrate-dry:
	$(PYTHON) jira_notion_sync.py --migrate --dry-run --verbose

deploy:
	bash deploy/deploy.sh

test:
	$(PYTHON) -m pytest tests/ -v
