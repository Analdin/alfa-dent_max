# AlfaDent MAX Bot

Обработчик webhook MAX. Работает через общий FastAPI API и единую базу постоянных аккаунтов Mini App/Telegram.

- MAX-бот: `max_bot/max_bot.py` (Flask + Waitress, слушает только `127.0.0.1:5000`);
- центральный API и Mini App: [Analdin/mini-app_main](https://github.com/Analdin/mini-app_main);
- инструкция установки, systemd units и Nginx: [deploy/README_DEPLOY_RU.md](https://github.com/Analdin/mini-app_main/blob/main/deploy/README_DEPLOY_RU.md).

При первой регистрации по телефону сервер создаёт постоянный пароль. Повторный вход требует телефон и пароль. Боты получают серверный session token и перепроверяют его через `/api/auth/me`; локального флага авторизации недостаточно.

**Ограничение:** SMS/другой независимой проверки владения номером нет. Номер телефона без такого подтверждения нельзя считать доказанной личностью пациента; не открывайте через этот способ медицинские документы и другие чувствительные данные.

Секреты задаются только в приватной конфигурации VPS:
- `MAX_BOT_TOKEN`
- `BITRIX24_WEBHOOK`
- `AUTH_API_URL=http://127.0.0.1:8001`
- `INTERNAL_AUTH_SECRET` (совпадает с секретом центрального API)
- `MAX_WEBHOOK_SECRET`
- `MAX_MINIAPP_LINK=https://ВАШ_ДОМЕН/miniapp.html`
- `PROMOTIONS_URL=https://ВАШ_ДОМЕН/assets/promotions.json`
- `MAX_DATA_DIR=/var/lib/alfadent/max`

MAX webhook публикуется через Nginx по адресу `https://ВАШ_ДОМЕН/max/webhook`. Nginx удаляет префикс `/max/` и проксирует запрос на локальный обработчик `/webhook`. Внешний порт 5000 открывать не нужно.

Установка зависимостей: `pip install -r requirements.txt`. Запуск из каталога `max_bot`: `python max_bot.py`.

Приложение создаёт заявки в Bitrix24; оно не подтверждает свободный слот и не подключено к МИС «Инфоклиника».
