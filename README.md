# AlfaDent MAX Bot

Обработчик webhook MAX. Работает через общий FastAPI API и единую базу постоянных аккаунтов Mini App/Telegram.

- MAX-бот: `max_bot/max_bot.py` (Flask + Waitress, слушает только `127.0.0.1:5000`);
- центральный API и Mini App: [Analdin/mini-app_main](https://github.com/Analdin/mini-app_main);
- приватные настройки: [Analdin/alfadent-miniapp](https://github.com/Analdin/alfadent-miniapp), файл `alfadent_config.py`;
- инструкция установки и Nginx: [deploy/README_DEPLOY_RU.md](https://github.com/Analdin/mini-app_main/blob/main/deploy/README_DEPLOY_RU.md).

**.env-файлы не используются.** MAX-бот, FastAPI и Telegram-бот импортируют один приватный Python-модуль `alfadent_config.py`. На VPS он размещается в `/etc/alfadent/alfadent_config.py`, подключается символической ссылкой в рабочий каталог и не должен коммититься в этот репозиторий.

При первой регистрации сервер создаёт постоянный пароль. Повторный вход требует телефон и пароль. Боты получают серверный session token и перепроверяют его через `/api/auth/me`.

**Ограничение:** SMS/другой независимой проверки владения номером нет. Номер телефона сам по себе не доказывает личность пациента.

MAX webhook публикуется через Nginx по адресу `https://ВАШ_ДОМЕН/max/webhook`. Внешний порт 5000 открывать не нужно. В сохранённых исходниках секрет MAX webhook не был задан и обязательная проверка была отключена; текущая конфигурация сохраняет это поведение, пока владелец не подтвердит значение секрета из кабинета MAX.

Приложение создаёт заявки в Bitrix24; оно не подтверждает свободный слот и не подключено к МИС «Инфоклиника».
