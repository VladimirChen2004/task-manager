# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ВАЖНО: Язык

**Весь диалог с пользователем ведётся на русском языке.**

---

## Что это за проект

Автоматизация управления задачами: связка **Notion** <-> **Jira** <-> **Confluence**.

Пользователь ведёт задачи в Notion (доска "Tasks 2026"). Этот проект обеспечивает:
1. При создании задачи через `/notion-task` — автоматическое создание issue в Jira VCHEN
2. Jira Automation (настроено в UI) — автоматическое создание Confluence-страницы с ТЗ при создании issue
3. Cron-скрипт — двусторонняя синхронизация статусов Jira ↔ Notion + прогресс подзадач

### Архитектура потока данных

```
Пользователь → /notion-task (Claude Code skill)
    ├── 1. Создаёт issue в Jira VCHEN (jira_vchen.py create)
    ├── 2. Создаёт подзадачи в Jira (jira_vchen.py create-subtasks) — опционально
    ├── 3. Создаёт страницу в Notion (MCP: notion-create-pages) с Jira Key
    └── Jira Automation (no-code, настроено в Jira UI)
          └── Создаёт Confluence-страницу с шаблоном ТЗ

/notion-status (Claude Code skill)
    ├── Обновляет Status в Notion
    └── Шаг 3.5: transition_issue() → обновляет статус в Jira

Cron (каждые 10 мин):
    jira_notion_sync.py --bidirectional --with-progress
    ├── Jira → Notion: статусы + прогресс подзадач в контент страницы
    └── Notion → Jira: обнаружение ручных изменений статуса в Notion UI
```

---

## Структура пакета

```
taskautomation/           # Python-пакет
├── config.py             # env, маппинги, константы (единственное место)
├── jira_client.py        # класс JiraVCHEN
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
$PYTHON $SCRIPT create-subtasks VCHEN-42 --subtasks '[{"title":"подзадача 1"}]'
$PYTHON $SCRIPT get VCHEN-42
$PYTHON $SCRIPT list-active
$PYTHON $SCRIPT recently-updated --minutes 30
$PYTHON $SCRIPT discover-statuses
$PYTHON $SCRIPT discover-issue-types

# Transitions (для бидирекционального sync)
$PYTHON $SCRIPT transition VCHEN-42 --status "In Progress"
$PYTHON $SCRIPT transitions VCHEN-42    # показать доступные переходы
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

## Jira VCHEN

- **URL:** `https://vchen.atlassian.net`
- **Проект:** `VCHEN`
- **Это ОТДЕЛЬНЫЙ инстанс** от nfware.atlassian.net (DOCS проект в doc-automation — другая история). НЕ СМЕШИВАТЬ.
- **API:** python-jira library, basic auth (email + API token)

### Маппинг статусов

| Notion Status | Jira Status |
|---|---|
| Not started | To Do |
| Idea | Backlog |
| In progress | In Progress |
| Hold | On Hold |
| Done | Done |
| Archived | (нет маппинга) |

Маппинги определены в `taskautomation/config.py`: `NOTION_TO_JIRA_STATUS`, `JIRA_TO_NOTION_STATUS`.

**ВАЖНО:** После заполнения credentials запустить `discover-statuses` и при необходимости скорректировать словари в `config.py`.

### Маппинг приоритетов

| Notion (Priority property) | Jira Priority |
|---|---|
| Наивысшая срочность | Highest |
| Срочно | High |
| Средняя срочность (default) | Medium |
| Не срочно | Low |
| Бессрочно | Lowest |

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
| Jira Key | text | VCHEN-XX (заполняется автоматически) |
| Priority | select | Наивысшая срочность, Срочно, Средняя срочность, Не срочно, Бессрочно |

---

## Связанные файлы вне проекта

- **`~/.claude/skills/notion-task/SKILL.md`** — Skill для создания задач. Шаги 2.5 и 2.7 вызывают `jira_vchen.py`.
- **`~/.claude/skills/notion-status/SKILL.md`** — Skill для обновления статуса. Шаг 3.5 вызывает `jira_vchen.py transition` для синхронизации в Jira.
- **`~/.claude/skills/notion-daily/SKILL.md`** — Skill для ведения дневных логов в Notion.

---

## Деплой

Сервер: Ubuntu (Nvidia Spark GX10), cron каждые 10 мин.

```bash
make deploy                    # rsync на сервер
# Далее на сервере:
nano /home/nfware/task-automation/.env    # заполнить credentials
crontab -e                     # добавить из deploy/crontab.example
```

---

## Confluence (автоматическое ТЗ)

Настраивается в Jira UI (не код): Jira VCHEN → Project Settings → Automation → Create rule. Подробный шаблон — в `SETUP.md` шаг 4.

---

## Окружение

- **Python:** 3.9+ (в venv)
- **venv:** `/Users/nfware/Documents/my_prjcts/task-automation/venv/`
- **Зависимости:** jira, requests, python-dotenv
- **macOS:** Darwin 25.2.0

## Контекст: другие проекты

- **`/Users/nfware/Documents/doc-automation/`** — pipeline документации (Confluence → RST → Sphinx). Другой Jira (nfware.atlassian.net, проект DOCS). НЕ СМЕШИВАТЬ.
- **`/Users/nfware/Documents/my_prjcts/server-monitor-bot/`** — Telegram-бот мониторинга серверов
- **`/Users/nfware/Documents/my_prjcts/money_analitics/`** — аналитика финансов

Этот проект (`task-automation`) — автономный, не зависит от других проектов.
