# AlfaDent MAX Bot

MAX webhook bot. Использует тот же API и общую базу постоянных аккаунтов, что Mini App и Telegram.

Первичная регистрация по телефону создаёт постоянный пароль; повторный вход требует телефон и пароль. API-адрес задаётся `AUTH_API_URL`, секрет серверных вызовов — `INTERNAL_AUTH_SECRET`. Ссылки Mini App и акций задаются через `MAX_MINIAPP_LINK` и `PROMOTIONS_URL`.

**Важно:** без SMS/другой независимой проверки владение номером не подтверждается. Не используйте это как надёжную идентификацию пациента и не открывайте чувствительные медицинские данные до подключения подтверждения номера.

Установка зависимостей: `pip install -r requirements.txt`. Запуск: `python max_bot.py`. Для production используйте systemd/Nginx из общего пакета. MAX webhook должен быть настроен на `https://YOUR_DOMAIN/max/webhook`, а секрет webhook — совпадать с `MAX_WEBHOOK_SECRET`.
