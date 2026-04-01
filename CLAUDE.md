# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ВАЖНО: Язык

**Весь диалог с пользователем ведётся на русском языке.**

---

## Что это за проект

Автоматизация управления задачами: связка **Notion** <-> **Jira** <-> **Confluence**.

Пользователь ведёт задачи в Notion (доска "Tasks 2026"). Демон на сервере автоматически:
1. Создаёт Jira issue для новых Notion-задач (и наоборот)
2. Синхронизирует статусы двунаправленно (Jira wins при конфликте)
3. Синхронизирует подзадачи Jira ↔ to-do чекбоксы Notion
4. Создаёт/обновляет Confluence-страницы (ТЗ, прогресс)
5. Поддерживает гиперссылки между всеми тремя системами

### Где что работает

| Компонент | Где | Как |
|-----------|-----|-----|
| Демон (`sync_daemon.py`) | **Сервер** `vchen@10.20.40.232` | tmux `task-sync`, цикл ~60 сек |
| Разработка | **Ноутбук** (macOS) | venv, `make deploy` → rsync на сервер |
| Skills (`/notion-task`, etc.) | **Ноутбук** | Claude Code вызывает локальные скрипты |

### Архитектура потока данных

```
Daemon (на сервере, tmux task-sync, каждые 60 сек):
    sync_daemon.py --verbose
    ├── Фаза 1: Notion→Jira creation (новые задачи без Jira Key)
    ├── Фаза 2: Jira→Notion creation (новые issue без Notion-страницы)
    ├── Фаза 3: BidirectionalSync (статус/приоритет: delta + timestamp при конфликте)
    ├── Фаза 4: Deleted page detection + template backfill
    ├── Фаза 5: Subtask↔Todo sync (подзадачи Jira ↔ чекбоксы Notion)
    ├── Фаза 6: Confluence sync (создание/обновление страниц, прогресс)
    └── Фаза 7: SectionSync (контент Notion ↔ Confluence)

Ручное создание:
    /notion-task (Claude Code skill) → Jira + Notion + Confluence
    /notion-status → обновление статуса
    /notion-daily → дневные логи
```

---

## Структура пакета

```
taskautomation/               # Python-пакет
├── config.py                  # env, маппинги, константы (единственное место)
├── jira_client.py             # класс JiraVCHEN (проект VC, REST API v3, ADF)
├── notion_client.py           # класс NotionClient (REST API + Block API)
├── confluence_client.py       # класс ConfluenceClient (REST API v1, XHTML)
├── sync.py                    # 6 sync-классов (creators, syncs, ConfluenceSync)
├── daemon.py                  # SyncDaemon — фоновый цикл с graceful shutdown
└── cli.py                     # единый CLI (main_jira, main_sync, main_daemon)

jira_vchen.py                  # shim → taskautomation.cli.main_jira()
jira_notion_sync.py            # shim → taskautomation.cli.main_sync()
sync_daemon.py                 # shim → taskautomation.cli.main_daemon()
cleanup_and_recreate.py        # миграционный скрипт (удаление + пересоздание)
deploy/                        # deploy.sh, sync_wrapper.sh, crontab.example
Makefile                       # make daemon, make sync, make deploy, make test
```

**Shim-файлы** в корне — скиллы ссылаются на абсолютные пути к ним.

### Зависимости между модулями

```
config.py ← загружает .env, все маппинги и константы
    ↑
jira_client.py ← JiraConfig, маппинги
notion_client.py ← NotionConfig, get_progress_emoji
confluence_client.py ← ConfluenceConfig
    ↑
sync.py ← jira_client + notion_client + confluence_client + config
    ↑
daemon.py ← sync + все клиенты
    ↑
cli.py ← daemon + sync + jira_client
```

---

## Запуск скриптов

```bash
PYTHON=/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3
```

### Jira CLI

```bash
$PYTHON jira_vchen.py create --title "Название" --description "Описание" --priority Medium
$PYTHON jira_vchen.py create-subtasks VC-42 --subtasks '[{"title":"подзадача 1"}]'
$PYTHON jira_vchen.py get VC-42
$PYTHON jira_vchen.py list-active
$PYTHON jira_vchen.py recently-updated --minutes 30
$PYTHON jira_vchen.py discover-statuses
$PYTHON jira_vchen.py transition VC-42 --status "В работе"
```

### Daemon

```bash
$PYTHON sync_daemon.py --verbose              # запуск
$PYTHON sync_daemon.py --dry-run --verbose    # dry-run
$PYTHON sync_daemon.py --no-creation          # только статусы
$PYTHON sync_daemon.py --interval 60          # кастомный интервал
```

### Sync (одноразовый)

```bash
$PYTHON jira_notion_sync.py                   # Jira→Notion, последние 15 мин
$PYTHON jira_notion_sync.py --full            # все активные
$PYTHON jira_notion_sync.py --bidirectional   # оба направления
$PYTHON jira_notion_sync.py --with-progress   # + прогресс подзадач
$PYTHON jira_notion_sync.py --migrate         # создать Jira для всех Notion без Key
$PYTHON jira_notion_sync.py --dry-run --full  # dry-run
```

### Makefile

```bash
make daemon        # запуск демона (verbose)
make daemon-dry    # демон в dry-run
make sync          # инкрементальный sync
make sync-full     # полный sync
make sync-bidi     # двусторонний
make deploy        # rsync на сервер
make test          # pytest
```

---

## Деплой и сервер

**Сервер:** `vchen@10.20.40.232` (Ubuntu 24.04, ARM64, NVIDIA DGX Spark)
**Путь:** `/home/vchen/automations/task-automation/`
**tmux-сессия:** `task-sync`

### Деплой с ноутбука

```bash
make deploy                    # rsync на сервер + pip install
```

### Управление на сервере

```bash
ssh vchen@10.20.40.232
~/automations/start-all.sh     # запустить все автоматизации
~/automations/stop-all.sh      # остановить все
tmux attach -t task-sync       # подключиться к демону (логи)
```

**Автозапуск:** `@reboot` crontab → `~/automations/start-all.sh`

### Процесс обновления

1. Изменить код на ноутбуке
2. `make deploy` (rsync)
3. SSH → перезапустить демон в tmux (или `stop-all.sh && start-all.sh`)

---

## Jira

- **URL:** `https://nfware.atlassian.net`
- **Проект:** `VC`
- **Board:** `https://nfware.atlassian.net/jira/core/projects/VC/board`
- **API:** REST API v3 (direct requests) + python-jira (для transitions). Endpoint: `/rest/api/3/search/jql` (не deprecated `/search`).
- **Issue types:** Задача, Подзадача (русские названия)
- **Описания:** Atlassian Document Format (ADF) с гиперссылками (не plain text)

### Маппинг статусов

| Notion Status | Jira Status |
|---|---|
| Not started | Новое |
| Idea | Idea |
| In progress | В работе |
| Hold | Hold |
| Done | Готово |
| Archived | Archieve |

Маппинги в `config.py`: `NOTION_TO_JIRA_STATUS`, `JIRA_TO_NOTION_STATUS`.

### Маппинг приоритетов

| Notion | Jira |
|---|---|
| Now | Highest |
| High | High |
| Medium (default) | Medium |
| Low | Low |

### Разрешение конфликтов (BidirectionalSync)

Используется delta-детекция через `known_notion_statuses` (dual format: `{"notion": ..., "jira": ...}`):

| Notion изменился? | Jira изменился? | Действие |
|---|---|---|
| Нет | Нет | Skip |
| Да | Нет | Notion → Jira |
| Нет | Да | Jira → Notion |
| Да | Да | Timestamp решает (CONFLICT) |
| Первый раз | — | Записать обе стороны, не синкать |

---

## Notion

### База "Tasks 2026"

- **Database ID:** `3050c57fd84181a7bb22ee1b23b37c6e`
- **data_source_id (для MCP):** `3050c57f-d841-81b5-98c6-000bda09220f`

### Свойства базы

| Property | Type | Значения |
|----------|------|----------|
| Task name | title | Название задачи |
| Status | status | Not started, Idea, In progress, Hold, Archived, Done |
| Summary | text | Краткое описание |
| Jira Key | text | VC-XX (заполняется автоматически) |
| Priority | select | Now, High, Medium, Low |
| Due | date | Срок задачи |
| Assignee | person | ID пользователя |

### Шаблон страницы (создаётся демоном)

```
1. Toggle "План выполнения" — to-do чекбоксы (ПЕРВЫМ для быстрого доступа)
2. Callout 🔗 — гиперссылки: Jira: [VC-XX] | Confluence: [ТЗ]
3. Divider
4. Toggle "Минимальный функционал (MVP)" — placeholder
5. Toggle "Результат" — заполняется по итогу
6. Toggle "Заметки / Лог" — заметки по ходу работы
7. Toggle "Описание задачи" — summary (если есть)
8. Callout 🤖 — "Создано автоматически" (для авто-созданных)
```

---

## Confluence

- **Инстанс:** тот же `nfware.atlassian.net/wiki`
- **Space:** `~7120207d46508b6f30445a8f04596b39efdffc` (Personal — Vladimir Chen)
- **Parent page:** ID `4349132807` ("VC Tasks")
- **Folder:** `https://nfware.atlassian.net/wiki/spaces/~7120207d46508b6f30445a8f04596b39efdffc/folder/4349460483`
- **API:** REST API v1 (storage format / XHTML), та же auth (email + api_token)
- **Jira Automation** (в Jira UI) также создаёт Confluence-страницы → `find_or_create_page` избегает дубликатов

### Шаблон Confluence страницы (XHTML)

```
1. Ссылки: [VC-XX (Jira)] | [Notion]  — гиперссылки
2. <hr/>
3. Описание задачи (summary)
4. Минимальный функционал (MVP) — что нужно сделать как минимум
5. Техническое задание — placeholder для ТЗ
6. Прогресс — ac:structured-macro status (NOT STARTED / IN PROGRESS / DONE)
7. План выполнения — ac:task-list с подзадачами
8. Заметки / Лог — заметки по ходу работы
```

### Jira описание (ADF)

```
1. Параграф с описанием задачи (summary)
2. Rule (разделитель)
3. Параграф: [Notion](url) | [Confluence (ТЗ)](url) — гиперссылки через ADF marks
```

---

## Гиперссылки между системами

Все три системы связаны кликабельными ссылками:

| Откуда | Куда | Как |
|--------|------|-----|
| **Notion** → Jira | 🔗 callout: `Jira: [VC-XX]` | Notion rich_text link |
| **Notion** → Confluence | 🔗 callout: `Confluence: [ТЗ]` | Notion rich_text link |
| **Jira** → Notion | ADF paragraph: `[Notion](url)` | ADF link mark |
| **Jira** → Confluence | ADF paragraph: `[Confluence (ТЗ)](url)` | ADF link mark |
| **Confluence** → Jira | `<a href>` в секции Ссылки | XHTML |
| **Confluence** → Notion | `<a href>` в секции Ссылки | XHTML |

---

## State файл (.sync_state.json)

```json
{
  "known_notion_statuses": {"VC-47": {"notion": "Done", "jira": "Done"}, ...},
  "known_notion_priorities": {"VC-47": {"notion": "Medium", "jira": "Medium"}, ...},
  "template_backfilled": ["VC-47", "VC-51", ...],
  "missing_keys": {},
  "subtask_todos": {"VC-47": {"page_last_edited": "...", "todos": {...}, "subtask_statuses": {...}}},
  "confluence_linked_keys": ["VC-47", "VC-51", ...],
  "daemon_last_cycle": "2026-02-19T21:35:09",
  "daemon_cycle_count": 5,
  "daemon_last_cycle_seconds": 53.0
}
```

- `known_notion_statuses` — dual format для BidirectionalSync (delta-детекция изменений с обеих сторон)
- `known_notion_priorities` — аналогично для приоритетов
- `template_backfilled` — страницы, которые уже получили все template-секции
- `subtask_todos` — кеш для Subtask↔Todo sync
- `confluence_linked_keys` — какие страницы уже прошли linking (callout + Jira desc + Confluence body)

---

## Связанные Skills

- **`~/.claude/skills/notion-task/SKILL.md`** — создание задач (Jira + Notion + Confluence)
- **`~/.claude/skills/notion-status/SKILL.md`** — обновление статуса (Notion + Jira transition)
- **`~/.claude/skills/notion-daily/SKILL.md`** — дневные логи в Notion

---

## Окружение

- **Python:** 3.9+ (в venv)
- **venv (ноутбук):** `/Users/nfware/Documents/my_prjcts/task-automation/venv/`
- **venv (сервер):** `/home/vchen/automations/task-automation/venv/`
- **Зависимости:** jira, requests, python-dotenv
- **macOS (ноутбук):** Darwin 25.2.0
- **Linux (сервер):** Ubuntu 24.04, ARM64

## Контекст: другие проекты

- **`doc-automation/`** — pipeline документации (Confluence → RST → Sphinx). Тот же Jira (nfware.atlassian.net), но проект DOCS. НЕ СМЕШИВАТЬ.
- **`server-monitor-bot/`** — Telegram-бот мониторинга серверов
- **`money_analitics/`** — аналитика финансов
