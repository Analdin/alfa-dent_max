#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAX webhook bot for AlfaDent. Uses the existing Mini App URL and Bitrix24 CRM.

The bot does not claim a visit is booked: without the MIS API, it submits a request
for the administrator to confirm. Secrets are read from environment variables/.env.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MAX_API_BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru").rstrip("/")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_MINIAPP_LINK = os.getenv("MAX_MINIAPP_LINK", "https://alfadent-miniapp.onrender.com/miniapp.html").strip()
_MAX_BOT_USERNAME_RAW = os.getenv("MAX_BOT_USERNAME", "").strip().lstrip("@")
MAX_BOT_USERNAME = "" if _MAX_BOT_USERNAME_RAW.startswith("PASTE_") else _MAX_BOT_USERNAME_RAW
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
_BITRIX24_WEBHOOK_RAW = os.getenv("BITRIX24_WEBHOOK", "").strip().rstrip("/")
BITRIX24_WEBHOOK = "" if _BITRIX24_WEBHOOK_RAW.startswith("PASTE_NEW_") else _BITRIX24_WEBHOOK_RAW
BITRIX_ASSIGNED_BY_ID = int(os.getenv("BITRIX_ASSIGNED_BY_ID", "33"))
BITRIX_ADMIN_IDS = [int(x.strip()) for x in os.getenv("BITRIX_ADMIN_IDS", "").split(",") if x.strip().isdigit()]
MAX_ADMIN_TO_BITRIX: dict[int, int] = {}
for _pair in os.getenv("MAX_ADMIN_TO_BITRIX", "").split(","):
    if ":" in _pair:
        try:
            _max_id, _b24_id = _pair.split(":", 1)
            MAX_ADMIN_TO_BITRIX[int(_max_id.strip())] = int(_b24_id.strip())
        except ValueError:
            pass
ADMIN_IDS = sorted(set(BITRIX_ADMIN_IDS + list(MAX_ADMIN_TO_BITRIX.values())))
OTP_CODE = os.getenv("DEMO_OTP_CODE", "1234").strip() or "1234"
CLINIC_PHONE = os.getenv("CLINIC_PHONE", "+7 (988) 233-77-25")
CLINIC_ADDRESS = os.getenv("CLINIC_ADDRESS", "г. Сочи, ул. Северная, 12, корп. 2, 2 этаж. Вход с торца здания, рядом с магазином «Коляски».")
CLINIC_HOURS = os.getenv("CLINIC_HOURS", "Ежедневно, 09:00–20:00")
YANDEX_MAPS_URL = os.getenv("YANDEX_MAPS_URL", "https://yandex.ru/medicine/clinic/alfa_dent_1617563656")
PROMOTIONS_URL = os.getenv("PROMOTIONS_URL", "https://alfadent-miniapp.onrender.com/assets/promotions.json").strip()
PROMOTIONS_FILE = BASE_DIR / "web" / "assets" / "promotions.json"
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PENDING_FILE = DATA_DIR / "pending_requests.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "max_bot.log", encoding="utf-8")],
)
logger = logging.getLogger("alfadent_max_bot")
app = Flask(__name__)

SERVICES = [
    "Консультация", "ПРОФ гигиена", "Лечение кариеса", "Удаление зуба", "Имплантация",
    "Протезирование", "Ортодонтия (брекеты, элайнеры)", "Гнатология / ВНЧС", "Диагностика (КЛКТ)",
]
PROBLEMS = ["Болит зуб", "Отёк, воспаление", "Скол зуба", "Выпала коронка", "Ребёнку нужна помощь", "Не знаю, что нужно"]
EMERGENCY_PROBLEMS = ["Острая боль", "Отёк", "Скол зуба", "Выпала коронка", "Ребёнку больно", "Нужен срочный звонок"]
# Names are taken from the source supplied by the clinic. Specialities and service mappings are not inferred.
DOCTORS = [
    "Батаева Анжела Маусыровна", "Бобовский Николай Анатольевич", "Богуцкий Сергей Владимирович",
    "Будайханова Айгуль Тамирлановна", "Бычкова Елена Викторовна", "Вылегжанина Екатерина Александровна",
    "Гаспарян Грачья Артурович", "Героль Дарья Олеговна", "Гитинов Шамиль Магомедович",
    "Гурьянова Анна Владимировна", "Дячук Кирилл Алексеевич", "Захарова Екатерина Алексеевна",
    "Зиновьева Диана Наримановна", "Казарян Инна Владимировна", "Мамеян Джульетта Анзоровна",
    "Мельникова Ольга Игоревна", "Минасян Кристина Андреевна", "Оганесян Тигран Сергеевич",
    "Петросян Гаянэ Борисовна", "Петросян Грант Эрикович", "Раблюк Анна", "Роскошная Валерия Витальевна",
    "Спивак Екатерина Алексеевна", "Татулян Анна Робертовна", "Широков Игорь Валентинович",
    "Ягафаров Наиль Фанилевич", "Якунина Александра Александровна",
]
DOCTORS_PER_PAGE = 6
user_sessions: dict[int, dict[str, Any]] = {}
active_shifts: dict[int, dict[str, datetime]] = {}
_promotions_cache: list[dict[str, Any]] = []
_promotions_cache_at = 0.0


def session_for(user_id: int, first_name: str = "Пациент") -> dict[str, Any]:
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "name": first_name or "Пациент", "auth": False, "phone": "", "step": None,
            "pending_action": None, "appointment": {},
        }
    elif first_name and user_sessions[user_id].get("name") in (None, "Пациент"):
        user_sessions[user_id]["name"] = first_name
    return user_sessions[user_id]


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
    try:
        existing: list[Any] = []
        if PENDING_FILE.exists():
            raw = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = raw
        existing.append({**payload, "saved_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        PENDING_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Не удалось сохранить резервное обращение")


def bitrix_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if not BITRIX24_WEBHOOK:
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
    result = bitrix_call("crm.item.add", {"entityTypeId": 1, "fields": {
        "title": title[:255], "name": (user.get("name") or "Пациент")[:100],
        "comments": description, "sourceId": "WEB", "assignedById": BITRIX_ASSIGNED_BY_ID,
        "OPENED": "Y", "observers": ADMIN_IDS,
    }})
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
    active = get_active_shift_users()
    if not active:
        return {"error": "Нет администраторов на активной смене"}
    for max_admin_id in active:
        b24_id = MAX_ADMIN_TO_BITRIX.get(max_admin_id)
        if not b24_id:
            continue
        result = bitrix_call("tasks.task.add", {"fields": {
            "TITLE": title[:255], "DESCRIPTION": description, "RESPONSIBLE_ID": b24_id, "PRIORITY": priority,
        }})
        if result.get("result", {}).get("task", {}).get("id"):
            return result
    return {"error": "Не удалось создать задачу для администраторов на смене"}


def create_support_request(user_id: int, subject: str, description: str, phone: str = "", priority: int = 2) -> dict[str, Any]:
    user = session_for(user_id)
    full = (f"Пациент: {user.get('name') or 'не указано'}\nТелефон: {phone or user.get('phone') or 'не указан'}\n"
            f"MAX user_id: {user_id}\nКанал: MAX\nВремя обращения: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n{description}")
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
    now = datetime.now()
    end = datetime(now.year, now.month, now.day, 20, 0)
    if now >= end:
        end += timedelta(days=1)
    active_shifts[user_id] = {"start_time": now, "end_time": end}
    return end


def end_shift(user_id: int) -> bool:
    return active_shifts.pop(user_id, None) is not None


def get_active_shift_users() -> list[int]:
    now = datetime.now()
    for uid in [uid for uid, s in active_shifts.items() if now >= s["end_time"]]:
        active_shifts.pop(uid, None)
    return list(active_shifts)


def callback(text: str, payload: str) -> dict[str, Any]:
    return {"type": "callback", "text": text[:64], "payload": payload[:128]}


def link_button(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": text[:64], "url": url}


def app_button(text: str = "📱 Открыть приложение клиники") -> Optional[dict[str, Any]]:
    # Use MAX's native open_app button; a plain HTTPS link may open outside the Mini App
    # context and then the API cannot validate window.WebApp.initData.
    if MAX_BOT_USERNAME:
        return {"type": "open_app", "text": text[:64], "web_app": MAX_BOT_USERNAME}
    return None


def keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"attachments": [{"type": "inline_keyboard", "payload": {"buttons": rows}}]} if rows else {}


def send_message(user_id: int, text: str, rows: Optional[list[list[dict[str, Any]]]] = None) -> dict[str, Any]:
    if not MAX_BOT_TOKEN or MAX_BOT_TOKEN.startswith("PASTE_NEW_"):
        logger.error("MAX_BOT_TOKEN не настроен; сообщение не отправлено")
        return {"error": "MAX_BOT_TOKEN не настроен"}
    payload: dict[str, Any] = {"text": text[:4000]}
    if rows:
        payload.update(keyboard(rows))
    try:
        response = requests.post(
            f"{MAX_API_BASE}/messages?user_id={user_id}",
            headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        if not response.ok:
            logger.error("MAX API %s: %s", response.status_code, response.text[:500])
        response.raise_for_status()
        return response.json() if response.content else {"ok": True}
    except Exception as exc:
        logger.exception("Не удалось отправить MAX-сообщение пользователю %s", user_id)
        return {"error": str(exc)}


def miniapp_rows() -> list[list[dict[str, Any]]]:
    b = app_button()
    return [[b]] if b else []


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


def require_auth(user_id: int, action: str) -> bool:
    user = session_for(user_id)
    if user.get("auth"):
        return True
    user["pending_action"] = action
    user["step"] = "waiting_phone"
    send_message(user_id, "🔐 Для этой функции нужно подтвердить номер телефона.\n\nВведите российский номер, например +7 988 233-77-25.\nSMS-провайдер пока не подключён: после номера будет предложен временный код 1234.", [[callback("⬅️ Главное меню", "back_to_menu")]])
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
    else:
        send_main_menu(user_id)


def process_phone(user_id: int, text: str) -> None:
    phone = normalize_phone(text)
    if not phone:
        send_message(user_id, "Не удалось распознать номер. Введите российский номер в формате +7 988 233-77-25.")
        return
    user = session_for(user_id)
    user["phone"] = "+7" + phone
    user["step"] = "waiting_code"
    send_message(user_id, f"Номер принят. SMS сейчас не отправляется, поскольку провайдер не подключён.\n\nВведите временный код: {OTP_CODE}")


def process_code(user_id: int, text: str) -> None:
    user = session_for(user_id)
    if text.strip() != OTP_CODE:
        send_message(user_id, "Неверный временный код. Попробуйте ещё раз.")
        return
    user["auth"] = True
    user["step"] = None
    send_message(user_id, "✅ Временное подтверждение выполнено. Реальная SMS-проверка не подключена.")
    dispatch_after_auth(user_id)


def show_my_visits(user_id: int) -> None:
    send_message(user_id, "📅 Мои визиты\n\nПолучение реальных визитов из МИС «Инфоклиника» пока не подключено. Я не буду показывать фиктивные записи или имитировать подтверждение, перенос и отмену. Можно отправить запрос администратору.", [
        [callback("📨 Запросить информацию о визитах", "visits_request")],
        [app_button("Открыть приложение клиники")] if app_button() else [callback("🏠 Главное меню", "back_to_menu")],
        [callback("⬅️ Главное меню", "back_to_menu")],
    ])


def show_services(user_id: int, appointment_flow: bool = False) -> None:
    rows = [[callback(service, f"book_service_{i}" if appointment_flow else f"service_info_{i}")] for i, service in enumerate(SERVICES)]
    if not appointment_flow:
        b = app_button("Открыть каталог в Mini App")
        if b:
            rows.append([b])
        rows.append([callback("🦷 Оставить заявку", "appointment")])
    else:
        rows.append([callback("Не уверен(а) в услуге — подберите на консультации", "book_service_unknown")])
    rows.append([callback("⬅️ Главное меню", "back_to_menu")])
    note = "Выберите услугу для заявки. Точный состав и стоимость подтвердит администратор." if appointment_flow else "📋 Услуги и цены\n\nОсновной список из клиентского ТЗ. Цены в чате не выдумываем: уточняйте стоимость по актуальному прайс-листу или откройте Mini App."
    send_message(user_id, note, rows)


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
        b = app_button("Карточки врачей в Mini App")
        if b:
            rows.append([b])
        rows.append([callback("⬅️ Главное меню", "back_to_menu")])
        txt = "👩‍⚕️ Врачи клиники\n\nСписок ФИО из предоставленного файла. Специализации, ключевые услуги, стаж и свободные окна в MAX не приписываем без проверенных данных. Полные карточки доступны в Mini App."
    send_message(user_id, txt, rows)


def show_booking_dates(user_id: int) -> None:
    rows = []
    today = date.today()
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
    txt = (f"🎁 Акция: {a.get('promotion')}\nУслуга: {a.get('service') or 'уточнить'}\n\n"
           "Связь с врачом не подтверждена. Вы можете оставить пожелание по врачу или поручить подбор администратору.")
    rows = [[callback("Пусть администрация подберёт врача", "book_doctor_unknown")],
            [callback("Выбрать врача как пожелание", "book_choose_doctor")],
            [callback("⬅️ К услугам", "book_change_service")]]
    send_message(user_id, txt, rows)


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
    b = app_button("Открыть акции в Mini App")
    if b:
        rows.append([b])
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
    description = f"Проблема: {problem}\nКомментарий пациента: {comment}\nВремя обращения: {datetime.now().astimezone().isoformat(timespec='seconds')}"
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
    if text.lower() in ("/start", "/menu", "меню", "главное меню"):
        user["step"] = None
        send_main_menu(user_id)
        return
    if text == "/shift_start":
        if user_id not in MAX_ADMIN_TO_BITRIX:
            send_message(user_id, "Команда доступна только администраторам с настроенным соответствием MAX ID → Bitrix24 ID.")
            return
        end = start_shift(user_id)
        send_message(user_id, f"Смена начата до {end:%H:%M}. Активных администраторов: {len(get_active_shift_users())}.")
        return
    if text == "/shift_end":
        if user_id not in MAX_ADMIN_TO_BITRIX:
            send_message(user_id, "Команда доступна только администраторам.")
            return
        send_message(user_id, "Смена завершена." if end_shift(user_id) else "У вас нет активной смены.")
        return
    if text == "/shift_status":
        send_message(user_id, "На смене: " + (", ".join(map(str, get_active_shift_users())) if get_active_shift_users() else "сейчас никого нет"))
        return
    if state == "waiting_phone":
        process_phone(user_id, text)
        return
    if state == "waiting_code":
        process_code(user_id, text)
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
                f"Адрес: {CLINIC_ADDRESS}\nГрафик: {CLINIC_HOURS}\nТелефон / WhatsApp: {CLINIC_PHONE}\n\n"
                "Преимущества: комплексный подход, цифровые технологии, лечение под микроскопом, КЛКТ-диагностика, имплантация и сложные случаи, лечение во сне и седация.")
        send_message(user_id, text, [[link_button("🌐 Сайт клиники", "https://alfadentstom.ru/")], [link_button("📍 Яндекс Карты", YANDEX_MAPS_URL)], [callback("🦷 Записаться", "appointment")], [callback("⬅️ Главное меню", "back_to_menu")]])
    elif data == "reviews":
        rows = [[link_button("⭐ Яндекс Карты", YANDEX_MAPS_URL)], [link_button("⭐ ПроДокторов", "https://prodoctorov.ru/sochi/alfadent/")], [link_button("⭐ 2ГИС", "https://2gis.ru/sochi/firm/alfadent/reviews")], [callback("⬅️ Главное меню", "back_to_menu")]]
        send_message(user_id, "⭐ Отзывы\n\nПроверьте актуальность страниц перед публикацией.", rows)
    elif data == "referral":
        link = f"https://max.ru/{MAX_BOT_USERNAME}?startapp=ref_{user_id}" if MAX_BOT_USERNAME else "https://alfadentstom.ru/"
        send_message(user_id, f"💛 Рекомендовать клинику\n\nПоделитесь ссылкой с друзьями. Условия бонусов не обещаем без согласования.\n\n{link}", [[link_button("📤 Поделиться ссылкой", link)], [callback("📋 Показать ссылку", "referral_show")], [callback("⬅️ Главное меню", "back_to_menu")]])
    elif data == "referral_show":
        link = f"https://max.ru/{MAX_BOT_USERNAME}?startapp=ref_{user_id}" if MAX_BOT_USERNAME else "https://alfadentstom.ru/"
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
    elif data == "book_doctor_unknown":
        a["doctor"] = "Подберёт администратор"
        show_booking_dates(user_id)
    elif data == "book_change_service":
        show_services(user_id, appointment_flow=True)
    elif data == "book_change_doctor":
        show_doctor_choice_for_booking(user_id)
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


def handle_update(data: dict[str, Any]) -> None:
    update_type = data.get("update_type")
    if update_type == "bot_started":
        message = data.get("message", {}) or {}
        sender = message.get("sender", {}) or {}
        user_id = sender.get("user_id")
        if user_id is not None:
            session_for(int(user_id), sender.get("first_name", "Пациент"))["step"] = None
            send_main_menu(int(user_id))
    elif update_type == "message_created":
        message = data.get("message", {}) or {}
        sender = message.get("sender", {}) or {}
        user_id = sender.get("user_id")
        if user_id is not None:
            user_id = int(user_id)
            session_for(user_id, sender.get("first_name", "Пациент"))
            body = message.get("body", {}) or {}
            handle_text(user_id, str(body.get("text") or "").strip())
    elif update_type == "message_callback":
        callback_data = data.get("callback", {}) or {}
        user = callback_data.get("user", {}) or {}
        user_id = user.get("user_id")
        if user_id is None:
            sender = (data.get("message", {}) or {}).get("sender", {}) or {}
            user_id = sender.get("user_id")
        if user_id is not None:
            dispatch_callback(int(user_id), str(callback_data.get("payload") or ""))
    else:
        logger.info("Игнорируем событие MAX update_type=%s", update_type)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "alfadent-max-bot", "max_token_configured": bool(MAX_BOT_TOKEN and not MAX_BOT_TOKEN.startswith("PASTE_NEW_")), "bitrix_configured": bool(BITRIX24_WEBHOOK), "promotions_loaded": len(load_promotions())})


def process_webhook(path_secret: Optional[str] = None):
    if MAX_WEBHOOK_SECRET and path_secret != MAX_WEBHOOK_SECRET:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "ignored", "reason": "invalid json"}), 200
    try:
        handle_update(data)
    except Exception:
        logger.exception("Ошибка обработки MAX webhook")
    # Answer 200 to avoid endless redelivery; errors are preserved in logs.
    return jsonify({"status": "ok"}), 200


@app.post("/webhook")
def webhook():
    return process_webhook()


@app.post("/webhook/<path_secret>")
def webhook_with_secret(path_secret: str):
    return process_webhook(path_secret)


def main() -> None:
    if not MAX_BOT_TOKEN or MAX_BOT_TOKEN.startswith("PASTE_NEW_"):
        raise RuntimeError("Укажите новый MAX_BOT_TOKEN в конфигурации сервера.")
    if not BITRIX24_WEBHOOK or BITRIX24_WEBHOOK.startswith("PASTE_NEW_"):
        logger.warning("BITRIX24_WEBHOOK не настроен: CRM-заявки не будут переданы автоматически.")
    if not MAX_MINIAPP_LINK.startswith("https://"):
        logger.warning("MAX_MINIAPP_LINK должен быть публичным HTTPS URL.")
    if not MAX_BOT_USERNAME:
        logger.warning("MAX_BOT_USERNAME не настроен: inline open_app кнопки скрыты. Привяжите URL Mini App в MAX Partner Portal и укажите публичное имя текущего бота.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)


if __name__ == "__main__":
    main()
