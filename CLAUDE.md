# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ВАЖНО: Язык

**Весь диалог с пользователем ведётся на русском языке.**

---

## Что это за проект

Автоматизация управления задачами: связка **Notion** <-> **Jira** <-> **Confluence**.

Пользователь ведёт задачи в Notion (доска "Tasks 2026"). Этот проект обеспечивает:
1. При создании задачи через `/notion-task` — автоматическое создание issue в Jira VC (nfware.atlassian.net)
2. Jira Automation (настроено в UI) — автоматическое создание Confluence-страницы с ТЗ при создании issue
3. Sync-скрипт — двусторонняя синхронизация статусов Jira ↔ Notion + прогресс подзадач

### Архитектура потока данных

```
Пользователь → /notion-task (Claude Code skill)
    ├── 1. Создаёт issue в Jira VC (jira_vchen.py create)
    ├── 2. Создаёт подзадачи в Jira (jira_vchen.py create-subtasks) — опционально
    ├── 3. Создаёт страницу в Notion (MCP: notion-create-pages) с Jira Key
    └── Jira Automation (no-code, настроено в Jira UI)
          └── Создаёт Confluence-страницу с шаблоном ТЗ

/notion-status (Claude Code skill)
    ├── Обновляет Status в Notion
    └── Шаг 3.5: transition_issue() → обновляет статус в Jira

Cron (каждые 10 мин, на сервере в tmux):
    jira_notion_sync.py --bidirectional --with-progress
    ├── Jira → Notion: статусы + прогресс подзадач в контент страницы
    └── Notion → Jira: обнаружение ручных изменений статуса в Notion UI
```

---

## Структура пакета

```
taskautomation/           # Python-пакет
├── config.py             # env, маппинги, константы (единственное место)
├── jira_client.py        # класс JiraVCHEN (проект VC, REST API v3)
├── notion_client.py      # класс NotionClient (REST API + Block API)
├── sync.py               # JiraToNotionSync, NotionToJiraSync, ProgressSync
└── cli.py                # единый CLI (main_jira, main_sync)

jira_vchen.py             # shim → taskautomation.cli.main_jira()
jira_notion_sync.py       # shim → taskautomation.cli.main_sync()
deploy/                   # deploy.sh, sync_wrapper.sh, crontab.example
Makefile                  # make sync, make deploy, make test
```

**Shim-файлы** в корне сохраняют обратную совместимость — скиллы ссылаются на абсолютные пути к ним.

### Зависимости между модулями

- `config.py` — загружает `.env`, содержит все маппинги и константы. Импортируется всеми.
- `jira_client.py` ← `config.py` (JiraConfig, маппинги)
- `notion_client.py` ← `config.py` (NotionConfig, get_progress_emoji)
- `sync.py` ← `jira_client.py` + `notion_client.py` + `config.py`
- `cli.py` ← `jira_client.py` + `sync.py`
- `.sync_state.json` — создаётся автоматически, хранит время sync + known_notion_statuses для bidirectional

---

## Запуск скриптов

```bash
PYTHON=/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3
SCRIPT=/Users/nfware/Documents/my_prjcts/task-automation/jira_vchen.py
SYNC=/Users/nfware/Documents/my_prjcts/task-automation/jira_notion_sync.py
```

### Jira CLI (jira_vchen.py)

```bash
$PYTHON $SCRIPT create --title "Название" --description "Описание" --priority Medium --labels "development"
$PYTHON $SCRIPT create-subtasks VC-42 --subtasks '[{"title":"подзадача 1"}]'
$PYTHON $SCRIPT get VC-42
$PYTHON $SCRIPT list-active
$PYTHON $SCRIPT recently-updated --minutes 30
$PYTHON $SCRIPT discover-statuses
$PYTHON $SCRIPT discover-issue-types

# Transitions (для бидирекционального sync)
$PYTHON $SCRIPT transition VC-42 --status "В работе"
$PYTHON $SCRIPT transitions VC-42    # показать доступные переходы
```

### Sync (jira_notion_sync.py)

```bash
$PYTHON $SYNC                          # Jira→Notion, последние 15 мин
$PYTHON $SYNC --full                   # Jira→Notion, все активные
$PYTHON $SYNC --dry-run --full         # dry-run
$PYTHON $SYNC --with-progress          # + прогресс подзадач в контент
$PYTHON $SYNC --reverse                # Notion→Jira только
$PYTHON $SYNC --bidirectional          # оба направления
$PYTHON $SYNC --bidirectional --with-progress --full  # полный двусторонний + прогресс
$PYTHON $SYNC --verbose                # debug logging
```

### Makefile

```bash
make sync          # инкрементальный Jira→Notion
make sync-full     # полный Jira→Notion
make sync-dry      # dry-run
make sync-progress # полный + прогресс
make sync-reverse  # Notion→Jira
make sync-bidi     # двусторонний
make deploy        # rsync на сервер
make test          # pytest
```

---

## Jira VC (nfware.atlassian.net)

- **URL:** `https://nfware.atlassian.net`
- **Проект:** `VC`
- **Board:** `https://nfware.atlassian.net/jira/core/projects/VC/board`
- **API:** REST API v3 (direct requests) + python-jira (для transitions/statuses). Гибридный подход т.к. nfware.atlassian.net deprecated `/rest/api/3/search` — используем `/rest/api/3/search/jql`.
- **Issue types:** Задача, Подзадача (русские названия)
- **Описания:** Atlassian Document Format (ADF), не plain text

### Маппинг статусов

| Notion Status | Jira Status |
|---|---|
| Not started | New |
| Idea | Idea |
| In progress | В работе |
| Hold | Hold |
| Done | Done |
| Archived | (нет маппинга) |

Маппинги определены в `taskautomation/config.py`: `NOTION_TO_JIRA_STATUS`, `JIRA_TO_NOTION_STATUS`.

### Маппинг приоритетов

| Notion (Priority property) | Jira Priority |
|---|---|
| Now | Highest |
| High | High |
| Medium (default) | Medium |
| Low | Low |

### Разрешение конфликтов (bidirectional sync)

**Jira побеждает:** сначала запускается Jira→Notion sync, потом Notion→Jira читает уже обновлённые статусы. Notion→Jira только отлавливает ручные изменения в Notion UI.

---

## Notion

### База "Tasks 2026"

- **Database ID:** `3050c57fd84181a7bb22ee1b23b37c6e`
- **data_source_id (для MCP):** `3050c57f-d841-81b5-98c6-000bda09220f`

### Свойства базы

| Property | Type | Значения |
|----------|------|----------|
| Task name | title | Название задачи (обязательно) |
| Status | status | Not started, Idea, In progress, Hold, Archived, Done |
| Assignee | person | ID пользователя |
| Due | date | date:Due:start, date:Due:end, date:Due:is_datetime |
| Summary | text | Краткое описание |
| Задача | text | Внутренний идентификатор |
| Приоритетность | select | Срочно |
| Приоритетность (1) | select | тест, Корп культура, Обучение, Онбординг, Процессы, Развитие, Рекрутинг |
| Jira Key | text | VC-XX (заполняется автоматически) |
| Priority | select | Now, High, Medium, Low |

---

## Связанные файлы вне проекта

- **`~/.claude/skills/notion-task/SKILL.md`** — Skill для создания задач. Шаги 2.5 и 2.7 вызывают `jira_vchen.py`.
- **`~/.claude/skills/notion-status/SKILL.md`** — Skill для обновления статуса. Шаг 3.5 вызывает `jira_vchen.py transition` для синхронизации в Jira.
- **`~/.claude/skills/notion-daily/SKILL.md`** — Skill для ведения дневных логов в Notion.

---

## Деплой

Сервер: `vchen@10.20.40.232` (Ubuntu 24.04, ARM64, NVIDIA DGX Spark).

Проект живёт в `/home/vchen/automations/task-automation/`. Sync запускается в tmux-сессии `task-sync` (цикл каждые 10 мин). Автозапуск при перезагрузке через `@reboot` crontab → `~/automations/start-all.sh`.

```bash
make deploy                    # rsync на сервер
# Управление на сервере:
ssh vchen@10.20.40.232
~/automations/start-all.sh     # запустить все автоматизации
~/automations/stop-all.sh      # остановить все
tmux attach -t task-sync       # подключиться к sync-сессии
```

---

## Confluence

- **Folder:** `https://nfware.atlassian.net/wiki/spaces/~7120207d46508b6f30445a8f04596b39efdffc/folder/4349460483`
- Настраивается в Jira UI: VC → Project Settings → Automation → Create rule. Подробный шаблон — в `SETUP.md`.

---

## Окружение

- **Python:** 3.9+ (в venv)
- **venv:** `/Users/nfware/Documents/my_prjcts/task-automation/venv/`
- **Зависимости:** jira, requests, python-dotenv
- **macOS:** Darwin 25.2.0

## Контекст: другие проекты

- **`/Users/nfware/Documents/doc-automation/`** — pipeline документации (Confluence → RST → Sphinx). Тот же Jira (nfware.atlassian.net), но проект DOCS. НЕ СМЕШИВАТЬ.
- **`/Users/nfware/Documents/my_prjcts/server-monitor-bot/`** — Telegram-бот мониторинга серверов
- **`/Users/nfware/Documents/my_prjcts/money_analitics/`** — аналитика финансов

Этот проект (`task-automation`) — автономный, не зависит от других проектов.
