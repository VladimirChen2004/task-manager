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
3. Cron-скрипт `jira_notion_sync.py` — синхронизация статусов из Jira обратно в Notion (каждые 10 мин)

### Архитектура потока данных

```
Пользователь → /notion-task (Claude Code skill)
    ├── 1. Создаёт issue в Jira VCHEN (jira_vchen.py create)
    ├── 2. Создаёт подзадачи в Jira (jira_vchen.py create-subtasks) — опционально
    ├── 3. Создаёт страницу в Notion (MCP: notion-create-pages) с Jira Key
    └── Jira Automation (no-code, настроено в Jira UI)
          └── Создаёт Confluence-страницу с шаблоном ТЗ

Cron (каждые 10 мин):
    jira_notion_sync.py
    ├── Читает обновлённые задачи из Jira (через JiraVCHEN)
    ├── Находит страницы в Notion по свойству "Jira Key" (через NotionClient → REST API)
    └── Обновляет Status в Notion
```

**Синхронизация однонаправленная:** Jira → Notion (только статусы). Notion → Jira только при создании задачи.

### Зависимости между модулями

- `jira_notion_sync.py` импортирует `JiraVCHEN` и `JIRA_TO_NOTION_STATUS` из `jira_vchen.py`
- `jira_vchen.py` — самостоятельный модуль (CLI + класс), не зависит от sync-скрипта
- `.sync_state.json` — создаётся автоматически при запуске sync, хранит время последней синхронизации

---

## Запуск скриптов

Всегда использовать python из venv:

```bash
PYTHON=/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3
SCRIPT=/Users/nfware/Documents/my_prjcts/task-automation/jira_vchen.py
SYNC=/Users/nfware/Documents/my_prjcts/task-automation/jira_notion_sync.py
```

### Jira CLI (jira_vchen.py)

```bash
# Создать задачу
$PYTHON $SCRIPT create --title "Название" --description "Описание" --priority Medium --labels "development"

# Создать подзадачи
$PYTHON $SCRIPT create-subtasks VCHEN-42 --subtasks '[{"title":"подзадача 1"},{"title":"подзадача 2"}]'

# Получить задачу
$PYTHON $SCRIPT get VCHEN-42

# Список активных
$PYTHON $SCRIPT list-active

# Недавно обновлённые
$PYTHON $SCRIPT recently-updated --minutes 30

# Обнаружить статусы (для проверки маппинга)
$PYTHON $SCRIPT discover-statuses

# Обнаружить типы задач
$PYTHON $SCRIPT discover-issue-types
```

### Sync (jira_notion_sync.py)

```bash
$PYTHON $SYNC                 # инкрементальный sync (последние 15 мин)
$PYTHON $SYNC --full          # полный sync (все активные задачи)
$PYTHON $SYNC --dry-run --full # dry-run (только показать что изменилось бы)
$PYTHON $SYNC --minutes 30    # custom окно
$PYTHON $SYNC --verbose       # debug logging
```

### Установка зависимостей

```bash
$PYTHON -m pip install -r /Users/nfware/Documents/my_prjcts/task-automation/requirements.txt
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

**ВАЖНО:** Маппинг может не совпадать с реальными статусами в VCHEN. После заполнения credentials запустить `discover-statuses` и при необходимости скорректировать словари `NOTION_TO_JIRA_STATUS` и `JIRA_TO_NOTION_STATUS` в `jira_vchen.py`.

### Маппинг приоритетов

| Notion (Priority property) | Jira Priority |
|---|---|
| Наивысшая срочность | Highest |
| Срочно | High |
| Средняя срочность (default) | Medium |
| Не срочно | Low |
| Бессрочно | Lowest |

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

- **`~/.claude/skills/notion-task/SKILL.md`** — Claude Code skill для создания задач. Шаги 2.5 и 2.7 вызывают скрипты из этого проекта.
- **`~/.claude/skills/notion-status/SKILL.md`** — Skill для обновления статуса задач в Notion.
- **`~/.claude/skills/notion-daily/SKILL.md`** — Skill для ведения дневных логов в Notion.

---

## Confluence (автоматическое ТЗ)

Настраивается в Jira UI (не код): Jira VCHEN → Project Settings → Automation → Create rule. Подробный шаблон — в `SETUP.md` шаг 4.

---

## Что нужно сделать (TODO)

Подробная инструкция настройки — в `SETUP.md`. Краткий чеклист:

### Обязательно:
1. Заполнить `.env` (JIRA_VCHEN_EMAIL, JIRA_VCHEN_API_TOKEN, NOTION_API_TOKEN)
2. Создать Notion Internal Integration и расшарить с ней базу "Tasks 2026"
3. Добавить свойства Jira Key (text) и Priority (select) в Notion базу
4. Проверить маппинг статусов через `discover-statuses`

### Желательно:
5. Настроить Jira Automation для Confluence (инструкция в SETUP.md шаг 4)
6. Деплой sync на сервер — cron каждые 10 мин
7. Тестовый прогон через `/notion-task`

### Будущие улучшения (вторая итерация):
8. Синхронизация прогресса подзадач в контент Notion-страницы (прогресс-бар)
9. Обратная синхронизация Notion → Jira (при изменении статуса в Notion)

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
