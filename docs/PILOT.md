# Недельный пилот QMemo News Radar

Цель пилота — за 7 дней понять, приносит ли Radar пригодные маркетинговые поводы для QMemo, и настроить источники и пороги. Публикация в Quote Memorial и X в пилоте выключена: одобренные материалы копятся в outbox.

## 1. День 0: подготовка (≈1 час)

- [ ] Создан отдельный бот в @BotFather, токен в `RADAR_TELEGRAM_BOT_TOKEN`.
- [ ] Свой Telegram ID в `RADAR_ALLOWED_TELEGRAM_ID`.
- [ ] В console.x.com создано приложение, пополнены кредиты, Bearer Token в `RADAR_X_BEARER_TOKEN`.
- [ ] LLM: `RADAR_LLM_BASE_URL`, `RADAR_LLM_API_KEY`, `RADAR_LLM_MODEL`.
- [ ] `sources.yaml`: 5-15 аккаунтов и 2-5 узких запросов, `max_pages_per_query: 1`.
- [ ] `RADAR_DIGEST_TIMES` совпадает с удобными вам часами.
- [ ] Обе переменные публикации равны `false`.

```bash
cp .env.example .env
cp sources.example.yaml sources.yaml
docker compose run --rm radar qmemo-radar check-config
docker compose run --rm radar qmemo-radar dry-run --db /app/data/dry-run.db
docker compose up -d --build
docker compose ps
```

Приёмка запуска:

1. `docker compose ps` показывает `healthy` через 2-3 минуты.
2. В Telegram `/status`: «Работает: да», «Пауза: выключена», «Публикация в QMemo: выключена · в X: выключена».
3. `/run` отвечает «Сбор завершён: SUCCESS» или понятным `PARTIAL`.
4. В `/status` строка «X: все источники в порядке».
5. Пройден один полный цикл: карточка → «Использовать» → черновик → «Принято» → `/saved` показывает пакет в outbox.

Если нет подходящих свежих публикаций, пришлите боту ссылку `https://x.com/<handle>/status/<id>` и выполните `/run`: ручная ссылка придёт карточкой независимо от возраста.

## 2. Стоимость X

X API оплачивается за каждую полученную публикацию. Худший случай за сутки:

```text
источников × max_pages_per_query × 100 × (24 × 60 / RADAR_COLLECT_INTERVAL_MINUTES)
```

Благодаря `since_id` обычно приходят только новые публикации за 30 минут, поэтому реальный объём намного меньше. Широкий запрос вроде `crypto` может приносить сотни публикаций за каждый сбор — используйте конкретные слова, `from:`, `lang:` и `-is:retweet`. Проверяйте расход в console.x.com в конце дня 1 и дня 2.

## 3. Ежедневная работа (10-15 минут)

1. Утром `/status`: сбор работает, источники без ошибок, LLM в порядке.
2. В каждой подборке по каждой карточке примите решение:
   - **Использовать** — повод подходит, нужен черновик;
   - **Пропустить** — не подходит;
   - **Позже** — вернуть в следующую подборку;
   - **Почему такой балл?** — если оценка кажется странной.
3. В черновике проверьте цитату по ссылке на источник. При необходимости одна переделка: «Переделать короче», «Другой угол» или своё сообщение с инструкцией.
4. Если есть «⚠️ требуется проверка», проверьте факты и нажмите «Проверено вручную», затем «Принято».
5. Вечером `/today`: сколько карточек пришло и что осталось в очереди.

Ведите короткую заметку: какие карточки были лишними и каких поводов не хватило.

## 4. Настройка по ходу пилота

| Наблюдение | Что изменить |
| --- | --- |
| Много мусора от одного аккаунта | `enabled: false` или `blocked_authors` |
| Повторяются нерелевантные темы | `blocked_terms` или сузить запрос |
| Мало карточек, хорошие поводы в архиве | снизить `RADAR_DIGEST_THRESHOLD` до 60 |
| Слишком много карточек | повысить `RADAR_DIGEST_THRESHOLD` до 70 |
| Срочные приходят слишком часто | повысить `RADAR_URGENT_THRESHOLD` до 85 |
| Подборки в неудобное время | `RADAR_DIGEST_TIMES` |
| `/status`: ошибка источника `client_error_400` | исправить синтаксис запроса |
| `/status`: `client_error_401` или `client_error_403` | проверить Bearer Token и кредиты X |
| `/status`: `rate_limited_429` | реже сбор или меньше источников |
| `/status`: «ошибка оценки» | ключ, модель и лимиты LLM-провайдера |

Изменения `sources.yaml` и `.env` применяются перезапуском:

```bash
docker compose restart radar
```

Меняйте не больше одного параметра в день, чтобы видеть эффект.

## 5. Диагностика

```bash
docker compose logs --since 1h radar                          # журналы за час
docker compose logs radar | grep '"level": "ERROR"'           # только ошибки
docker compose exec radar qmemo-radar status                  # события по статусам и outbox
docker compose exec radar qmemo-radar healthcheck             # жив ли планировщик
```

| Симптом | Причина и действие |
| --- | --- |
| Контейнер перезапускается, в журнале `configuration problem` | не задана обязательная переменная или нет `sources.yaml`; исправьте и `docker compose up -d` |
| `sources.yaml is invalid` | ошибка YAML или неизвестное поле; `check-config` покажет подробности |
| Бот молчит | неверный токен или бот Radar запущен в двух местах; проверьте журнал `radar stopped after a failure` |
| Бот отвечает «Нет доступа» | ID в `RADAR_ALLOWED_TELEGRAM_ID` не совпадает с ID из ответа |
| `unhealthy` | планировщик не пишет heartbeat; смотрите последние ошибки в журнале |
| Нет карточек | пауза, дневной лимит исчерпан (`/today`), или все события ниже порога |

Запускайте только один экземпляр Radar на один том с базой.

## 6. Итоги недели

Резервная копия базы:

```bash
docker compose exec radar python -c "import sqlite3; sqlite3.connect('/app/data/radar.db').backup(sqlite3.connect('/app/data/pilot-backup.db'))"
docker compose cp radar:/app/data/pilot-backup.db ./pilot-backup.db
```

Метрики (выполнять на копии, например `python -m sqlite3 pilot-backup.db`):

```sql
-- события по итоговым статусам
SELECT status, COUNT(*) FROM radar_events GROUP BY status;

-- решения пользователя
SELECT action, COUNT(*) FROM feedback GROUP BY action;

-- доля «Использовать» среди показанных карточек
SELECT ROUND(100.0 * (SELECT COUNT(DISTINCT event_id) FROM feedback WHERE action = 'USE')
             / COUNT(DISTINCT event_id), 1) AS use_rate_percent
FROM telegram_deliveries;

-- распределение баллов показанных карточек
SELECT s.total / 10 * 10 AS bucket, COUNT(*)
FROM telegram_deliveries d JOIN event_scores s ON s.event_id = d.event_id
GROUP BY bucket ORDER BY bucket;

-- качество источников: сколько карточек и одобрений дал каждый
SELECT e.source_key, COUNT(DISTINCT d.event_id) AS cards,
       COUNT(DISTINCT o.event_id) AS approved
FROM radar_events e
LEFT JOIN telegram_deliveries d ON d.event_id = e.id
LEFT JOIN publication_outbox o ON o.event_id = e.id
GROUP BY e.source_key ORDER BY approved DESC, cards DESC;

-- здоровье запусков
SELECT status, COUNT(*) FROM pipeline_runs GROUP BY status;

-- одобренные пакеты
SELECT json_extract(payload_json, '$.quote_author'), json_extract(payload_json, '$.quote_text')
FROM publication_outbox;
```

Решение по итогам:

| Результат | Вывод |
| --- | --- |
| Больше 30% карточек получили «Использовать», есть 5+ одобренных пакетов | продолжать и готовить этап B (публикация через outbox) |
| 10-30% | сузить источники и пороги, повторить неделю |
| Меньше 10% или нет одобренных пакетов | пересмотреть источники и критерии оценки |

## 7. Остановка

```bash
docker compose stop radar      # остановить, данные сохраняются в томе radar_data
docker compose up -d           # продолжить с того же состояния
```

`docker compose down -v` удаляет том с базой — выполнять только после резервной копии.
