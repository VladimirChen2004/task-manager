# Рефакторинг task-automation: план

Дата: 2026-05-06 (обновлён 2026-05-07)
Статус: **В работе — Фаза 0**

---

## Контекст

Демон синхронизирует задачи между Notion ↔ Jira ↔ Confluence (7 фаз, цикл ~5 мин).
Текущие проблемы: destructive writes, скрытые source priorities, отсутствие тестов,
дублирование API вызовов, ненадёжный conflict resolution.

**Принцип порядка работ:** сначала прекращаем портить данные, потом покрываем тестами,
потом рефакторим архитектуру.

---

## Фаза 0: P0 Safety Fixes (прекратить портить данные)

Цель: демон перестаёт делать destructive writes. Ничего не ломает — только фиксы.

**Порядок выполнения внутри фазы** (от самого опасного к менее):

### 0.3 → Шаг 1: find_or_create_page — не перезаписывать existing page шаблоном

**Приоритет: CRITICAL — самый разрушительный баг.**
Если existing Confluence page перезаписывается шаблоном, пользовательский текст
исчезает целиком. Хуже любого неверного checkbox.

**Проблема:** `ConfluenceClient.find_or_create_page()` при нахождении существующей
страницы **перезаписывает** её нашим шаблоном (строки 121-126). Если пользователь
отредактировал страницу в Confluence — всё теряется при следующем вызове.

**Что делать:**
- [ ] `find_or_create_page()` при нахождении existing → вернуть as-is, НЕ обновлять body
- [ ] Если нужно добавить недостающие секции — делать через `replace_section`
  по одной, а не full body overwrite
- [ ] Добавить параметр `update_if_exists: bool = False` для явного контроля

**Файлы:** `confluence_client.py` (find_or_create_page)

### 0.2 → Шаг 2: Confluence plan — убрать no-action rebuild + скрытый Jira приоритет

**Приоритет: HIGH — каждый цикл может тихо перетирать Confluence plan через Jira fallback.**
Нельзя считать 0.1 fully fixed, пока этот баг не закрыт — общий Confluence rebuild
может сделать те же самые destructive writes обходным путём.

**Проблема A:** `_update_confluence_from_unified()` вызывается ВСЕГДА когда есть
Confluence page, даже если actions пустой (строка 1354-1359 в _sync_page).

**Проблема B:** В `_update_confluence_from_unified` приоритет: override → **jira** → conf → notion.
Jira побеждает Confluence по дефолту — скрытый source priority.

**Что делать:**
- [ ] Не вызывать `_update_confluence_from_unified()` если нет conf-related actions
  и нет новых items для Confluence
- [ ] Изменить приоритет на: override → **conf (как есть)** → jira → notion
  (Confluence данные сохраняются если нет явного override)

**Что НЕ делать сейчас (→ Фаза 2):**
- Hash-normalization для сравнения — это оптимизация, не safety fix

**Файлы:** `sync.py` (SubtaskTodoSync._sync_page, _update_confluence_from_unified)

### 0.1 → Шаг 3: SubtaskTodoSync baseline — верификация через тесты

**Приоритет: MEDIUM — код уже пофикшен 2026-05-06, нужна верификация.**

**Что уже сделано:**
- [x] При `not known` — ТОЛЬКО создавать отсутствующие items, НИКОГДА не менять existing
- [x] При `not changed` (нет delta vs prev) — не трогать, записать baseline

**Что делать:**
- [ ] Написать unit-тесты подтверждающие что фикс работает (не просто читать код):
  - empty known + sources расходятся → no check/uncheck/close/reopen actions
  - no delta + sources расходятся → no actions
- [ ] Проверить что `_update_confluence_from_unified()` при пустых overrides
  не обходит baseline-only через Jira fallback (зависит от 0.2)

**Файлы:** `sync.py`, `tests/test_subtask_sync.py`

### 0.5 → Шаг 4: SectionSync converter idempotency тест

**Приоритет: MEDIUM — ложные diff'ы вызывают лишние writes каждый цикл.**

**Проблема:** Логи показывают `MVP → Confluence` каждый цикл. Вероятно
roundtrip `Notion → XHTML → Notion → XHTML` не idempotent.

**Что делать:**
- [ ] Написать idempotency тест: roundtrip должен давать одинаковый hash
- [ ] Если тест красный — зафиксировать, починить в Фазе 2
- [ ] Добавить guard в SectionSync: если direction flip-flopped 2+ цикла подряд
  → skip + warn (временный костыль до фикса конвертера)

**Файлы:** `content_converter.py`, `sync.py` (SectionSync), `tests/test_converter.py`

### 0.4 → Шаг 5: replace_toggle_content — idempotency guard

**Приоритет: MEDIUM — destructive write без precondition.**

**Проблема:** `NotionClient.replace_toggle_content()` делает delete all children +
append new. Нет проверки "а контент вообще отличается?".

**Что делать сейчас:**
- [ ] Idempotency guard: hash текущего контента == hash нового → skip, не трогать
- [ ] Precondition check: перед delete проверить что toggle всё ещё существует

**Что НЕ делать сейчас (→ позже):**
- Полный patch-подход (diff blocks, update/add/delete по одному) — это Фаза 5
- Backup в known state — слабая защита, не P0

**Файлы:** `notion_client.py` (replace_toggle_content)

---

## Фаза 1: Тесты на P0 кейсы

Цель: покрыть safety-инварианты unit-тестами. Тесты без реальных API — mock/simulation.

### 1.1 SubtaskTodoSync safety tests

- [ ] **test_empty_known_no_updates:** empty known + Notion/Jira/Conf расходятся
  → actions содержат только creates для отсутствующих items, NO check/uncheck/close/reopen
- [ ] **test_no_delta_no_updates:** known есть, sources расходятся, но ни один
  не изменился vs known → no actions
- [ ] **test_single_source_changed:** Notion checked, others not → propagate to Jira+Conf
- [ ] **test_conflict_resolution_by_timestamp:** Notion + Jira changed differently
  → latest timestamp wins
- [ ] **test_new_item_in_one_source:** item в Confluence, нет в Jira/Notion
  → create subtask + create todo, checked берётся из Confluence
- [ ] **test_deleted_subtask_not_recreated:** item был в known, пропал из Jira
  → remove_from_known, не создавать заново

### 1.2 Confluence safety tests

- [ ] **test_find_or_create_no_overwrite:** existing page → возвращается as-is
- [ ] **test_confluence_plan_no_rewrite_without_changes:** unified без conf-actions
  → Confluence plan не обновляется
- [ ] **test_confluence_plan_preserves_conf_state:** если нет override для item,
  Confluence checked сохраняется (не заменяется Jira значением)

### 1.3 SectionSync idempotency tests

- [ ] **test_notion_xhtml_roundtrip:** Notion → XHTML → Notion → XHTML = same hash
- [ ] **test_confluence_notion_roundtrip:** XHTML → Notion → XHTML = same hash
- [ ] **test_no_flipflop:** SectionSync с одним и тем же контентом → 0 syncs

### 1.4 Тестовая инфраструктура

- [ ] Создать `tests/` директорию
- [ ] Mock-классы для JiraVCHEN, NotionClient, ConfluenceClient
- [ ] Fixtures с реальными примерами данных из production state

**Файлы:** `tests/test_subtask_sync.py`, `tests/test_confluence_safety.py`,
`tests/test_section_sync.py`, `tests/conftest.py`

---

## Фаза 2: Content Converter Idempotency

Цель: `content_converter.py` даёт стабильные roundtrip-результаты.

### 2.1 Аудит конвертера

- [ ] Прогнать roundtrip на реальных секциях из production (MVP, Описание, Заметки)
- [ ] Выявить расхождения: пробелы, порядок атрибутов, потерянные annotations,
  лишние теги
- [ ] Зафиксировать в тестах

### 2.2 Фиксы конвертера

- [ ] Нормализовать whitespace перед hash-сравнением
- [ ] Стабилизировать порядок атрибутов
- [ ] Обработка edge cases: пустые параграфы, nested lists, code blocks,
  ac:structured-macro

### 2.3 Нормализатор для hash-сравнения

- [ ] Отдельная функция `normalize_for_comparison(xhtml) -> str` которая
  стрипает несущественные различия
- [ ] Использовать её в `compute_content_hash()` вместо простого `" ".join(split())`

**Файлы:** `content_converter.py`, `tests/test_converter.py`

---

## Фаза 3: Stable Identity для Plan Items

Цель: items в "План выполнения" имеют стабильную identity, не зависящую от title.

### 3.1 Проблема

Сейчас matching по `title.strip().lower()`. Проблемы:
- Rename → потеря связи (старый unmatched → delete, новый → create = потеря status)
- Дубликаты titles → неверная связка
- Case/punctuation → false mismatch

### 3.2 Решение: sync_id binding

- [ ] Хранить в known state маппинг:
  ```json
  {
    "sync_id": "uuid",
    "jira_key": "VCSUB-127",
    "notion_block_id": "abc123",
    "confluence_task_uuid": "def456",
    "title": "Текст задачи",
    "title_hash": "sha256[:8]"
  }
  ```
- [ ] Primary match: по `sync_id` через stored bindings
- [ ] Fallback: по title (для новых items)
- [ ] Rename detection: item с known `notion_block_id` но другим title
  → rename в Jira/Confluence, обновить binding

**Файлы:** `sync.py` (SubtaskTodoSync), `.sync_state.json` schema

---

## Фаза 4: Baseline/Journal Schema (новый known state)

Цель: known state = "last seen snapshots + last synced baseline + write journal",
а НЕ "source of truth".

### 4.1 Проблема с текущим подходом

Если локальный JSON станет "canonical state", это четвёртый source of truth.
При рассинхроне (демон рестартнул с битым state, файл потёрся) —
непредсказуемое поведение. Known state должен быть **journal/baseline**,
не авторитетный источник.

### 4.2 Новая schema

```json
{
  "tasks": {
    "VC-115": {
      "last_synced": "2026-05-06T17:00:00",
      "sections": {
        "Минимальный функционал (MVP)": {
          "notion_hash": "abc123",
          "confluence_hash": "def456",
          "last_synced_hash": "abc123",
          "last_synced_at": "2026-05-06T17:00:00"
        }
      },
      "plan_items": [
        {
          "sync_id": "uuid",
          "jira_key": "VCSUB-127",
          "notion_block_id": "...",
          "conf_task_uuid": "...",
          "title": "...",
          "notion_checked": false,
          "jira_checked": false,
          "conf_checked": false,
          "last_synced_at": "..."
        }
      ],
      "status": {
        "notion": "In progress",
        "jira": "В работе",
        "last_synced_at": "..."
      },
      "write_journal": [
        {
          "at": "2026-05-06T17:00:00",
          "target": "confluence",
          "section": "MVP",
          "action": "update",
          "hash_before": "...",
          "hash_after": "..."
        }
      ]
    }
  }
}
```

### 4.3 Write journal

- [ ] При каждой записи в Notion/Jira/Confluence — логировать в journal
- [ ] При чтении: если source hash == наш последний write hash → это наше
  собственное изменение, НЕ считать "user change"
- [ ] Это решает проблему: "демон записал в Confluence → следующий цикл видит
  `conf_changed=true` → считает что пользователь изменил"

### 4.4 Миграция

- [ ] При старте: если state в старом формате → мигрировать
- [ ] Если state отсутствует → baseline-only первый цикл (уже реализовано)

**Файлы:** `sync.py` (all classes), `config.py` (STATE_FILE schema)

---

## Фаза 5: Unified Task Snapshot + Single Read

Цель: один проход чтения, одна модель данных, одна точка принятия решений.

### 5.1 TaskSnapshot

```python
@dataclass
class TaskSnapshot:
    jira_key: str
    # Notion
    notion_page: Dict              # raw page from query
    notion_blocks: Dict[str, List] # section_name → blocks
    notion_todos: List[Dict]       # plan items
    notion_edited: str             # page last_edited_time
    # Jira
    jira_issue: Dict               # full issue
    jira_subtasks: List[Dict]      # with updated timestamps
    # Confluence
    conf_page: Optional[Dict]      # page metadata
    conf_body: Optional[str]       # full body HTML
    conf_version: Optional[int]
    conf_when: Optional[str]       # version.when
    conf_sections: Dict[str, str]  # section_name → HTML
    conf_tasks: List[Dict]         # parsed plan items
```

### 5.2 Single read pass

- [ ] Один `query_all_pages_with_jira_key()` в начале цикла
- [ ] Один batch Jira read (JQL `project = VC AND statusCategory != Done`)
- [ ] Один Confluence read per page (с кешированием в snapshot)
- [ ] Все фазы работают с одним и тем же snapshot

### 5.3 Unified sync engine

- [ ] Заменить 7 отдельных фаз на один `sync_task(snapshot, known_state) -> actions`
- [ ] Actions исполняются после resolution (не по ходу)
- [ ] Batch writes: один Confluence update per page, minimal Notion API calls

### 5.4 Оптимизация API calls

- [ ] Notion page list кешируется на весь цикл (сейчас 4+ раза)
- [ ] Confluence page body кешируется (сейчас 3 раза per page)
- [ ] Параллельные запросы через `concurrent.futures.ThreadPoolExecutor`
  для Jira + Confluence + Notion reads

**Файлы:** новый `taskautomation/engine.py` или рефакторинг `sync.py`

---

## Фаза 6: Per-Section Timestamps (улучшенный conflict resolution)

Цель: более точное определение "кто менялся" на уровне секций, не страниц.

### 6.1 Notion: block-level timestamps

- [ ] Notion API возвращает `last_edited_time` для каждого блока
- [ ] При чтении toggle content — брать max(last_edited_time) children
  → per-section timestamp
- [ ] Это точнее чем page-level timestamp

### 6.2 Jira: changelog API

- [ ] Для status/priority: `GET /rest/api/3/issue/{key}/changelog`
  → точное время изменения конкретного поля
- [ ] Для subtasks: `updated` field (уже реализовано)

### 6.3 Confluence: page-level + hash

- [ ] Confluence REST API не даёт per-section timestamps
- [ ] Workaround: hash changed + page-level `version.when`
- [ ] Conservative policy: если hash changed но timestamp старше
  другого source → warn, не sync (потенциальный false positive)

### 6.4 Conflict policy

- [ ] Разные секции изменились в разных sources → оба применяются (no conflict)
- [ ] Одна секция, одинаковое значение → применяем
- [ ] Одна секция, разные значения → per-section timestamp wins
- [ ] Если timestamps не различимы (< 60с разницы) → warn + skip
  (лучше не синкнуть чем сломать)

**Файлы:** `sync.py`, `notion_client.py`, `jira_client.py`

---

## Порядок работ (summary)

| # | Фаза | Что | Риск без этого |
|---|---|---|---|
| 0 | P0 Safety Fixes | Прекращаем портить данные | Данные ломаются каждый цикл |
| 1 | Тесты | Покрываем safety-инварианты | Регрессии при любой правке |
| 2 | Converter Idempotency | Стабильный roundtrip | Ложные diff'ы, лишние writes |
| 3 | Stable Identity | ID binding для plan items | Дубли, потеря связей при rename |
| 4 | Journal Schema | Write tracking, no self-loop | Демон реагирует на свои записи |
| 5 | Unified Engine | Single read, single model | Медленно, 132 лишних API calls |
| 6 | Per-Section Timestamps | Точный conflict resolution | Ложные конфликты |

Фазы 0-2 — **critical**, нужны до любого другого рефакторинга.
Фазы 3-4 — **important**, фундамент для надёжности.
Фазы 5-6 — **improvement**, производительность и точность.
