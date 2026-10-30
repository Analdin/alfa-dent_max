# AlfaDent MAX bot — исправленная версия

## Что сохранено

Все 50 исходных функций и пользовательские сценарии сохранены: `/start`, `/menu`, меню клиники, врачи, услуги, акции, запись-заявка, экстренное обращение, обратная связь, отзывы, рекомендации, поиск клиента в Bitrix24, смены администраторов и временный код `1234`.

Код `1234` оставлен намеренно по просьбе владельца проекта. Реальная SMS-отправка не подключена; этот код нельзя считать безопасной авторизацией для публичного production-сервиса.

## Что исправлено

1. `bot_started` читает пользователя из верхнеуровневого `user.user_id` объекта MAX Update. `message_created` и `message_callback` читают пользователя из соответствующих полей событий.
2. `open_app.web_app` больше не получает произвольный URL Render. Нативная кнопка создаётся только при наличии проверенного `MAX_BOT_USERNAME` или `MAX_BOT_CONTACT_ID`. Иначе бот отправляет обычную HTTPS link-кнопку.
3. MAX и Bitrix24 секреты больше не вшиты в Python. Они читаются из переменных окружения.
4. Webhook проверяет секрет через `X-Max-Bot-Api-Secret` и дополнительно поддерживает путь `/webhook/<secret>`, который использует текущий tunnel supervisor.
5. Входящий webhook сначала сохраняется в SQLite, и только потом MAX получает быстрый HTTP 200. Повторные одинаковые обновления отсекаются по fingerprint.
6. Ошибочные события отмечаются в очереди как `failed`, но автоматически не исполняются повторно: повтор может создать дублирующую заявку или задачу в Bitrix24. Ошибку нужно разбирать по журналу и записи очереди.
7. Ошибки отправки исходящих MAX-сообщений логируются и не заставляют повторно выполнять CRM-действия.
8. Сессии пользователей и смены администраторов хранятся в SQLite и переживают перезапуск.
9. Время смен использует `Europe/Moscow`.
10. MAX ID и Bitrix24 user ID разделены. Для назначения задач используется явная переменная `MAX_TO_BITRIX_ADMIN_MAP`; без неё бот создаёт лид Bitrix24 вместо назначения задачи не тому пользователю.
11. Команда `/shift_status`, как и остальные команды управления сменами, доступна только MAX-администраторам.
12. SQLite-соединения закрываются корректно; журналы ротируются.
13. Flask development server заменён на Waitress.

## Важная ротация секретов

Предыдущий исходник содержал MAX bot token и входящий Bitrix24 webhook URL. Считайте их скомпрометированными:

- отзовите старый MAX bot token в MAX Partner Portal и выпустите новый;
- деактивируйте старый Bitrix24 incoming webhook и создайте новый;
- не публикуйте новые значения в GitHub и не отправляйте их в чат.

Не переносите старые значения в новый файл.

## 1. Загрузка в GitHub

Замените только файл:

`max_bot/max_bot.py`

на файл `max_bot/max_bot.py` из этого архива. Остальные файлы проекта не удаляйте.

Добавьте строки из `.gitignore.additions.txt` в существующий корневой `.gitignore` (не затирайте его целиком). Реальный файл `/etc/alfadent/max_bot.env` в GitHub не загружайте.

Опционально добавьте `requirements-max.txt` в корень репозитория. В текущем серверном виртуальном окружении Flask и requests уже использовались старым ботом; для новой версии дополнительно нужен Waitress.

## 2. Конфигурация на VPS

Создайте защищённый файл:

```bash
mkdir -p /etc/alfadent
nano /etc/alfadent/max_bot.env
```

Скопируйте туда шаблон из `max_bot/max_bot.env.example` и заполните локально:

- новый `MAX_BOT_TOKEN`;
- новый `BITRIX24_WEBHOOK`;
- `MAX_WEBHOOK_SECRET` — сгенерируйте командой `openssl rand -hex 32` и вставьте результат;
- остальные значения проверьте по комментариям.

Оставьте `MAX_REQUIRE_WEBHOOK_SECRET=1`. `MAX_BOT_USERNAME` можно пока оставить пустым: тогда бот предложит обычную HTTPS-ссылку на Mini App, а не некорректную `open_app`-кнопку.

`BITRIX_ASSIGNED_BY_ID=33` сохранено как прежнее значение исходника, но перед запуском проверьте, что `33` действительно является Bitrix24 user ID ответственного. `BITRIX_OBSERVER_IDS` тоже должны содержать только Bitrix24 user ID.

`MAX_TO_BITRIX_ADMIN_MAP` можно пока оставить пустой. В этом случае задачи смены не будут назначаться по MAX ID; обращения будут передаваться как лиды Bitrix24. После проверки соответствий заполните формат `MAX_USER_ID:BITRIX_USER_ID`.

Ограничьте доступ к файлу:

```bash
chmod 600 /etc/alfadent/max_bot.env
```

## 3. Подключение EnvironmentFile к двум systemd-службам

Обе службы должны видеть одни и те же настройки: MAX-бот и tunnel supervisor. Создайте drop-in для MAX-бота:

```bash
mkdir -p /etc/systemd/system/alfadent-max.service.d
cat > /etc/systemd/system/alfadent-max.service.d/override.conf <<'UNIT'
[Service]
EnvironmentFile=/etc/alfadent/max_bot.env
UNIT
```

И для tunnel supervisor:

```bash
mkdir -p /etc/systemd/system/alfadent-max-tunnel.service.d
cat > /etc/systemd/system/alfadent-max-tunnel.service.d/override.conf <<'UNIT'
[Service]
EnvironmentFile=/etc/alfadent/max_bot.env
UNIT
```

Это важно: tunnel supervisor читает MAX token и webhook secret из environment, чтобы создать подписку MAX на текущий Cloudflare Quick Tunnel.

## 4. Обновление кода и установка Waitress

После commit в GitHub выполните на VPS:

```bash
cd /opt/alfa-dent_max/alfa-dent_max && git pull
```

Установите Waitress в существующее виртуальное окружение:

```bash
/opt/alfa-dent_max/alfa-dent_max/.venv/bin/pip install 'waitress>=3.0,<4.0'
```

Затем примените systemd drop-ins и перезапустите только MAX-службы:

```bash
systemctl daemon-reload
systemctl restart alfadent-max
systemctl restart alfadent-max-tunnel
```

Telegram-службу перезапускать не нужно.

## 5. Проверка

Проверьте службу:

```bash
systemctl status alfadent-max --no-pager -l
```

Проверьте локальный health endpoint:

```bash
curl -s http://127.0.0.1:5000/health
```

В health JSON должны быть `"ok": true`, `"max_token_configured": true`, `"webhook_secret_configured": true`. Значение `miniapp_open_app_configured` может быть `false`, пока не указан публичный username/ID бота; обычная link-кнопка при этом продолжает работать.

Посмотрите логи:

```bash
journalctl -u alfadent-max -n 80 --no-pager
journalctl -u alfadent-max-tunnel -n 80 --no-pager
```

Потом отправьте MAX-боту `/start`, проверьте меню, «Врачи», «Услуги», кнопку Mini App и прохождение тестового сценария авторизации с кодом `1234`.

## Ограничения, которые требуют данных владельца/клиники

- Для реальной авторизации пациентов нужен SMS-провайдер; `1234` пока остаётся демонстрационным кодом.
- Для назначения Bitrix24 задач по сменам нужны проверенные пары MAX user ID → Bitrix24 user ID.
- `BITRIX_ASSIGNED_BY_ID=33` нужно подтвердить в Bitrix24.
- Для нативной `open_app`-кнопки нужен реальный публичный username или bot contact ID. Если их пока нет, используется обычная HTTPS-ссылка.
- Интеграция с МИС «Инфоклиника» отсутствует: бот передаёт заявку, но не бронирует подтверждённый слот.

## Проверки, выполненные при подготовке

- Python bytecode compilation (`py_compile`) успешно.
- Smoke tests с тестовой заглушкой Flask: структура `bot_started`, `message_created`, `message_callback`; 19 врачей; OTP `1234`; fallback-кнопка Mini App; дедупликация webhook; сохранение смен; проверка webhook-secret header/path; права на команду `/shift_status`.
- Реальные запросы к MAX API и Bitrix24 из этой среды не выполнялись. Их нужно подтвердить после установки на VPS.
