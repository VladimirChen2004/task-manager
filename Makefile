PYTHON = venv/bin/python3

.PHONY: sync sync-full sync-dry sync-progress sync-reverse sync-bidi deploy test

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

deploy:
	bash deploy/deploy.sh

test:
	$(PYTHON) -m pytest tests/ -v
