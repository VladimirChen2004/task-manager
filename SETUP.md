# Настройка интеграции Notion <-> Jira <-> Confluence

## 1. Локальная настройка (.env)

Скопировать и заполнить:
```bash
cp .env.example .env
```

Заполнить:
- `JIRA_VCHEN_URL` — `https://vchen.atlassian.net`
- `JIRA_VCHEN_EMAIL` — email аккаунта Atlassian
- `JIRA_VCHEN_API_TOKEN` — [создать тут](https://id.atlassian.com/manage-profile/security/api-tokens)
- `NOTION_API_TOKEN` — см. шаг 2
- `NOTION_DATABASE_ID` — `3050c57fd84181a7bb22ee1b23b37c6e` (уже заполнено)

## 2. Notion Internal Integration

1. Перейти на https://www.notion.so/my-integrations
2. "New integration" -> Название: **Jira Sync** -> Submit
3. Скопировать **Internal Integration Secret** -> вставить в `.env` как `NOTION_API_TOKEN`
4. Permissions: Read content, Update content, Insert content
5. Открыть базу "Tasks 2026" в Notion -> "..." -> "Connections" -> добавить **Jira Sync**

## 3. Notion Database — новые свойства

В базе "Tasks 2026" добавить 2 свойства:
- **Jira Key** — тип: Text
- **Priority** — тип: Select, опции:
  - Наивысшая срочность
  - Срочно
  - Средняя срочность
  - Не срочно
  - Бессрочно

## 4. Jira Automation -> Confluence

Настройка автоматического создания Confluence-страницы при создании задачи в Jira.

### Шаги:

1. Открыть https://vchen.atlassian.net/jira/software/projects/VCHEN/settings
2. В левом меню: **Automation** -> **Create rule**
3. **Trigger:** "Issue created"
4. (Опционально) **Condition:** Issue type = Task
5. **Action:** "Create Confluence page"
   - **Space:** выбрать нужное пространство Confluence
   - **Parent page:** выбрать корневую страницу (или оставить пустым)
   - **Title:** `{{issue.key}} — {{issue.summary}}`
   - **Body:**

```html
<h2>Описание задачи</h2>
<p>{{issue.description}}</p>

<h2>Функционал</h2>
<p><em>Описание функционала, который затрагивает задача</em></p>

<h2>Требования к исполнению</h2>
<ul>
  <li><em>Добавить требования...</em></li>
</ul>

<h2>Важность</h2>
<p><strong>Приоритет:</strong> {{issue.priority.name}}</p>
<p><strong>Срок:</strong> {{issue.duedate}}</p>

<h2>Статус работы</h2>
<table>
  <tr><th>Блок</th><th>Статус</th><th>Комментарий</th></tr>
  <tr><td><em>Добавить блоки...</em></td><td>Не начат</td><td></td></tr>
</table>

<h2>Результат</h2>
<p><em>Описание выполненной работы (заполняется по ходу)</em></p>
```

6. **Turn on** правило

### Проверка:
Создать тестовую задачу в Jira -> через минуту проверить Confluence.

## 5. Проверка Jira-скрипта

```bash
# Проверить доступные статусы в VCHEN
/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3 jira_vchen.py discover-statuses

# Проверить типы задач
/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3 jira_vchen.py discover-issue-types

# Тестовое создание
/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3 jira_vchen.py create \
  --title "Test integration" --priority Medium
```

**Важно:** после `discover-statuses` проверить что маппинг в `jira_vchen.py` (`JIRA_TO_NOTION_STATUS`) соответствует реальным статусам VCHEN. Если статусы отличаются — скорректировать словарь.

## 6. Настройка sync на сервере (cron)

```bash
# На сервере: добавить env vars
# Скопировать jira_vchen.py и jira_notion_sync.py
# Установить зависимости: pip install jira requests python-dotenv

# Добавить в crontab:
crontab -e

# Каждые 10 минут:
*/10 * * * * cd /path/to/task-automation && /path/to/python3 jira_notion_sync.py >> /tmp/jira_notion_sync.log 2>&1
```

### Проверка sync:
```bash
# Локально, dry-run:
/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3 jira_notion_sync.py --dry-run --full

# Реальный sync:
/Users/nfware/Documents/my_prjcts/task-automation/venv/bin/python3 jira_notion_sync.py --full
```
