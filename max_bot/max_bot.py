#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAX webhook bot for AlfaDent, integrated with the existing Mini App and Bitrix24 CRM.

Secrets are read from environment variables or a private env file. Without MIS API access,
the bot creates requests for administrator confirmation and never claims a visit is booked.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent


# Настройки восстановлены из старого рабочего исходника max_bot_alfa_dent_updated.py.
# .env не используется: основные ключи и параметры находятся непосредственно в этом файле.
MAX_API_BASE = "https://platform-api2.max.ru"
MAX_API = MAX_API_BASE
TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_BOT_TOKEN = TOKEN
BITRIX24_WEBHOOK = os.getenv("BITRIX24_WEBHOOK", "").strip()
BITRIX_ASSIGNED_BY_ID = 33
BITRIX_OBSERVER_IDS = []
MAX_ADMIN_IDS = [15, 33, 35, 37, 39, 727, 7821]
ADMIN_IDS = MAX_ADMIN_IDS
# В старом исходнике соответствие MAX ID -> Bitrix ID не задавалось.
MAX_TO_BITRIX_ADMIN_MAP = {}

AUTH_API_URL = os.getenv("AUTH_API_URL", "http://127.0.0.1:8001").rstrip("/")
AUTH_API_SECRET = os.getenv("INTERNAL_AUTH_SECRET", "").strip()
MAX_MINIAPP_LINK = os.getenv("MAX_MINIAPP_LINK", "https://YOUR_DOMAIN/miniapp.html").strip()
MAX_BOT_USERNAME = ""
MAX_BOT_CONTACT_ID = None
# В старом исходнике секрет webhook не был задан; обязательная проверка отключена.
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
MAX_REQUIRE_WEBHOOK_SECRET = os.getenv("MAX_REQUIRE_WEBHOOK_SECRET", "true").lower() in ("1", "true", "yes")

CLINIC_PHONE = "+7 (988) 233-77-25"
CLINIC_ADDRESS = "г. Сочи, ул. Северная, 12, корп. 2, 2 этаж. Ориентир: с торца здания, рядом с магазином «Коляски»."
CLINIC_HOURS = "Ежедневно, 09:00–20:00"
YANDEX_MAPS_URL = "https://yandex.ru/medicine/clinic/alfa_dent_1617563656"
PROMOTIONS_URL = os.getenv("PROMOTIONS_URL", "https://YOUR_DOMAIN/assets/promotions.json").strip()
PROMOTIONS_FILE = BASE_DIR / "web" / "assets" / "promotions.json"
CLINIC_TIMEZONE_NAME = "Europe/Moscow"
try:
    CLINIC_TZ = ZoneInfo(CLINIC_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    CLINIC_TZ = ZoneInfo("Europe/Moscow")
    CLINIC_TIMEZONE_NAME = "Europe/Moscow"

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
if os.name == "posix":
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass
PENDING_FILE = DATA_DIR / "pending_requests.json"  # legacy-compatible emergency queue
DATABASE_FILE = DATA_DIR / "max_bot.sqlite3"

LOG_LEVEL = "INFO"
_file_handler = RotatingFileHandler(DATA_DIR / "max_bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), _file_handler],
)
logger = logging.getLogger("alfadent_max_bot")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

DB_LOCK = threading.RLock()
SESSION_LOCK = threading.RLock()
PENDING_LOCK = threading.RLock()
WEBHOOK_QUEUE_WAKE = threading.Event()
WEBHOOK_WORKER_STOP = threading.Event()
WEBHOOK_WORKER_THREAD: Optional[threading.Thread] = None

user_sessions: dict[int, dict[str, Any]] = {}
active_shifts: dict[int, dict[str, datetime]] = {}
_promotions_cache: list[dict[str, Any]] = []
_promotions_cache_at = 0.0


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection and always commit/close it."""
    conn = sqlite3.connect(DATABASE_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database() -> None:
    with DB_LOCK, db_connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_sessions (
            user_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS active_shifts (
            user_id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ends_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS webhook_events (
            event_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_error TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_webhook_events_ready ON webhook_events(status, next_attempt, created_at)")
        conn.execute(
            "UPDATE webhook_events SET status='failed', updated_at=?, "
            "last_error='Previous process stopped while processing; inspect before manual replay' "
            "WHERE status='processing'",
            (time.time(),),
        )
        conn.execute("DELETE FROM webhook_events WHERE status IN ('done','failed') AND created_at < ?", (time.time() - 30 * 86400,))
    if os.name == "posix":
        try:
            DATABASE_FILE.chmod(0o600)
        except OSError:
            pass


init_database()

SERVICES = [
    "Консультация", "ПРОФ гигиена", "Лечение кариеса", "Удаление зуба", "Имплантация",
    "Протезирование", "Ортодонтия (брекеты, элайнеры)", "Гнатология / ВНЧС", "Диагностика (КЛКТ)",
]
SERVICE_CATEGORIES = [
    "Консультация стоматолога", "КЛКТ-диагностика", "Лечение зубов",
    "Профессиональная гигиена", "Хирургия", "Имплантация", "Протезирование",
    "Виниры", "Гнатология", "Ортодонтия", "Детская стоматология",
    "Лечение во сне и седации",
]
PROBLEMS = ["Болит зуб", "Отёк, воспаление", "Скол зуба", "Выпала коронка", "Ребёнку нужна помощь", "Не знаю, что нужно"]
EMERGENCY_PROBLEMS = ["Острая боль", "Отёк", "Скол зуба", "Выпала коронка", "Ребёнку больно", "Нужен срочный звонок"]
# Names are taken from the source supplied by the clinic. Specialities and service mappings are not inferred.
DOCTORS = [
    'Петросян Гаянэ Борисовна',
    'Казарян Инна Владимировна',
    'Дячук Кирилл Алексеевич',
    'Ягафаров Наиль Фанилевич',
    'Спивак Екатерина Алексеевна',
    'Минасян Кристина Андреевна',
    'Зиновьева Диана Наримановна',
    'Бычкова Елена Викторовна',
    'Гитинов Шамиль Магомедович',
    'Мамеян Джульетта Анзоровна',
    'Роскошная Валерия Витальевна',
    'Героль Дарья Олеговна',
    'Захарова Екатерина Алексеевна',
    'Петросян Грант Эрикович',
    'Гурьянова Анна Владимировна',
    'Вылегжанина Екатерина Александровна',
    'Калайджян Анна Робертовна',
]
DOCTORS_PER_PAGE = 5


def session_for(user_id: int, first_name: str = "Пациент") -> dict[str, Any]:
    """Get a cached session; load it from SQLite after a process restart."""
    user_id = int(user_id)
    with SESSION_LOCK:
        if user_id not in user_sessions:
            payload: dict[str, Any] = {}
            try:
                with db_connect() as conn:
                    row = conn.execute("SELECT payload FROM user_sessions WHERE user_id=?", (user_id,)).fetchone()
                if row:
                    loaded = json.loads(row["payload"])
                    if isinstance(loaded, dict):
                        payload = loaded
            except (sqlite3.Error, json.JSONDecodeError):
                logger.exception("Не удалось загрузить сессию пользователя MAX user_id=%s", user_id)
            if not payload:
                payload = {
                    "name": first_name or "Пациент", "auth": False, "auth_until": 0,
                    "phone": "", "step": None, "pending_action": None, "appointment": {},
                }
            user_sessions[user_id] = payload
        elif first_name and user_sessions[user_id].get("name") in (None, "Пациент"):
            user_sessions[user_id]["name"] = first_name
        return user_sessions[user_id]


def persist_session(user_id: int) -> None:
    with SESSION_LOCK:
        payload = user_sessions.get(int(user_id))
        if payload is None:
            return
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO user_sessions(user_id,payload,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (int(user_id), encoded, time.time()),
        )


def load_active_shifts() -> None:
    now = datetime.now(CLINIC_TZ)
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute("SELECT user_id, started_at, ends_at FROM active_shifts").fetchall()
        for row in rows:
            try:
                started = datetime.fromisoformat(row["started_at"])
                ends = datetime.fromisoformat(row["ends_at"])
                if ends.tzinfo is None:
                    started = started.replace(tzinfo=CLINIC_TZ)
                    ends = ends.replace(tzinfo=CLINIC_TZ)
                if ends > now:
                    active_shifts[int(row["user_id"])] = {"start_time": started, "end_time": ends}
                else:
                    conn.execute("DELETE FROM active_shifts WHERE user_id=?", (int(row["user_id"]),))
            except (ValueError, TypeError):
                logger.warning("Удалена некорректная запись смены MAX user_id=%s", row["user_id"])
                conn.execute("DELETE FROM active_shifts WHERE user_id=?", (int(row["user_id"]),))


load_active_shifts()


def normalize_phone(value: str) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def load_promotions(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Read the currently published promotions from the existing Mini App first."""
    global _promotions_cache, _promotions_cache_at
    now = time.monotonic()
    if _promotions_cache_at and not force_refresh and now - _promotions_cache_at < 300:
        return list(_promotions_cache)
    data: Any = None
    if PROMOTIONS_URL.startswith("https://"):
        try:
            response = requests.get(PROMOTIONS_URL, timeout=5)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("Не удалось загрузить акции с Mini App, используется локальная копия: %s", exc)
    if not isinstance(data, list):
        try:
            data = json.loads(PROMOTIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Не удалось загрузить локальную копию promotions.json")
            data = []
    _promotions_cache = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    _promotions_cache_at = now
    return list(_promotions_cache)


def promotion_by_id(promo_id: str | int) -> Optional[dict[str, Any]]:
    return next((p for p in load_promotions() if str(p.get("id")) == str(promo_id)), None)


def save_pending_request(payload: dict[str, Any]) -> None:
    """Write failed CRM submissions to a durable local queue (atomic replace)."""
    with PENDING_LOCK:
        try:
            existing: list[Any] = []
            if PENDING_FILE.exists():
                raw = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    existing = raw
            existing.append({**payload, "saved_at": datetime.now(CLINIC_TZ).isoformat(timespec='seconds')})
            temp_path = PENDING_FILE.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, PENDING_FILE)
            if os.name == "posix":
                PENDING_FILE.chmod(0o600)
        except Exception:
            logger.exception("Не удалось сохранить резервное обращение")


def bitrix_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if not BITRIX24_WEBHOOK or BITRIX24_WEBHOOK.startswith("PASTE_NEW_"):
        return {"error": "BITRIX24_WEBHOOK не настроен"}
    try:
        resp = requests.post(f"{BITRIX24_WEBHOOK}/{method}", json=params, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("error"):
            logger.error("Bitrix24 %s: %s", method, result.get("error_description", result.get("error")))
        return result
    except Exception as exc:
        logger.exception("Ошибка Bitrix24: %s", method)
        return {"error": str(exc)}


def lead_id_from(result: dict[str, Any]) -> Optional[str]:
    value = result.get("result", {}).get("item", {}).get("id")
    return str(value) if value else None


def create_lead(title: str, user_id: int, description: str, phone: str = "") -> dict[str, Any]:
    user = session_for(user_id)
    fields: dict[str, Any] = {
        "title": title[:255],
        "name": (user.get("name") or "Пациент")[:100],
        "comments": description,
        "sourceId": "WEB",
        "assignedById": BITRIX_ASSIGNED_BY_ID,
        "OPENED": "Y",
    }
    if BITRIX_OBSERVER_IDS:
        fields["observers"] = BITRIX_OBSERVER_IDS
    result = bitrix_call("crm.item.add", {"entityTypeId": 1, "fields": fields})
    new_id = lead_id_from(result)
    if new_id and phone:
        bitrix_call("crm.item.update", {"entityTypeId": 1, "id": new_id, "fields": {
            "fm": [{"typeId": "PHONE", "valueType": "WORK", "value": phone}]
        }})
    if not new_id:
        save_pending_request({"type": title, "user_id": user_id, "name": user.get("name"), "phone": phone,
                              "description": description, "bitrix_error": result.get("error", "unknown")})
    return result


def create_task_for_active_admins(title: str, description: str, priority: int = 2) -> dict[str, Any]:
    """Assign tasks only through an explicit MAX-user-ID -> Bitrix-user-ID map."""
    active_admins = get_active_shift_users()
    if not active_admins:
        logger.warning("Нет активных администраторов на смене")
        return {"error": "Нет администраторов на смене"}
    if not MAX_TO_BITRIX_ADMIN_MAP:
        logger.warning("MAX_TO_BITRIX_ADMIN_MAP не настроена; задача не будет назначена по MAX ID")
        return {"error": "Не настроено соответствие MAX ID и Bitrix ID"}

    results = []
    for max_admin_id in active_admins:
        bitrix_admin_id = MAX_TO_BITRIX_ADMIN_MAP.get(max_admin_id)
        if not bitrix_admin_id:
            logger.warning("Для MAX admin_id=%s не задан Bitrix ID", max_admin_id)
            continue
        result = bitrix_call("tasks.task.add", {"fields": {
            "TITLE": title[:255], "DESCRIPTION": description,
            "RESPONSIBLE_ID": bitrix_admin_id, "PRIORITY": priority,
        }})
        results.append(result)
        if result.get("result", {}).get("task", {}).get("id"):
            return result
    return results[0] if results else {"error": "Нет активных администраторов с настроенным Bitrix ID"}


def create_support_request(user_id: int, subject: str, description: str, phone: str = "", priority: int = 2) -> dict[str, Any]:
    user = session_for(user_id)
    full = (f"Пациент: {user.get('name') or 'не указано'}\nТелефон: {phone or user.get('phone') or 'не указан'}\n"
            f"MAX user_id: {user_id}\nКанал: MAX\nВремя обращения: {datetime.now(CLINIC_TZ).isoformat(timespec='seconds')}\n\n{description}")
    task = create_task_for_active_admins(f"[MAX] {subject}", full, priority)
    if task.get("result", {}).get("task", {}).get("id"):
        return task
    return create_lead(f"[MAX] {subject}", user_id, full, phone or user.get("phone", ""))


def create_appointment_lead(user_id: int) -> dict[str, Any]:
    user = session_for(user_id)
    a = user.get("appointment", {})
    phone = user.get("phone", "")
    description = (
        f"Пациент: {user.get('name') or 'Пациент'}\nТелефон: {phone or 'не указан'}\nMAX user_id: {user_id}\nКанал: MAX\n"
        f"Проблема: {a.get('problem') or 'не указана'}\nУслуга: {a.get('service') or 'не указана'}\n"
        f"Врач (пожелание): {a.get('doctor') or 'подберёт администратор'}\nАкция: {a.get('promotion') or 'не выбрана'}\n"
        f"Предпочтительная дата: {a.get('date') or 'не указана'}\nПредпочтительное время: {a.get('time') or 'не указано'}\n"
        f"Клиника: {CLINIC_ADDRESS}\n\nВАЖНО: это заявка, а не запись. Слот не забронирован; администратор должен подтвердить возможность приёма.")
    title = f"[MAX] Заявка по акции: {a.get('promotion')}" if a.get("promotion") else f"[MAX] Заявка на приём: {a.get('service') or 'консультация'}"
    return create_lead(title, user_id, description, phone)


def start_shift(user_id: int) -> datetime:
    now = datetime.now(CLINIC_TZ)
    end = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if now >= end:
        end += timedelta(days=1)
    record = {"start_time": now, "end_time": end}
    active_shifts[int(user_id)] = record
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO active_shifts(user_id,started_at,ends_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET started_at=excluded.started_at, ends_at=excluded.ends_at",
            (int(user_id), now.isoformat(), end.isoformat()),
        )
    return end


def end_shift(user_id: int) -> bool:
    existed = active_shifts.pop(int(user_id), None) is not None
    with DB_LOCK, db_connect() as conn:
        conn.execute("DELETE FROM active_shifts WHERE user_id=?", (int(user_id),))
    return existed


def get_active_shift_users() -> list[int]:
    now = datetime.now(CLINIC_TZ)
    expired = [uid for uid, shift in active_shifts.items() if now >= shift["end_time"]]
    if expired:
        for uid in expired:
            active_shifts.pop(uid, None)
        with DB_LOCK, db_connect() as conn:
            conn.executemany("DELETE FROM active_shifts WHERE user_id=?", [(uid,) for uid in expired])
    return list(active_shifts)


def callback(text: str, payload: str) -> dict[str, Any]:
    return {"type": "callback", "text": text[:64], "payload": payload[:128]}


def link_button(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": text[:64], "url": url}


def app_button(text: str = "📱 Открыть приложение клиники") -> Optional[dict[str, Any]]:
    """Create an open_app button only when MAX bot identity is configured.

    MAX open_app.web_app accepts the target bot's public username/link, not a
    Render page URL. If the bot identity is unknown, miniapp_rows() uses a normal
    HTTPS link button instead, which avoids MAX API 404 Link-not-found responses.
    """
    if MAX_BOT_CONTACT_ID:
        return {"type": "open_app", "text": text[:128], "contact_id": MAX_BOT_CONTACT_ID}
    bot_reference = MAX_BOT_USERNAME.strip()
    if bot_reference:
        if bot_reference.startswith("@"):
            bot_reference = bot_reference[1:]
        return {"type": "open_app", "text": text[:128], "web_app": bot_reference}
    return None


def keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"attachments": [{"type": "inline_keyboard", "payload": {"buttons": rows}}]} if rows else {}


class MaxAPIError(RuntimeError):
    pass


def send_message(user_id: int, text: str, rows: Optional[list[list[dict[str, Any]]]] = None) -> dict[str, Any]:
    """Send a MAX message. Delivery failures are logged, not raised into webhook replay."""
    if not MAX_BOT_TOKEN or not MAX_BOT_TOKEN.strip() or MAX_BOT_TOKEN.startswith("PASTE_NEW_"):
        logger.error("MAX_BOT_TOKEN не настроен; сообщение user_id=%s не отправлено", user_id)
        return {"error": "MAX_BOT_TOKEN не настроен"}

    payload: dict[str, Any] = {"text": str(text)[:4000]}
    if rows:
        payload.update(keyboard(rows))

    try:
        response = requests.post(
            f"{MAX_API_BASE}/messages",
            params={"user_id": int(user_id)},
            headers={"Authorization": MAX_BOT_TOKEN.strip(), "Content-Type": "application/json"},
            json=payload,
            timeout=(4, 10),
        )
        if not response.ok:
            body = response.text[:800]
            logger.error("MAX API вернул HTTP %s при отправке сообщения: %s", response.status_code, body)
            return {"error": f"MAX API HTTP {response.status_code}", "details": body}
        if not response.content:
            return {"ok": True}
        try:
            result = response.json()
            if isinstance(result, dict) and result.get("error"):
                logger.error("MAX API вернул ошибку в теле ответа: %s", result.get("error"))
                return result
            return result if isinstance(result, dict) else {"ok": True}
        except ValueError:
            logger.warning("MAX API вернул успешный HTTP-ответ не в JSON формате")
            return {"ok": True}
    except requests.RequestException as exc:
        logger.exception("Ошибка сети при отправке MAX-сообщения user_id=%s", user_id)
        return {"error": f"Ошибка сети: {type(exc).__name__}"}


def max_bot_profile_url() -> str:
    """Return a shareable MAX bot URL, or an empty string if username is unknown."""
    reference = MAX_BOT_USERNAME.strip()
    if reference.startswith("@"):
        reference = reference[1:]
    if reference.startswith("https://"):
        return reference.rstrip("/")
    if reference:
        return f"https://max.ru/{reference}"
    return ""


def miniapp_rows(text: str = "📱 Открыть приложение клиники") -> list[list[dict[str, Any]]]:
    button = app_button(text)
    if button:
        return [[button]]
    if MAX_MINIAPP_LINK.startswith("https://"):
        return [[link_button(text[:64], MAX_MINIAPP_LINK)]]
    return []


def send_main_menu(user_id: int) -> None:
    user = session_for(user_id)
    name = user.get("name") or ""
    greeting = f"Здравствуйте, {name}! 👋" if name and name != "Пациент" else "Здравствуйте! 👋"
    rows = miniapp_rows() + [
        [callback("🦷 Записаться", "appointment"), callback("📅 Мои визиты", "my_visits")],
        [callback("🚨 Экстренная помощь", "emergency"), callback("👩‍⚕️ Врачи", "doctors_0")],
        [callback("📋 Услуги и цены", "services"), callback("🎁 Акции", "promo")],
        [callback("🏥 О клинике", "about"), callback("💬 Написать администратору", "support")],
        [callback("⭐ Отзывы", "reviews"), callback("💛 Рекомендовать клинику", "referral")],
    ]
    send_message(user_id, f"{greeting}\n\nЯ чат-бот стоматологической клиники «Альфа-Дент».\n\n📍 {CLINIC_ADDRESS}\n🕘 {CLINIC_HOURS}\n📞 {CLINIC_PHONE}\n\nПодробные карточки услуг, врачей и акции доступны в приложении клиники.", rows)


def validate_auth_session(user_id: int, user: dict[str, Any]) -> bool:
    token = str(user.get("access_token") or "")
    if not token:
        user["auth"] = False
        return False
    try:
        response = requests.get(
            f"{AUTH_API_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        data = response.json()
        valid = response.ok and bool(data.get("authorized"))
    except (requests.RequestException, ValueError):
        logger.exception("Не удалось проверить серверную сессию MAX")
        return False
    if not valid:
        user["auth"] = False
        user.pop("access_token", None)
        persist_session(user_id)
    return valid


def require_auth(user_id: int, action: str) -> bool:
    user = session_for(user_id)
    if user.get("auth") and validate_auth_session(user_id, user):
        return True
    user["auth"] = False
    user["pending_action"] = action
    user["step"] = "waiting_phone"
    send_message(user_id, "🔐 Для доступа введите номер телефона. При первой регистрации будет создан постоянный пароль; при следующих входах понадобится этот пароль.", [[callback("⬅️ Главное меню", "back_to_menu")]])
    return False


def dispatch_after_auth(user_id: int) -> None:
    user = session_for(user_id)
    action = user.pop("pending_action", None)
    if action == "booking":
        start_booking(user_id)
    elif action == "visits":
        show_my_visits(user_id)
    elif action and action.startswith("promo:"):
        start_promotion_booking(user_id, action.split(":", 1)[1])
    elif action and action.startswith("doctor:"):
        try:
            index = int(action.split(":", 1)[1])
            doctor = DOCTORS[index]
        except (ValueError, IndexError):
            doctor = ""
        user["appointment"] = {"service": "", "problem": "", "doctor": doctor, "date": "", "time": "", "promotion": ""}
        show_services(user_id, appointment_flow=True)
    elif action and action.startswith("service_category:"):
        try:
            index = int(action.split(":", 1)[1])
            category = SERVICE_CATEGORIES[index]
        except (ValueError, IndexError):
            send_main_menu(user_id)
            return
        user["appointment"] = {"service": category, "problem": "", "doctor": "Подберёт администратор", "date": "", "time": "", "promotion": ""}
        show_booking_dates(user_id)
    else:
        send_main_menu(user_id)


def find_client_in_bitrix(phone: str) -> dict[str, Any]:
    """Ищет клиента в Bitrix24 по номеру телефона; возвращает пустой результат при ошибке."""
    phone_clean = re.sub(r"\D", "", phone or "")
    if len(phone_clean) == 11 and phone_clean[0] in "78":
        phone_clean = phone_clean[1:]
    if not phone_clean:
        return {"found": False}
    result = bitrix_call("crm.item.list", {
        "entityTypeId": 1,
        "filter": {"%PHONE": phone_clean},
        "select": ["id", "title", "name", "assignedById"],
    })
    items = result.get("result", {}).get("items", []) if isinstance(result, dict) else []
    if items:
        item = items[0]
        return {"found": True, "type": "LEAD", "id": item.get("id"),
                "name": item.get("name") or item.get("title") or "Клиент"}
    result = bitrix_call("crm.item.list", {
        "entityTypeId": 3,
        "filter": {"%PHONE": phone_clean},
        "select": ["id", "name", "lastName", "assignedById"],
    })
    items = result.get("result", {}).get("items", []) if isinstance(result, dict) else []
    if items:
        item = items[0]
        full_name = f"{item.get('name', '')} {item.get('lastName', '')}".strip()
        return {"found": True, "type": "CONTACT", "id": item.get("id"), "name": full_name or "Клиент"}
    return {"found": False}


def process_phone(user_id: int, text: str) -> None:
    phone = normalize_phone(text)
    if not phone:
        send_message(user_id, "Не удалось распознать номер. Введите российский номер в формате +7 988 233-77-25.")
        return
    user = session_for(user_id); user["phone"] = "+7" + phone
    client = find_client_in_bitrix(user["phone"])
    if client.get("found"):
        user["bitrix_id"] = client.get("id"); user["bitrix_type"] = client.get("type")
        user["name"] = client.get("name") or user.get("name") or "Пациент"
    try:
        response=requests.post(f"{AUTH_API_URL}/api/auth/register",json={"platform":"max","phone":user["phone"],"name":user.get("name","")},headers={"X-Internal-Auth":AUTH_API_SECRET},timeout=10)
        if response.status_code==200:
            data=response.json()
            user["access_token"]=data["access_token"]
            user["auth"]=True;user["auth_until"]=0;user["step"]=None;persist_session(user_id)
            send_message(user_id,"✅ Аккаунт создан. Ваш постоянный пароль — сохраните его, повторно он не будет показан:\n\n"+data["password"]+"\n\nНе передавайте пароль другим людям.")
            dispatch_after_auth(user_id);return
        if response.status_code==409:
            user["step"]="waiting_password";persist_session(user_id)
            send_message(user_id,"Для этого номера уже существует аккаунт. Введите постоянный пароль.");return
        raise RuntimeError(response.json().get("detail","Сервис авторизации недоступен"))
    except Exception as exc:
        logger.exception("Auth register failed MAX")
        send_message(user_id,"Не удалось создать аккаунт: "+str(exc)+". Попробуйте позже.")

def process_password(user_id: int, text: str) -> None:
    user=session_for(user_id)
    try:
        response=requests.post(f"{AUTH_API_URL}/api/auth/login",json={"platform":"max","phone":user.get("phone",""),"password":text},headers={"X-Internal-Auth":AUTH_API_SECRET},timeout=10)
        data=response.json()
        if response.ok and data.get("authorized"):
            user["access_token"]=data["access_token"]
            user["auth"]=True;user["auth_until"]=0;user["step"]=None;persist_session(user_id)
            send_message(user_id,"✅ Вход выполнен.");dispatch_after_auth(user_id)
        else: send_message(user_id,data.get("detail","Неверный пароль."))
    except Exception:
        logger.exception("Auth login failed MAX");send_message(user_id,"Сервис авторизации временно недоступен. Попробуйте позже.")


def show_my_visits(user_id: int) -> None:
    send_message(user_id, "📅 Мои визиты\n\nПолучение реальных визитов из МИС «Инфоклиника» пока не подключено. Я не буду показывать фиктивные записи или имитировать подтверждение, перенос и отмену. Можно отправить запрос администратору.", [
        [callback("📨 Запросить информацию о визитах", "visits_request")],
        *miniapp_rows(),
        [callback("⬅️ Главное меню", "back_to_menu")],
    ])


def show_services(user_id: int, appointment_flow: bool = False) -> None:
    if appointment_flow:
        rows = [[callback(service, f"book_service_{i}")] for i, service in enumerate(SERVICES)]
        rows.append([callback("Не уверен(а) в услуге — подберите на консультации", "book_service_unknown")])
        rows.append([callback("⬅️ Главное меню", "back_to_menu")])
        note = "🦷 Запись на приём\n\nВыберите одну из 9 согласованных услуг. Дату и время можно указать как пожелание — фактическое свободное окно подтвердит администратор, поскольку МИС «Инфоклиника» не подключена."
    else:
        rows = [[callback(category, f"service_category_{i}")] for i, category in enumerate(SERVICE_CATEGORIES)]
        rows.extend(miniapp_rows("📱 Полный каталог и цены в Mini App"))
        rows.append([callback("🦷 Записаться", "appointment")])
        rows.append([callback("⬅️ Главное меню", "back_to_menu")])
        note = "📋 Услуги и цены\n\nВыберите направление. Подробные карточки с составом, показаниями, длительностью, актуальной ценой и врачами доступны в существующем Mini App. В чате не публикуем неподтверждённые цены и соответствия врачей."
    send_message(user_id, note, rows)


def show_service_category_detail(user_id: int, index: int) -> None:
    if not 0 <= index < len(SERVICE_CATEGORIES):
        send_message(user_id, "Направление не найдено.", [[callback("⬅️ К услугам", "services")]])
        return
    category = SERVICE_CATEGORIES[index]
    rows = []
    rows.extend(miniapp_rows("📱 Открыть карточки в Mini App"))
    rows.extend([
        [callback("🦷 Оставить заявку по направлению", f"service_category_book_{index}")],
        [callback("⬅️ К услугам и ценам", "services")],
    ])
    send_message(user_id, f"📋 {category}\n\nПолный состав услуги, длительность, актуальную стоимость и подходящих специалистов смотрите в Mini App. Для записи можно оставить заявку; администратор уточнит детали и подберёт врача.", rows)

def show_doctors(user_id: int, page: int = 0, appointment_flow: bool = False) -> None:
    pages = max(1, (len(DOCTORS) + DOCTORS_PER_PAGE - 1) // DOCTORS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * DOCTORS_PER_PAGE
    rows = [[callback(f"👩‍⚕️ {doctor}", f"book_doctor_{i}" if appointment_flow else f"doctor_info_{i}")]
            for i, doctor in enumerate(DOCTORS[start:start + DOCTORS_PER_PAGE], start)]
    nav = []
    if page > 0:
        nav.append(callback("◀️", f"book_doctor_page_{page-1}" if appointment_flow else f"doctors_{page-1}"))
    if page < pages - 1:
        nav.append(callback("▶️", f"book_doctor_page_{page+1}" if appointment_flow else f"doctors_{page+1}"))
    if nav:
        rows.append(nav)
    if appointment_flow:
        rows.extend([[callback("Пусть врача подберёт администратор", "book_doctor_unknown")], [callback("⬅️ К услугам", "book_change_service")]])
        txt = "Выберите врача только как пожелание. Связь «услуга → врач» ещё не подтверждена, поэтому администратор проверит, оказывает ли выбранный специалист эту услугу. Можно поручить подбор администратору."
    else:
        rows.extend(miniapp_rows("Карточки врачей в Mini App"))
        rows.append([callback("⬅️ Главное меню", "back_to_menu")])
        txt = "👩‍⚕️ Врачи клиники\n\nСписок ФИО из предоставленного файла. Специализации, ключевые услуги, стаж и свободные окна в MAX не приписываем без проверенных данных. Полные карточки доступны в Mini App."
    send_message(user_id, txt, rows)


def show_booking_dates(user_id: int) -> None:
    rows = []
    today = datetime.now(CLINIC_TZ).date()
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for offset in range(1, 8):
        day = today + timedelta(days=offset)
        rows.append([callback(f"{day:%d.%m.%Y} ({weekdays[day.weekday()]})", f"book_date_{day:%Y%m%d}")])
    rows.append([callback("⬅️ К врачу", "book_change_doctor")])
    send_message(user_id, "Выберите желаемую дату. Это предпочтение в заявке, а не подтверждённое свободное окно: МИС не подключена.", rows)


def show_booking_times(user_id: int) -> None:
    slots = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00"]
    rows = []
    for i in range(0, len(slots), 3):
        rows.append([callback(t, f"book_time_{t.replace(':', '')}") for t in slots[i:i + 3]])
    rows.append([callback("Время не важно", "book_time_any")])
    rows.append([callback("⬅️ К дате", "book_change_date")])
    day = session_for(user_id).get("appointment", {}).get("date", "")
    send_message(user_id, f"Выберите желаемое время на {day}. Список показывает только пожелание, а не реальные свободные окна.", rows)


def show_booking_confirmation(user_id: int) -> None:
    user = session_for(user_id)
    a = user.get("appointment", {})
    text = ("✅ Проверьте данные заявки\n\n"
            f"Услуга: {a.get('service') or 'уточнить с администратором'}\n"
            f"Врач: {a.get('doctor') or 'подберёт администратор'}\n"
            f"Акция: {a.get('promotion') or 'не выбрана'}\n"
            f"Желаемая дата: {a.get('date') or 'не указана'}\n"
            f"Желаемое время: {a.get('time') or 'не важно'}\n"
            f"Клиника: {CLINIC_ADDRESS}\nИмя: {user.get('name') or 'не указано'}\nТелефон: {user.get('phone') or 'не указан'}\n\n"
            "Это заявка, не подтверждённая запись. Администратор проверит доступность и свяжется с вами.")
    send_message(user_id, text, [
        [callback("✅ Подтвердить заявку", "book_submit")],
        [callback("Изменить услугу", "book_change_service"), callback("Изменить врача", "book_change_doctor")],
        [callback("Изменить дату", "book_change_date"), callback("Изменить время", "book_change_time")],
        [callback("⬅️ Главное меню", "back_to_menu")],
    ])


def start_booking(user_id: int, promo: Optional[dict[str, Any]] = None) -> None:
    user = session_for(user_id)
    if not user.get("auth"):
        user["pending_action"] = f"promo:{promo.get('id')}" if promo else "booking"
        require_auth(user_id, user["pending_action"])
        return
    user["appointment"] = {"service": (promo or {}).get("service", ""), "problem": "", "doctor": "",
                            "date": "", "time": "", "promotion": (promo or {}).get("title", "")}
    if promo and promo.get("service"):
        # The promotion's service is known; do not invent the doctor mapping. Ask for preference or admin selection.
        show_doctor_choice_for_booking(user_id)
    else:
        show_services(user_id, appointment_flow=True)


def show_doctor_choice_for_booking(user_id: int) -> None:
    user = session_for(user_id)
    a = user.setdefault("appointment", {})
    # If the patient started from an individual doctor's page, preserve that as a preference.
    if a.get("doctor") and a.get("doctor") != "Подберёт администратор":
        send_message(user_id, f"Врач как пожелание: {a.get('doctor')}. Администратор подтвердит, оказывает ли специалист выбранную услугу.", [[callback("➡️ Выбрать желаемую дату", "book_keep_doctor_preference")], [callback("Изменить услугу", "book_change_service")]])
        return
    txt = (f"Услуга: {a.get('service') or 'уточнить'}\n\n"
           "Связка «услуга → врач» из МИС не предоставлена. Чтобы не показывать неподходящих специалистов, врач будет подобран администратором."
           " Вы можете указать пожелание по дате и времени; свободные окна будут подтверждены отдельно.")
    send_message(user_id, txt, [[callback("Пусть администрация подберёт врача", "book_doctor_unknown")], [callback("⬅️ К услугам", "book_change_service")]])

def submit_booking(user_id: int) -> None:
    user = session_for(user_id)
    result = create_appointment_lead(user_id)
    lead_id = lead_id_from(result)
    if lead_id:
        message = f"✅ Заявка принята, номер #{lead_id}. Администратор подтвердит возможность записи. Это ещё не подтверждённый визит."
    else:
        message = f"⚠️ Не удалось подтвердить передачу заявки в CRM. Данные сохранены в резервной очереди; позвоните в клинику: {CLINIC_PHONE}."
    user["appointment"] = {}
    user["step"] = None
    send_message(user_id, message, [[callback("🏠 Главное меню", "back_to_menu")], [callback("💬 Написать администратору", "support")]])


def show_promotions(user_id: int) -> None:
    promos = load_promotions()
    if not promos:
        send_message(user_id, "Список акций сейчас недоступен. Уточните предложения у администратора или откройте Mini App.", miniapp_rows() + [[callback("💬 Написать администратору", "support")], [callback("⬅️ Главное меню", "back_to_menu")]])
        return
    rows = [[callback(f"🎁 {p.get('title','Акция')} · {p.get('price','')}", f"promo_open_{p.get('id')}")] for p in promos]
    rows.extend(miniapp_rows("Открыть акции в Mini App"))
    rows.append([callback("⬅️ Главное меню", "back_to_menu")])
    send_message(user_id, "🎁 Акции и предложения\n\nСписок загружается из файла существующего Mini App. Сроки части предложений в исходной таблице не указаны; перед записью администратор подтвердит актуальность условий.", rows)


def show_promotion_detail(user_id: int, promo_id: str) -> None:
    promo = promotion_by_id(promo_id)
    if not promo:
        send_message(user_id, "Акция не найдена. Обновите список предложений.", [[callback("⬅️ К акциям", "promo")]])
        return
    doctors = ", ".join(promo.get("doctors") or []) or "Уточнить у администратора"
    text = (f"🎁 {promo.get('title','Акция')}\n\nЦена / предложение: {promo.get('price','Условия уточняются')}\n\n"
            f"{promo.get('summary','')}\n\nУсловия и ограничения: {promo.get('conditions','Уточнить у администратора')}\n\n"
            f"Врачи: {doctors}\n\nАктуальность и применимость предложения подтвердит администратор.")
    rows = [[callback("🦷 Записаться по акции", f"promo_book_{promo.get('id')}")],
            [callback("⬅️ К акциям", "promo")]]
    if str(promo.get("more_url", "")).startswith("https://"):
        rows.insert(1, [link_button("Подробнее об акции", promo["more_url"])])
    send_message(user_id, text, rows)


def start_promotion_booking(user_id: int, promo_id: str) -> None:
    promo = promotion_by_id(promo_id)
    if not promo:
        send_message(user_id, "Акция не найдена. Сначала откройте список акций.", [[callback("🎁 К акциям", "promo")]])
        return
    start_booking(user_id, promo)


def show_emergency(user_id: int) -> None:
    rows = [[callback(problem, f"emergency_issue_{i}")] for i, problem in enumerate(EMERGENCY_PROBLEMS)]
    rows.append([callback("⬅️ Главное меню", "back_to_menu")])
    send_message(user_id, "🚨 Экстренная помощь\n\nВыберите проблему, затем напишите комментарий и оставьте телефон для связи. Обращение будет передано администратору. При критическом состоянии не ждите ответа в чате.", rows)


def submit_emergency(user_id: int) -> None:
    user = session_for(user_id)
    problem = user.pop("emergency_problem", "Экстренное обращение")
    comment = user.pop("emergency_comment", "")
    phone = user.get("phone", "")
    description = f"Проблема: {problem}\nКомментарий пациента: {comment}\nВремя обращения: {datetime.now(CLINIC_TZ).isoformat(timespec='seconds')}"
    result = create_support_request(user_id, f"ЭКСТРЕННОЕ обращение: {problem}", description, phone, priority=1)
    task_id = result.get("result", {}).get("task", {}).get("id")
    lead_id = lead_id_from(result)
    if task_id:
        message = f"Обращение передано администратору. Номер задачи #{task_id}."
    elif lead_id:
        message = f"Обращение передано в CRM. Номер обращения #{lead_id}."
    else:
        message = f"Не удалось подтвердить передачу. Позвоните в клинику: {CLINIC_PHONE}. Данные сохранены для повторной обработки."
    user["step"] = None
    send_message(user_id, "🚨 " + message, [[callback("🏠 Главное меню", "back_to_menu")], [callback("📞 Позвонить в клинику", "clinic_phone")]])


def begin_support(user_id: int) -> None:
    session_for(user_id)["step"] = "waiting_support"
    send_message(user_id, "💬 Напишите одним сообщением ваш вопрос администратору. Для ответа в MAX авторизация не требуется.", [[callback("⬅️ Отмена", "back_to_menu")]])


def handle_text(user_id: int, text: str) -> None:
    user = session_for(user_id)
    state = user.get("step")
    command = (text or "").strip().lower().split(maxsplit=1)[0] if (text or "").strip() else ""
    if command in ("/start", "/menu") or command.startswith("/start@") or command.startswith("/menu@") or text.strip().lower() in ("меню", "главное меню"):
        user["step"] = None
        send_main_menu(user_id)
        return
    if command == "/shift_start":
        if user_id not in MAX_ADMIN_IDS:
            send_message(user_id, "Команда доступна только администраторам.")
            return
        end = start_shift(user_id)
        send_message(user_id, f"Смена начата до {end:%H:%M}. Активных администраторов: {len(get_active_shift_users())}.")
        return
    if command == "/shift_end":
        if user_id not in MAX_ADMIN_IDS:
            send_message(user_id, "Команда доступна только администраторам.")
            return
        send_message(user_id, "Смена завершена." if end_shift(user_id) else "У вас нет активной смены.")
        return
    if command in ("/shift_status", "/shiftstatus"):
        if user_id not in MAX_ADMIN_IDS:
            send_message(user_id, "Команда доступна только администраторам.")
            return
        active = get_active_shift_users()
        send_message(user_id, "На смене: " + (", ".join(map(str, active)) if active else "сейчас никого нет"))
        return
    if state == "waiting_phone":
        process_phone(user_id, text)
        return
    if state == "waiting_password":
        process_password(user_id, text)
        return
    if user.get("auth") and not validate_auth_session(user_id, user):
        send_message(user_id, "Сессия завершена. Снова войдите по телефону и постоянному паролю.")
        user["step"] = "waiting_phone"
        persist_session(user_id)
        return
    if state == "waiting_support":
        user["step"] = None
        result = create_support_request(user_id, "Вопрос пациента", text, priority=1)
        task_id = result.get("result", {}).get("task", {}).get("id")
        lead_id = lead_id_from(result)
        msg = f"Обращение передано администратору. Номер задачи #{task_id}." if task_id else (f"Обращение передано в CRM. Номер обращения #{lead_id}." if lead_id else f"Передачу не удалось подтвердить. Позвоните: {CLINIC_PHONE}.")
        send_message(user_id, msg, [[callback("🏠 Главное меню", "back_to_menu")]])
        return
    if state == "waiting_emergency_comment":
        user["emergency_comment"] = text[:1500]
        if user.get("phone"):
            submit_emergency(user_id)
        else:
            user["step"] = "waiting_emergency_phone"
            send_message(user_id, "Оставьте телефон для обратной связи, например +7 988 233-77-25.")
        return
    if state == "waiting_emergency_phone":
        phone = normalize_phone(text)
        if not phone:
            send_message(user_id, "Не удалось распознать номер. Введите +7 988 233-77-25.")
            return
        user["phone"] = "+7" + phone
        submit_emergency(user_id)
        return
    send_main_menu(user_id)


def dispatch_callback(user_id: int, data: str) -> None:
    user = session_for(user_id)
    a = user.setdefault("appointment", {})
    if data in ("back_to_menu", "back_to_main", "menu"):
        user["step"] = None
        send_main_menu(user_id)
    elif data == "appointment":
        if require_auth(user_id, "booking"):
            start_booking(user_id)
    elif data == "my_visits":
        if require_auth(user_id, "visits"):
            show_my_visits(user_id)
    elif data == "visits_request":
        result = create_support_request(user_id, "Запрос по визитам", "Пациент просит предоставить сведения о реальных визитах из МИС «Инфоклиника».")
        task_id = result.get("result", {}).get("task", {}).get("id")
        lead_id = lead_id_from(result)
        msg = f"Запрос передан администратору. Номер задачи #{task_id}." if task_id else (f"Запрос передан в CRM. Номер обращения #{lead_id}." if lead_id else f"Передачу не удалось подтвердить. Позвоните: {CLINIC_PHONE}.")
        send_message(user_id, msg, [[callback("🏠 Главное меню", "back_to_menu")]])
    elif data == "emergency":
        show_emergency(user_id)
    elif data.startswith("emergency_issue_"):
        try:
            idx = int(data.rsplit("_", 1)[1]); problem = EMERGENCY_PROBLEMS[idx]
        except (ValueError, IndexError):
            problem = "Экстренное обращение"
        user["emergency_problem"] = problem
        user["step"] = "waiting_emergency_comment"
        send_message(user_id, f"Вы выбрали: {problem}.\n\nОпишите, что произошло. При сильной боли, отёке или резком ухудшении состояния не ждите ответа в чате.")
    elif data == "services":
        show_services(user_id)
    elif data.startswith("service_category_book_"):
        try:
            index = int(data.rsplit("_", 1)[1])
            category = SERVICE_CATEGORIES[index]
        except (ValueError, IndexError):
            send_message(user_id, "Направление не найдено.", [[callback("⬅️ К услугам", "services")]])
            return
        if not require_auth(user_id, f"service_category:{index}"):
            return
        user["appointment"] = {"service": category, "problem": "", "doctor": "Подберёт администратор", "date": "", "time": "", "promotion": ""}
        show_booking_dates(user_id)
    elif data.startswith("service_category_"):
        try:
            index = int(data.rsplit("_", 1)[1])
        except ValueError:
            index = -1
        show_service_category_detail(user_id, index)
    elif data.startswith("service_info_"):
        try: service = SERVICES[int(data.rsplit("_", 1)[1])]
        except (ValueError, IndexError): service = "Услуга"
        send_message(user_id, f"📋 {service}\n\nСостав, длительность и актуальную стоимость подтвердит клиника. Подробная карточка доступна в Mini App.", miniapp_rows() + [[callback("🦷 Оставить заявку", "appointment")], [callback("⬅️ К услугам", "services")]])
    elif data == "doctors" or data.startswith("doctors_"):
        try: page = int(data.rsplit("_", 1)[1]) if data.rsplit("_", 1)[1].isdigit() else 0
        except ValueError: page = 0
        show_doctors(user_id, page)
    elif data.startswith("doctor_info_"):
        try: idx = int(data.rsplit("_", 1)[1]); doctor = DOCTORS[idx]
        except (ValueError, IndexError):
            send_message(user_id, "Врач не найден.", [[callback("⬅️ К врачам", "doctors_0")]]); return
        send_message(user_id, f"👩‍⚕️ {doctor}\n\nПодробная карточка, специализация, ключевые услуги и ближайшее свободное время доступны в Mini App. Запись к этому врачу — только пожелание, администратор подтвердит соответствие услуги и расписание.", [[callback("🦷 Оставить пожелание по врачу", f"book_specific_doc_{idx}")], [callback("⬅️ К врачам", "doctors_0")]])
    elif data.startswith("book_specific_doc_"):
        if not require_auth(user_id, f"doctor:{data.rsplit('_', 1)[1]}"):
            return
        try: idx = int(data.rsplit("_", 1)[1]); doctor = DOCTORS[idx]
        except (ValueError, IndexError):
            send_message(user_id, "Не удалось выбрать врача."); return
        user["appointment"] = {"service": "", "problem": "", "doctor": doctor, "date": "", "time": "", "promotion": ""}
        show_services(user_id, appointment_flow=True)
    elif data == "promo":
        show_promotions(user_id)
    elif data.startswith("promo_open_"):
        show_promotion_detail(user_id, data.rsplit("_", 1)[1])
    elif data.startswith("promo_book_"):
        start_promotion_booking(user_id, data.rsplit("_", 1)[1])
    elif data == "about":
        text = ("🏥 О клинике «Альфа-Дент»\n\nКлиника работает с 2011 года.\n\n"
                f"Адрес: {CLINIC_ADDRESS}\nГрафик: {CLINIC_HOURS}\nТелефон / WhatsApp: {CLINIC_PHONE}\n"
                "Парковка: городская платная. Оплата: наличными и безналичным способом.\n\n"
                "Преимущества: комплексный подход, цифровые технологии, лечение под микроскопом, КЛКТ-диагностика, имплантация и сложные случаи, лечение во сне и седация.\n\n"
                "Видео о клинике и полные сведения доступны в приложении клиники.")
        rows = [[link_button("🌐 Сайт клиники", "https://alfadentstom.ru/")], [link_button("📍 Яндекс Карты", YANDEX_MAPS_URL)]]
        rows.extend(miniapp_rows("📱 Видео и подробности в Mini App"))
        rows.extend([[callback("🦷 Записаться", "appointment")], [callback("⬅️ Главное меню", "back_to_menu")]])
        send_message(user_id, text, rows)
    elif data == "reviews":
        rows = [[link_button("⭐ Яндекс Карты", YANDEX_MAPS_URL)], [link_button("⭐ ПроДокторов", "https://prodoctorov.ru/sochi/alfadent/")], [link_button("⭐ 2ГИС", "https://2gis.ru/sochi/firm/alfadent/reviews")], [callback("⬅️ Главное меню", "back_to_menu")]]
        send_message(user_id, "⭐ Отзывы\n\nПроверьте актуальность страниц перед публикацией.", rows)
    elif data == "referral":
        profile_url = max_bot_profile_url()
        if profile_url:
            link = f"{profile_url}?startapp=ref_{user_id}" if "?" not in profile_url else f"{profile_url}&startapp=ref_{user_id}"
        else:
            link = MAX_MINIAPP_LINK
        send_message(user_id, f"💛 Рекомендовать клинику\n\nПоделитесь ссылкой с друзьями. Условия бонусов не обещаем без согласования.\n\n{link}", [[link_button("📤 Поделиться ссылкой", link)], [callback("📋 Показать ссылку", "referral_show")], [callback("⬅️ Главное меню", "back_to_menu")]])
    elif data == "referral_show":
        profile_url = max_bot_profile_url()
        link = (f"{profile_url}?startapp=ref_{user_id}" if "?" not in profile_url else f"{profile_url}&startapp=ref_{user_id}") if profile_url else MAX_MINIAPP_LINK
        send_message(user_id, link, [[callback("⬅️ Главное меню", "back_to_menu")]])
    elif data == "support":
        begin_support(user_id)
    elif data == "clinic_phone":
        send_message(user_id, f"Телефон клиники: {CLINIC_PHONE}")
    elif data == "book_service_unknown":
        a["service"] = "Не знаю — требуется консультация"
        show_doctor_choice_for_booking(user_id)
    elif data.startswith("book_service_"):
        try: idx = int(data.rsplit("_", 1)[1]); service = SERVICES[idx]
        except (ValueError, IndexError):
            send_message(user_id, "Не удалось выбрать услугу. Выберите ещё раз."); return
        a["service"] = service
        show_doctor_choice_for_booking(user_id)
    elif data == "book_choose_doctor":
        show_doctors(user_id, 0, appointment_flow=True)
    elif data.startswith("book_doctor_page_"):
        try: show_doctors(user_id, int(data.rsplit("_", 1)[1]), appointment_flow=True)
        except ValueError: show_doctors(user_id, 0, appointment_flow=True)
    elif data.startswith("book_doctor_") and data != "book_doctor_unknown":
        try: idx = int(data.rsplit("_", 1)[1]); a["doctor"] = DOCTORS[idx]
        except (ValueError, IndexError): a["doctor"] = ""
        show_booking_dates(user_id)
    elif data == "book_keep_doctor_preference":
        show_booking_dates(user_id)
    elif data == "book_doctor_unknown":
        a["doctor"] = "Подберёт администратор"
        show_booking_dates(user_id)
    elif data == "book_change_service":
        show_services(user_id, appointment_flow=True)
    elif data == "book_change_doctor":
        show_doctors(user_id, 0, appointment_flow=True)
    elif data.startswith("book_date_"):
        raw = data.removeprefix("book_date_")
        try: a["date"] = datetime.strptime(raw, "%Y%m%d").strftime("%d.%m.%Y")
        except ValueError:
            send_message(user_id, "Некорректная дата. Выберите ещё раз."); return
        show_booking_times(user_id)
    elif data == "book_time_any":
        a["time"] = "Не важно"
        show_booking_confirmation(user_id)
    elif data.startswith("book_time_"):
        raw = data.removeprefix("book_time_")
        if re.fullmatch(r"\d{4}", raw): a["time"] = raw[:2] + ":" + raw[2:]
        else: a["time"] = "Не важно"
        show_booking_confirmation(user_id)
    elif data == "book_change_date":
        show_booking_dates(user_id)
    elif data == "book_change_time":
        show_booking_times(user_id)
    elif data == "book_submit":
        submit_booking(user_id)
    else:
        send_message(user_id, "Не удалось распознать действие. Вернитесь в главное меню.", [[callback("🏠 Главное меню", "back_to_menu")]])


def update_user_id(data: dict[str, Any]) -> Optional[int]:
    update_type = data.get("update_type")
    if update_type == "bot_started":
        user = data.get("user") or {}
        if not user:
            user = (data.get("message") or {}).get("sender") or {}
    elif update_type == "message_callback":
        callback_data = data.get("callback") or {}
        user = callback_data.get("user") or {}
        if not user:
            user = ((data.get("message") or {}).get("sender") or {})
    elif update_type in {"message_created", "bot_stopped", "bot_added", "bot_removed"}:
        if update_type == "message_created":
            user = ((data.get("message") or {}).get("sender") or {})
        else:
            user = data.get("user") or {}
    else:
        user = {}
    raw_id = user.get("user_id") if isinstance(user, dict) else None
    try:
        return int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        return None


def handle_update(data: dict[str, Any]) -> None:
    update_type = data.get("update_type")
    user_id = update_user_id(data)
    logger.info("Получено событие MAX: type=%s", update_type)
    try:
        if update_type == "bot_started":
            # MAX Update.bot_started stores the starter in top-level `user`.
            sender = data.get("user") or {}
            if not sender:
                sender = ((data.get("message") or {}).get("sender") or {})
            if user_id is not None:
                first_name = sender.get("first_name") or sender.get("name") or "Пациент"
                user = session_for(user_id, first_name)
                user["step"] = None
                send_main_menu(user_id)
            else:
                logger.warning("bot_started: в событии не найден user.user_id")

        elif update_type == "message_created":
            message = data.get("message") or {}
            sender = message.get("sender") or {}
            if user_id is not None:
                first_name = sender.get("first_name") or sender.get("name") or "Пациент"
                session_for(user_id, first_name)
                body = message.get("body") or {}
                text_value = body.get("text") or message.get("text") or ""
                handle_text(user_id, str(text_value).strip())
            else:
                logger.warning("message_created: в message.sender не найден user_id")

        elif update_type == "message_callback":
            callback_data = data.get("callback") or {}
            payload = str(callback_data.get("payload") or "")
            if user_id is not None:
                callback_user = callback_data.get("user") or {}
                first_name = callback_user.get("first_name") or callback_user.get("name") or "Пациент"
                session_for(user_id, first_name)
                dispatch_callback(user_id, payload)
            else:
                logger.warning("message_callback: в callback.user не найден user_id")

        elif update_type == "bot_stopped":
            if user_id is not None:
                with SESSION_LOCK:
                    user_sessions.pop(user_id, None)
                with DB_LOCK, db_connect() as conn:
                    conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
                logger.info("Пользователь остановил MAX-бота; сессия очищена")

        else:
            logger.info("Событие MAX пропущено: update_type=%s", update_type)
    finally:
        if user_id is not None and update_type != "bot_stopped":
            persist_session(user_id)


def update_fingerprint(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enqueue_webhook_update(data: dict[str, Any]) -> bool:
    """Persist update before acknowledging MAX; unique hash prevents duplicate delivery."""
    event_key = update_fingerprint(data)
    now = time.time()
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with DB_LOCK, db_connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO webhook_events(event_key,payload,status,attempts,next_attempt,created_at,updated_at,last_error) "
            "VALUES(?,?,'pending',0,?,?,?,'')",
            (event_key, encoded, now, now, now),
        )
        inserted = cursor.rowcount > 0
    if inserted:
        WEBHOOK_QUEUE_WAKE.set()
    return inserted


def process_webhook_queue() -> None:
    """Process persisted events once; failed events are stored for manual inspection."""
    while not WEBHOOK_WORKER_STOP.is_set():
        row = None
        try:
            with DB_LOCK, db_connect() as conn:
                row = conn.execute(
                    "SELECT event_key,payload,attempts FROM webhook_events "
                    "WHERE status='pending' AND next_attempt<=? ORDER BY created_at LIMIT 1",
                    (time.time(),),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE webhook_events SET status='processing', updated_at=? WHERE event_key=? AND status='pending'",
                        (time.time(), row["event_key"]),
                    )
        except sqlite3.Error:
            logger.exception("Ошибка чтения очереди MAX webhook; worker повторит проверку через 2 секунды")
            WEBHOOK_WORKER_STOP.wait(2.0)
            continue
        if not row:
            WEBHOOK_QUEUE_WAKE.wait(2.0)
            WEBHOOK_QUEUE_WAKE.clear()
            continue

        event_key = row["event_key"]
        attempts = int(row["attempts"] or 0) + 1
        try:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise ValueError("Webhook payload is not a JSON object")
            handle_update(payload)
            with DB_LOCK, db_connect() as conn:
                conn.execute(
                    "UPDATE webhook_events SET status='done', attempts=?, updated_at=?, last_error='' WHERE event_key=?",
                    (attempts, time.time(), event_key),
                )
            logger.info("Событие MAX обработано успешно (attempt=%s)", attempts)
        except Exception as exc:
            # A webhook may already have created a CRM lead/task before the exception.
            # Automatic replay could duplicate that side effect. Store it for inspection.
            logger.exception("Ошибка обработки события MAX; автоматический повтор отключён")
            try:
                with DB_LOCK, db_connect() as conn:
                    conn.execute(
                        "UPDATE webhook_events SET status='failed', attempts=?, updated_at=?, last_error=? WHERE event_key=?",
                        (attempts, time.time(), f"{type(exc).__name__}: {str(exc)[:500]}", event_key),
                    )
            except sqlite3.Error:
                logger.exception("Не удалось записать статус failed в очередь MAX webhook")
            logger.error("Событие MAX помещено в failed без автоматического повтора; проверьте очередь через /health и БД %s", DATABASE_FILE)


def start_webhook_worker() -> None:
    global WEBHOOK_WORKER_THREAD
    if WEBHOOK_WORKER_THREAD and WEBHOOK_WORKER_THREAD.is_alive():
        return
    WEBHOOK_WORKER_STOP.clear()
    WEBHOOK_WORKER_THREAD = threading.Thread(target=process_webhook_queue, name="max-webhook-worker", daemon=True)
    WEBHOOK_WORKER_THREAD.start()


def webhook_secret_is_valid(path_secret: Optional[str] = None) -> bool:
    expected = MAX_WEBHOOK_SECRET.strip()
    if not expected:
        return not MAX_REQUIRE_WEBHOOK_SECRET
    supplied = request.headers.get("X-Max-Bot-Api-Secret", "").strip()
    # Compatibility: the current Quick Tunnel supervisor appends the secret to /webhook/<secret>.
    # MAX's recommended mechanism is the X-Max-Bot-Api-Secret header.
    if not supplied and path_secret:
        supplied = path_secret.strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


@app.get("/")
def root():
    return jsonify({"ok": True, "service": "alfadent-max-bot", "health": "/health"})


@app.get("/health")
def health():
    with DB_LOCK, db_connect() as conn:
        queue_counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM webhook_events GROUP BY status").fetchall()
        }
    return jsonify({
        "ok": True,
        "service": "alfadent-max-bot",
        "max_token_configured": bool(MAX_BOT_TOKEN and MAX_BOT_TOKEN.strip() and not MAX_BOT_TOKEN.startswith("PASTE_NEW_")),
        "bitrix_configured": bool(BITRIX24_WEBHOOK and not BITRIX24_WEBHOOK.startswith("PASTE_NEW_")),
        "webhook_secret_configured": bool(MAX_WEBHOOK_SECRET.strip()),
        "webhook_secret_required": MAX_REQUIRE_WEBHOOK_SECRET,
        "miniapp_open_app_configured": bool(MAX_BOT_USERNAME or MAX_BOT_CONTACT_ID),
        "promotions_cache_count": len(_promotions_cache),
        "webhook_queue": queue_counts,
    })


def process_webhook(path_secret: Optional[str] = None):
    if MAX_REQUIRE_WEBHOOK_SECRET and not MAX_WEBHOOK_SECRET.strip():
        logger.error("Webhook отклонён: MAX_REQUIRE_WEBHOOK_SECRET=1, но MAX_WEBHOOK_SECRET не настроен")
        return jsonify({"error": "webhook secret is not configured"}), 503
    if MAX_WEBHOOK_SECRET and not webhook_secret_is_valid(path_secret):
        logger.warning("Webhook отклонён: неверный X-Max-Bot-Api-Secret")
        return jsonify({"error": "forbidden"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "ignored", "reason": "invalid json"}), 400
    try:
        inserted = enqueue_webhook_update(data)
    except sqlite3.Error:
        logger.exception("Не удалось записать входящий webhook MAX в очередь")
        return jsonify({"status": "queue_error"}), 500
    # MAX expects a fast HTTP 200. The durable local worker handles the event afterwards.
    return jsonify({"status": "queued" if inserted else "duplicate"}), 200


@app.post("/webhook")
def webhook():
    return process_webhook()


@app.post("/webhook/<path_secret>")
def webhook_with_secret(path_secret: str):
    return process_webhook(path_secret)


def main() -> None:
    if not MAX_API_BASE.startswith("https://"):
        raise RuntimeError("MAX_API_BASE должен быть HTTPS URL.")
    if not MAX_BOT_TOKEN or not MAX_BOT_TOKEN.strip() or MAX_BOT_TOKEN.startswith("PASTE_NEW_"):
        raise RuntimeError("MAX_BOT_TOKEN не настроен. Добавьте новый токен в /etc/alfadent/max_bot.env.")
    if not AUTH_API_SECRET:
        raise RuntimeError("Не задан INTERNAL_AUTH_SECRET: MAX-боту нужен защищённый доступ к общему API авторизации.")
    if not BITRIX24_WEBHOOK or BITRIX24_WEBHOOK.startswith("PASTE_NEW_"):
        logger.warning("BITRIX24_WEBHOOK не настроен: обращения будут сохраняться в локальную резервную очередь.")
    if MAX_WEBHOOK_SECRET.strip() and not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", MAX_WEBHOOK_SECRET.strip()):
        raise RuntimeError("MAX_WEBHOOK_SECRET должен содержать 5–256 символов: латинские буквы, цифры, дефис или подчёркивание.")
    if not MAX_WEBHOOK_SECRET.strip():
        message = "MAX_WEBHOOK_SECRET не настроен: webhook не будет проверяться на подлинность. Рекомендуется задать секрет в MAX и в конфигурации сервера."
        if MAX_REQUIRE_WEBHOOK_SECRET:
            raise RuntimeError(message)
        logger.warning(message)
    if not (MAX_BOT_USERNAME or MAX_BOT_CONTACT_ID):
        logger.warning("MAX_BOT_USERNAME/MAX_BOT_CONTACT_ID не заданы: в сообщениях будет обычная HTTPS-ссылка Mini App, не open_app.")
    if not MAX_TO_BITRIX_ADMIN_MAP:
        logger.warning("MAX_TO_BITRIX_ADMIN_MAP не задана: задачи смены не будут назначаться на MAX ID; будет создан лид Bitrix24.")
    if not MAX_ADMIN_IDS:
        logger.warning("MAX_ADMIN_IDS пуст: команды /shift_start, /shift_end доступны никому.")
    if BITRIX_ASSIGNED_BY_ID <= 0:
        raise RuntimeError("BITRIX_ASSIGNED_BY_ID должен быть положительным Bitrix24 user ID.")

    try:
        from waitress import serve
    except ImportError as exc:
        raise RuntimeError("Не установлен Waitress. Выполните: .venv/bin/pip install waitress") from exc

    start_webhook_worker()
    # The Flask/Waitress service is private; Nginx is the only public entry point.
    host = "127.0.0.1"
    port = 5000
    if not 1 <= port <= 65535:
        raise RuntimeError("API_PORT должен находиться в диапазоне 1–65535.")
    threads = 8
    logger.info("Запуск MAX webhook через Waitress на %s:%s (threads=%s)", host, port, threads)
    try:
        serve(app, host=host, port=port, threads=threads, channel_timeout=30, cleanup_interval=30)
    finally:
        WEBHOOK_WORKER_STOP.set()
        WEBHOOK_QUEUE_WAKE.set()
        if WEBHOOK_WORKER_THREAD:
            WEBHOOK_WORKER_THREAD.join(timeout=5)


if __name__ == "__main__":
    main()
