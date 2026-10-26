"""Telegram-бот клиники «АльфаДент».

Заявки на приём создаются в Bitrix24 и требуют подтверждения администратора;
прямое бронирование свободного окна, визиты, перенос и отмена требуют интеграции
с МИС «Инфоклиника». До подключения SMS/OTP используется временный фиксированный
код подтверждения номера 1234.
"""

import asyncio
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# ========== БИТРИКС24 ==========
BITRIX24_WEBHOOK = "https://alfadent23.bitrix24.ru/rest/11599/tleyme67jjk7x2gf/"

# ---------------- CONFIGURATION ----------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "PASTE_NEW_TELEGRAM_BOT_TOKEN_HERE").strip()
BITRIX24_WEBHOOK_RAW = os.getenv("BITRIX24_WEBHOOK", "PASTE_NEW_BITRIX24_WEBHOOK_HERE").strip().rstrip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://alfadent-miniapp.onrender.com/miniapp.html").strip().rstrip("/")
# SOCKS5-прокси используется только для запросов Telegram Bot API и getUpdates.
# Укажите НОВЫЕ действующие учётные данные: прежние данные попадали в скриншоты.
# Спецсимволы в логине/пароле необходимо URL-кодировать.
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "socks5://LOGIN:PASSWORD@IP:PORT").strip()
BITRIX24_WEBHOOK = BITRIX24_WEBHOOK_RAW + "/" if BITRIX24_WEBHOOK_RAW and not BITRIX24_WEBHOOK_RAW.startswith("PASTE_NEW_") else ""
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
BITRIX_ADMIN_IDS = [int(x.strip()) for x in os.getenv("BITRIX_ADMIN_IDS", "").split(",") if x.strip().isdigit()]
# Временный фиксированный код подтверждения до подключения SMS/OTP-провайдера.
OTP_CODE = "1234"
CLINIC_PHONE = "+7 (988) 233-77-25"
CLINIC_ADDRESS = "г. Сочи, ул. Северная, 12, корп. 2, 2 этаж. Ориентир: с торца здания, рядом с магазином «Коляски»."
CLINIC_HOURS = "Ежедневно, 09:00–20:00"
YANDEX_MAPS_URL = "https://yandex.ru/medicine/clinic/alfa_dent_1617563656"
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "./data"))
PENDING_FILE = DATA_DIR / "pending_requests.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "bot_telegram.log", encoding="utf-8")],
)
logger = logging.getLogger("alfadent_bot")

# Состояния в памяти. Для долговременного хранения сессий/визитов нужна БД и API МИС.
user_sessions: Dict[int, Dict[str, Any]] = {}
active_shifts: Dict[int, Dict[str, datetime]] = {}

# В списке — имена из файла бота; непроверенные специальности, цены и расписание не добавляем.
DOCTORS = [
    "Батаева Анжела Маусыровна", "Бобовский Николай Анатольевич",
    "Богуцкий Сергей Владимирович", "Будайханова Айгуль Тамирлановна",
    "Бычкова Елена Викторовна", "Вылегжанина Екатерина Александровна",
    "Гаспарян Грачья Артурович", "Героль Дарья Олеговна",
    "Гитинов Шамиль Магомедович", "Гурьянова Анна Владимировна",
    "Дячук Кирилл Алексеевич", "Захарова Екатерина Алексеевна",
    "Зиновьева Диана Наримановна", "Казарян Инна Владимировна",
    "Мамеян Джульетта Анзоровна", "Мельникова Ольга Игоревна",
    "Минасян Кристина Андреевна", "Оганесян Тигран Сергеевич",
    "Петросян Гаянэ Борисовна", "Петросян Грант Эрикович",
    "Раблюк Анна", "Роскошная Валерия Витальевна",
    "Спивак Екатерина Алексеевна", "Татулян Анна Робертовна",
    "Широков Игорь Валентинович", "Ягафаров Наиль Фанилевич",
    "Якунина Александра Александровна",
]
DOCTORS_PER_PAGE = 5
PROMOTIONS_FILE = Path(__file__).resolve().parent / "web" / "assets" / "promotions.json"


def load_promotions() -> list[Dict[str, Any]]:
    """Read patient-facing promotion data supplied by the clinic."""
    try:
        with PROMOTIONS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, dict) and p.get("id") and p.get("title")]
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Не удалось загрузить файл акций %s: %s", PROMOTIONS_FILE, exc)
        return []


def find_promotion(promotion_id: str | int) -> Optional[Dict[str, Any]]:
    return next((p for p in load_promotions() if str(p.get("id")) == str(promotion_id)), None)

SERVICES = [
    "Консультация", "ПРОФ гигиена", "Лечение кариеса", "Удаление зуба",
    "Имплантация", "Протезирование", "Ортодонтия (брекеты, элайнеры)",
    "Гнатология / ВНЧС", "Диагностика (КЛКТ)",
]
PROBLEMS = ["Болит зуб", "Отёк, воспаление", "Скол зуба", "Выпала коронка", "Ребёнку нужна помощь", "Не знаю, что нужно"]
EMERGENCY_PROBLEMS = ["Острая боль", "Отёк", "Скол зуба", "Выпала коронка", "Ребёнку больно", "Нужен срочный звонок"]


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def keyboard(rows):
    """Build callback, URL and Telegram Mini App buttons."""
    built = []
    for row in rows:
        buttons = []
        for item in row:
            if len(item) == 2:
                buttons.append(InlineKeyboardButton(item[0], callback_data=item[1]))
            elif item[1] == "web_app":
                buttons.append(InlineKeyboardButton(item[0], web_app=WebAppInfo(url=item[2])))
            else:
                buttons.append(InlineKeyboardButton(item[0], url=item[2]))
        built.append(buttons)
    return InlineKeyboardMarkup(built)


def miniapp_button_row():
    if WEBAPP_URL.startswith("https://"):
        return [("📱 Открыть приложение клиники", "web_app", WEBAPP_URL)]
    return [("🧩 Mini App пока не настроен", "webapp_help")]


async def send(user_id: int, text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None):
    try:
        await context.bot.send_message(
            chat_id=user_id, text=text, reply_markup=reply_markup,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение user_id=%s", user_id)


def session_for(user_id: int, first_name: str = "Пациент") -> Dict[str, Any]:
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "step": None, "auth": False, "name": first_name or "Пациент",
            "phone": "", "appointment": {}, "pending_question": "",
        }
    elif first_name and user_sessions[user_id].get("name") in (None, "Пациент"):
        user_sessions[user_id]["name"] = first_name
    return user_sessions[user_id]


def normalize_phone(value: str) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def save_pending_request(payload: Dict[str, Any]) -> None:
    """Сохраняет обращение на диск, если CRM недоступна, чтобы не потерять его."""
    try:
        current = []
        if PENDING_FILE.exists():
            current = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
            if not isinstance(current, list):
                current = []
        current.append({**payload, "saved_at": datetime.now().isoformat(timespec="seconds")})
        PENDING_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Не удалось сохранить обращение локально")


def bitrix_call(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not BITRIX24_WEBHOOK.strip("/"):
        return {"error": "BITRIX24_WEBHOOK не настроен"}
    try:
        response = requests.post(f"{BITRIX24_WEBHOOK}{method}", json=params, timeout=15)
        response.raise_for_status()
        result = response.json()
        if result.get("error"):
            logger.error("Bitrix24 %s вернул ошибку: %s", method, result.get("error_description", result.get("error")))
        return result
    except Exception as exc:
        logger.exception("Ошибка вызова Bitrix24 method=%s", method)
        return {"error": str(exc)}


def create_task_for_admins(title: str, description: str, priority: int = 2) -> Dict[str, Any]:
    if not ADMIN_IDS:
        return {"error": "ADMIN_IDS не настроен"}
    # Сохраняем совместимость с текущей логикой: задачи распределяются по администраторам,
    # которые включили смену командами /shift_start.
    active_ids = get_active_shift_users()
    if not active_ids:
        return {"error": "Нет администраторов на активной смене"}
    results = []
    for admin_id in active_ids:
        result = bitrix_call("tasks.task.add", {"fields": {
            "TITLE": title[:255], "DESCRIPTION": description,
            "RESPONSIBLE_ID": admin_id, "PRIORITY": priority,
        }})
        results.append(result)
    successful = next((r for r in results if r.get("result", {}).get("task", {}).get("id")), None)
    return successful or {"error": "Не удалось создать задачу", "details": results}


def create_support_task(user_id: int, subject: str, description: str, priority: int = 2) -> Dict[str, Any]:
    user = user_sessions.get(user_id, {})
    full_description = (
        f"Пациент: {user.get('name', 'Не указано')}\n"
        f"Телефон: {user.get('phone') or 'не указан'}\n"
        f"Telegram ID: {user_id}\n\n{description}"
    )
    result = create_task_for_admins(f"[БОТ] {subject}", full_description, priority)
    if result.get("error"):
        save_pending_request({"type": subject, "user_id": user_id, "description": full_description})
        # Резервный канал — отправить обращение администраторам в Telegram.
        # Не обещаем, что оно доставлено, если отправка не подтверждена.
        return result
    return result


def create_appointment_lead(
    user_id: int,
    appointment: Dict[str, Any],
    channel: str = "Telegram",
    session_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a CRM request from Telegram or MAX without claiming an MIS booking."""
    session_key = session_user_id if session_user_id is not None else user_id
    user = user_sessions.get(session_key, {})
    channel = (channel or "Telegram").strip()[:40]
    channel_tag = "MAX" if channel.lower().startswith("max") else "БОТ"
    description = (
        f"Пациент: {user.get('name', 'Пациент')}\nТелефон: {user.get('phone') or 'не указан'}\n"
        f"ID пользователя {channel}: {user_id}\nКанал: {channel} Mini App\n"
        f"Проблема: {appointment.get('problem') or 'не указана'}\n"
        f"Услуга: {appointment.get('service') or 'не указана'}\n"
        f"Врач (пожелание): {appointment.get('doctor') or 'не выбран'}\n"
        f"Предпочтительная дата: {appointment.get('date') or 'не указана'}\n"
        f"Предпочтительное время: {appointment.get('time') or 'не указано'}\n"
        f"Акция: {appointment.get('promotion') or 'не выбрана'}\n"
        "Важно: это заявка; свободный слот не забронирован. Администратор должен подтвердить время."
    )
    title = (f"[{channel_tag}] Заявка по акции: {appointment.get('promotion')}" if appointment.get('promotion') else
             f"[{channel_tag}] Заявка на приём: {appointment.get('service') or 'консультация'}")
    assigned_by_id = int(os.getenv("BITRIX_ASSIGNED_BY_ID", "33"))
    result = bitrix_call("crm.item.add", {"entityTypeId": 1, "fields": {
        "title": title[:255], "name": user.get("name", "Пациент"),
        "comments": description, "sourceId": "WEB", "assignedById": assigned_by_id,
        "OPENED": "Y", "observers": BITRIX_ADMIN_IDS,
    }})
    lead_id = result.get("result", {}).get("item", {}).get("id")
    if lead_id and user.get("phone"):
        bitrix_call("crm.item.update", {"entityTypeId": 1, "id": lead_id, "fields": {
            "fm": [{"typeId": "PHONE", "valueType": "WORK", "value": user["phone"]}]
        }})
    if not lead_id:
        save_pending_request({"type": title, "user_id": user_id, "channel": channel,
                              "description": description, "phone": user.get("phone")})
    return result


# ---------------- STAFF SHIFT COMMANDS ----------------
def start_shift(user_id: int) -> datetime:
    now = datetime.now()
    end = datetime(now.year, now.month, now.day, 20, 0)
    if now >= end:
        end += timedelta(days=1)
    active_shifts[user_id] = {"start_time": now, "end_time": end}
    return end


def end_shift(user_id: int) -> bool:
    return active_shifts.pop(user_id, None) is not None


def get_active_shift_users():
    now = datetime.now()
    expired = [uid for uid, shift in active_shifts.items() if now >= shift["end_time"]]
    for uid in expired:
        active_shifts.pop(uid, None)
    return list(active_shifts)


def get_shift_end_time(user_id: int):
    shift = active_shifts.get(user_id)
    return shift.get("end_time") if shift else None


# ---------------- MENUS AND SCREENS ----------------
async def send_main_menu(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = user_sessions.get(user_id, {})
    name = esc(user.get("name") or "")
    greeting = f"Здравствуйте, {name}! 👋" if name and name != "Пациент" else "Здравствуйте! 👋"
    text = (
        f"<b>{greeting}</b>\n\nЯ бот стоматологической клиники «АльфаДент». "
        "Выберите, чем могу помочь.\n\n"
        f"📍 {esc(CLINIC_ADDRESS)}\n🕘 {esc(CLINIC_HOURS)}\n📞 {esc(CLINIC_PHONE)}"
    )
    rows = [
        miniapp_button_row(),
        [("🦷 Записаться", "appointment"), ("📅 Мои визиты", "my_visits")],
        [("🚨 Экстренная помощь", "emergency"), ("👩‍⚕️ Врачи", "doctors_0")],
        [("📋 Услуги и цены", "services"), ("🎁 Акции", "promo")],
        [("🏥 О клинике", "about"), ("💬 Написать администратору", "support")],
        [("⭐ Отзывы", "reviews"), ("💛 Рекомендовать клинику", "referral")],
    ]
    await send(user_id, text, context, keyboard(rows))


async def start_auth(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[user_id]["step"] = "waiting_phone"
    markup = keyboard([miniapp_button_row()])
    await send(user_id,
        "<b>Авторизация</b> 🔐\n\nВведите номер телефона, который вы указывали в клинике.\n"
        "Пример: +7 988 233-77-25\n\nДля доступа к личным визитам требуется подтверждение номера. "
        "Для просмотра услуг, врачей и формы заявки можно открыть Mini App.", context, markup)


async def show_appointment_entry(user_id: int, context: ContextTypes.DEFAULT_TYPE, preserve: bool = False):
    if not preserve:
        user_sessions[user_id]["appointment"] = {}
    user_sessions[user_id]["step"] = "appointment_entry"
    await send(user_id, "<b>Запись на приём</b>\nВыберите удобный способ:", context, keyboard([
        [("🦷 По проблеме", "book_by_problem")],
        [("📋 По услуге", "book_by_service")],
        [("👩‍⚕️ К конкретному врачу", "book_by_doctor")],
        [("🕒 На ближайшее время", "book_nearest")],
        [("⬅️ В главное меню", "back_menu")],
    ]))


async def show_problem_choices(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    rows = [[(f"{p}", f"problem_{i}")] for i, p in enumerate(PROBLEMS)]
    rows.append([("⬅️ Назад", "appointment")])
    user_sessions[user_id]["step"] = "choose_problem"
    await send(user_id, "Что вас беспокоит?", context, keyboard(rows))


async def show_services(user_id: int, context: ContextTypes.DEFAULT_TYPE, appointment_flow: bool = False):
    rows = [[(service, f"svc_{i}")] for i, service in enumerate(SERVICES)]
    rows.append([("⬅️ Назад в меню" if not appointment_flow else "⬅️ Назад", "back_menu" if not appointment_flow else "appointment")])
    if appointment_flow:
        user_sessions[user_id]["step"] = "choose_service"
        text = "Выберите услугу. Стоимость зависит от конкретной процедуры и уточняется у клиники."
    else:
        text = ("<b>Услуги и направления</b>\n\n" + "\n".join(f"• {esc(s)}" for s in SERVICES) +
                "\n\nАктуальные цены и состав лечения уточнит администратор. Вы можете оставить заявку на запись.")
        rows = [[(s, f"svc_info_{i}")] for i, s in enumerate(SERVICES)]
        rows.append([("🦷 Записаться", "appointment")])
        rows.append([("⬅️ В главное меню", "back_menu")])
    await send(user_id, text, context, keyboard(rows))


async def show_doctors(user_id: int, context: ContextTypes.DEFAULT_TYPE, page: int = 0, appointment_flow: bool = False):
    total_pages = max(1, (len(DOCTORS) + DOCTORS_PER_PAGE - 1) // DOCTORS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DOCTORS_PER_PAGE
    rows = [[(f"👩‍⚕️ {name}", f"doc_{start + i}")] for i, name in enumerate(DOCTORS[start:start + DOCTORS_PER_PAGE])]
    nav = []
    if page > 0:
        nav.append(("◀️", f"doctors_{page - 1}"))
    if page < total_pages - 1:
        nav.append(("▶️", f"doctors_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("⬅️ Назад в запись" if appointment_flow else "⬅️ В главное меню", "appointment" if appointment_flow else "back_menu")])
    user_sessions[user_id]["step"] = "choose_doctor" if appointment_flow else user_sessions[user_id].get("step")
    text = ("<b>Выберите врача</b>\n\n" if appointment_flow else "<b>Наши специалисты</b>\n\n")
    text += "Для уточнения специализации и доступного времени обратитесь к администратору клиники."
    await send(user_id, text, context, keyboard(rows))


async def show_emergency(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    rows = [[(p, f"emergency_{i}")] for i, p in enumerate(EMERGENCY_PROBLEMS)]
    rows.append([("⬅️ В главное меню", "back_menu")])
    await send(user_id,
        "<b>Экстренная помощь</b> 🚨\nВыберите проблему. Бот передаст обращение администратору.\n\n"
        "При угрозе здоровью или резком ухудшении состояния не ждите ответа в чате — позвоните в клинику или в экстренную службу.",
        context, keyboard(rows))


async def show_about(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>О клинике «АльфаДент»</b> 🏥\n\n"
        "Стоматологический центр для взрослых и детей в Сочи.\n\n"
        "<b>Наши преимущества</b>\n"
        "✓ Комплексный подход и цифровые технологии\n"
        "✓ КЛКТ-диагностика\n"
        "✓ Лечение под микроскопом\n"
        "✓ Лечение во сне и седация\n"
        "✓ Специалисты разных направлений\n"
        "✓ Удобная запись и сопровождение пациента\n\n"
        f"<b>Адрес:</b> {esc(CLINIC_ADDRESS)}\n"
        f"<b>График:</b> {esc(CLINIC_HOURS)}\n"
        f"<b>Телефон / WhatsApp:</b> {esc(CLINIC_PHONE)}\n"
        "<b>Парковка:</b> городская платная\n"
        "<b>Оплата:</b> наличными и безналичный расчёт"
    )
    await send(user_id, text, context, keyboard([
        [("📍 Яндекс Карты", "open_map")],
        [("💬 Написать администратору", "support")],
        [("🦷 Записаться", "appointment")],
        [("⬅️ В главное меню", "back_menu")],
    ]))


async def show_promotions(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    promotions = load_promotions()
    if not promotions:
        await send(user_id,
            "<b>Акции и предложения</b> 🎁\n\nСписок предложений временно недоступен. Уточните действующие условия у администратора.",
            context, keyboard([[ ("💬 Написать администратору", "support") ], [("⬅️ В главное меню", "back_menu")]]))
        return
    rows = [[(f"{i + 1}. {p['title'][:48]}", f"promo_view_{p['id']}")] for i, p in enumerate(promotions)]
    rows += [[("💬 Уточнить условия", "support")], [("🦷 Записаться", "appointment")], [("⬅️ В главное меню", "back_menu")]]
    await send(user_id,
        "<b>Акции и предложения «Альфа-Дент»</b> 🎁\n\n"
        "Выберите предложение, чтобы посмотреть подробности и оставить заявку.\n\n"
        "Важно: в файле клиники не для всех предложений указаны сроки и ограничения. Перед оформлением администратор подтвердит актуальность и применимость акции.",
        context, keyboard(rows))


async def show_promotion_detail(user_id: int, context: ContextTypes.DEFAULT_TYPE, promotion_id: str):
    p = find_promotion(promotion_id)
    if not p:
        await send(user_id, "Акция не найдена. Вернитесь к списку предложений.", context,
                   keyboard([[ ("⬅️ Все акции", "promo") ]]))
        return
    parts = [f"<b>{esc(p.get('title'))}</b>"]
    if p.get("price"):
        parts.append(f"<b>Цена / условие:</b> {esc(p.get('price'))}")
    if p.get("summary"):
        parts.append(esc(p.get("summary")))
    if p.get("doctors"):
        parts.append("<b>Врачи:</b> " + esc(", ".join(p.get("doctors", []))))
    if p.get("conditions"):
        parts.append("<b>Условия:</b> " + esc(p.get("conditions")))
    rows = [[("🦷 Записаться по акции", f"promo_book_{p['id']}")]]
    if p.get("more_url", "").startswith("https://"):
        rows.append([("🔎 Подробнее на сайте", "unused", p["more_url"])])
    rows += [[("⬅️ К списку акций", "promo")], [("💬 Уточнить у администратора", "support")]]
    await send(user_id, "\n\n".join(parts), context, keyboard(rows))


async def start_promotion_booking(user_id: int, context: ContextTypes.DEFAULT_TYPE, promotion_id: str):
    p = find_promotion(promotion_id)
    if not p:
        await send(user_id, "Не удалось найти акцию. Откройте раздел акций ещё раз.", context,
                   keyboard([[ ("🎁 Все акции", "promo") ]]))
        return
    user = session_for(user_id)
    user["appointment"] = {
        "promotion": p.get("title", ""),
        "promotion_id": str(p.get("id", "")),
        "service": p.get("service", "") or "",
        "mode": "promotion",
    }
    await show_appointment_entry(user_id, context, preserve=True)


async def show_reviews(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    await send(user_id,
        "<b>Отзывы о клинике</b> ⭐\n\nВы можете открыть страницу клиники на Яндекс Картах. "
        "Ссылки на 2ГИС и ПроДокторов необходимо подтвердить у заказчика.", context, keyboard([
            [("📍 Яндекс Карты", "open_map")], [("💬 Уточнить другие площадки", "support")], [("⬅️ В главное меню", "back_menu")]
        ]))


async def show_referral(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user_id}" if me.username else ""
    if not link:
        await send(user_id, "Не удалось сформировать ссылку. Попробуйте позже или обратитесь к администратору.", context)
        return
    await send(user_id,
        "<b>Рекомендовать клинику</b> 💛\n\nПоделитесь ссылкой на бота с друзьями. "
        "Условия возможных бонусов должны быть отдельно согласованы с клиникой.\n\n"
        f"Ваша ссылка:\n{esc(link)}", context, keyboard([
            [("📤 Поделиться ссылкой", "share_referral")], [("⬅️ В главное меню", "back_menu")]
        ]))


async def show_my_visits(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    # Не отображаем визиты, пока их нельзя достоверно получить из МИС.
    await send(user_id,
        "<b>Мои визиты</b> 📅\n\nЧтобы получить информацию о ваших визитах или уточнить изменения, отправьте запрос администратору клиники.",
        context, keyboard([
            [("📨 Запросить информацию о визитах", "visits_request")],
            [("🦷 Оставить заявку на приём", "appointment")],
            [("⬅️ В главное меню", "back_menu")],
        ]))

async def begin_support(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[user_id]["step"] = "waiting_support"
    await send(user_id,
        "<b>Написать администратору</b> 💬\n\nНапишите одним сообщением ваш вопрос. "
        "Бот не будет отвечать автоматически — обращение передаётся администратору.", context,
        keyboard([[("⬅️ Отмена", "back_menu")]]))


async def begin_booking(user_id: int, context: ContextTypes.DEFAULT_TYPE, mode: str):
    appt = user_sessions[user_id].setdefault("appointment", {})
    carry = {k: appt.get(k) for k in ("promotion", "promotion_id", "service") if appt.get(k)}
    appt.clear()
    appt.update(carry)
    appt["mode"] = mode
    if mode == "problem":
        await show_problem_choices(user_id, context)
    elif mode == "service":
        if appt.get("service"):
            await show_booking_doctor_step(user_id, context)
        else:
            await show_services(user_id, context, appointment_flow=True)
    elif mode == "doctor":
        await show_doctors(user_id, context, 0, appointment_flow=True)
    else:
        appt["problem"] = "Запись на ближайшее время"
        if appt.get("service"):
            await show_booking_doctor_step(user_id, context)
        else:
            await show_services(user_id, context, appointment_flow=True)


async def show_booking_doctor_step(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    await show_doctors(user_id, context, 0, appointment_flow=True)


async def show_date_choices(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[user_id]["step"] = "choose_date"
    appt = user_sessions[user_id]["appointment"]
    rows = []
    for offset in range(1, 8):
        d = datetime.now().date() + timedelta(days=offset)
        label = d.strftime("%d.%m.%Y")
        rows.append([(label, f"date_{d.strftime('%Y%m%d')}")])
    rows.append([("⬅️ Назад к услуге", "back_service")])
    await send(user_id,
        "Выберите желаемую дату. <b>Это предпочтение для заявки, не подтверждённое свободное окно.</b>", context, keyboard(rows))


async def show_time_choices(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[user_id]["step"] = "choose_time"
    rows = []
    times = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00"]
    for i in range(0, len(times), 3):
        rows.append([(t, f"time_{t.replace(':', '')}") for t in times[i:i + 3]])
    rows.append([("Не важно", "time_any")])
    rows.append([("⬅️ Назад к датам", "back_date")])
    await send(user_id,
        f"Выберите желаемое время на {esc(user_sessions[user_id]['appointment'].get('date'))}. "
        "Это пожелание; администратор подтвердит возможность записи.", context, keyboard(rows))


async def show_booking_confirmation(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[user_id]["step"] = "booking_confirm"
    a = user_sessions[user_id]["appointment"]
    text = (
        "<b>Проверьте заявку</b>\n\n"
        f"Проблема: {esc(a.get('problem') or 'не указана')}\n"
        f"Услуга: {esc(a.get('service') or 'не выбрана')}\n"
        f"Врач: {esc(a.get('doctor') or 'не выбран')}\n"
        f"Желаемая дата: {esc(a.get('date') or 'не указана')}\n"
        f"Желаемое время: {esc(a.get('time') or 'не важно')}\n"
        f"Акция: {esc(a.get('promotion') or 'не выбрана')}\n"
        f"Имя: {esc(user_sessions[user_id].get('name') or 'не указано')}\n"
        f"Телефон: {esc(user_sessions[user_id].get('phone') or 'не указан')}\n\n"
        "После отправки администратор проверит доступность и подтвердит запись."
    )
    await send(user_id, text, context, keyboard([
        [("✅ Отправить заявку", "booking_submit")],
        [("🔄 Начать заново", "appointment")],
        [("⬅️ Изменить время", "back_date")],
        [("⬅️ В главное меню", "back_menu")],
    ]))


async def submit_booking(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    a = user_sessions[user_id].get("appointment", {})
    result = create_appointment_lead(user_id, a)
    lead_id = result.get("result", {}).get("item", {}).get("id")
    if lead_id:
        text = f"✅ <b>Заявка принята.</b> Номер заявки: #{esc(lead_id)}. Администратор свяжется с вами для подтверждения времени.\n\nЭто ещё не подтверждённая запись."
    else:
        # Заявка записана в локальную очередь, если не удалось отправить в Bitrix24.
        text = "⚠️ Не удалось подтвердить передачу заявки в CRM. Данные сохранены локально для повторной обработки, но свяжитесь с клиникой по телефону " + esc(CLINIC_PHONE) + "."
    await send(user_id, text, context, keyboard([[("🏠 В главное меню", "back_menu")], [("💬 Написать администратору", "support")]]))
    user_sessions[user_id]["appointment"] = {}
    user_sessions[user_id]["step"] = None


async def send_admin_fallback(context: ContextTypes.DEFAULT_TYPE, subject: str, user_id: int, description: str):
    sent = False
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, f"<b>[БОТ] {esc(subject)}</b>\nПациент Telegram ID: {user_id}\n\n{esc(description)}", parse_mode=ParseMode.HTML)
            sent = True
        except Exception:
            logger.exception("Не удалось отправить обращение администратору %s", admin_id)
    return sent


# ---------------- AUTH AND MESSAGE HANDLING ----------------
async def process_phone(user_id: int, raw_phone: str, context: ContextTypes.DEFAULT_TYPE):
    phone = normalize_phone(raw_phone)
    if not phone:
        await send(user_id, "Не удалось распознать номер. Введите российский номер из 10 цифр после +7 или 8.", context)
        return
    user_sessions[user_id]["phone"] = "+7" + phone
    user_sessions[user_id]["step"] = "waiting_code"
    await send(
        user_id,
        "Номер принят. Для подтверждения введите текущий код <code>1234</code>. "
        "Автоматическая отправка SMS ещё не настроена.",
        context,
    )

async def process_code(user_id: int, code: str, context: ContextTypes.DEFAULT_TYPE):
    user = user_sessions[user_id]
    if code.strip() != OTP_CODE:
        await send(user_id, "Неверный код. Попробуйте ещё раз.", context)
        return
    user["auth"] = True
    user["step"] = None
    await send(user_id, "Номер подтверждён.", context)
    after_auth = user.pop("after_auth", "menu")
    if after_auth == "promo_book":
        promotion_id = str(user.pop("pending_promotion_id", ""))
        await start_promotion_booking(user_id, context, promotion_id)
    elif after_auth == "appointment":
        await show_appointment_entry(user_id, context)
    elif after_auth == "my_visits":
        await show_my_visits(user_id, context)
    else:
        await send_main_menu(user_id, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = session_for(tg_user.id, tg_user.first_name or "Пациент")
    if context.args and context.args[0].startswith("ref_"):
        user["referrer_id"] = context.args[0][4:]
        logger.info("Referral start: user_id=%s referrer=%s", tg_user.id, user["referrer_id"])
    await send_main_menu(tg_user.id, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message or not update.message.text:
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()
    user = session_for(user_id, update.effective_user.first_name or "Пациент")
    state = user.get("step")

    # Staff commands retained from the original project.
    if text == "/shift_start":
        if user_id not in ADMIN_IDS:
            await send(user_id, "Команда доступна только администраторам.", context); return
        end = start_shift(user_id)
        await send(user_id, f"Смена начата до {end:%H:%M}. Активных администраторов: {len(get_active_shift_users())}.", context); return
    if text == "/shift_end":
        if user_id not in ADMIN_IDS:
            await send(user_id, "Команда доступна только администраторам.", context); return
        await send(user_id, "Смена завершена." if end_shift(user_id) else "У вас нет активной смены.", context); return
    if text == "/shift_status":
        active = get_active_shift_users()
        await send(user_id, "На смене: " + (", ".join(map(str, active)) if active else "сейчас никого нет"), context); return

    if state == "waiting_phone":
        await process_phone(user_id, text, context); return
    if state == "waiting_code":
        await process_code(user_id, text, context); return
    if state == "waiting_support":
        result = create_support_task(user_id, "Вопрос пациента", text, priority=1)
        task_id = result.get("result", {}).get("task", {}).get("id")
        if task_id:
            await send(user_id, f"✅ Обращение передано администратору. Номер задачи: #{esc(task_id)}.", context)
        else:
            delivered = await send_admin_fallback(context, "Вопрос пациента (резервная отправка)", user_id,
                f"Телефон: {user.get('phone') or 'не указан'}\nВопрос: {text}")
            if delivered:
                await send(user_id, "Обращение отправлено администраторам резервным способом.", context)
            else:
                await send(user_id, f"Не удалось подтвердить доставку обращения. Пожалуйста, позвоните: {esc(CLINIC_PHONE)}. Обращение также сохранено для повторной обработки.", context)
        user["step"] = None
        await send_main_menu(user_id, context)
        return
    await send_main_menu(user_id, context)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = session_for(user_id, query.from_user.first_name or "Пациент")
    data = query.data or ""
    if data.startswith("promo_book_") and not user.get("auth"):
        user["pending_promotion_id"] = data.rsplit("_", 1)[-1]
        user["after_auth"] = "promo_book"
        await start_auth(user_id, context)
        return
    auth_required = (
        data in {"appointment", "my_visits", "visits_request", "booking_submit"}
        or data.startswith(("book_by_", "problem_", "doc_", "date_", "time_", "select_service_"))
        or re.fullmatch(r"svc_\d+", data) is not None
        or data in {"back_service", "back_date"}
    )
    if auth_required and not user.get("auth"):
        user["after_auth"] = "my_visits" if data in {"my_visits", "visits_request"} else "appointment"
        await start_auth(user_id, context)
        return

    if data == "webapp_help":
        await send(user_id, "Чтобы включить Mini App, укажите публичный HTTPS-адрес в переменной WEBAPP_URL и перезапустите бота.", context,
                   keyboard([[ ("⬅️ В главное меню", "back_menu") ]]))
        return
    if data in ("back_menu", "back_to_menu"):
        user["step"] = None; await send_main_menu(user_id, context); return
    if data == "appointment":
        await show_appointment_entry(user_id, context); return
    if data == "book_by_problem":
        await begin_booking(user_id, context, "problem"); return
    if data == "book_by_service":
        await begin_booking(user_id, context, "service"); return
    if data == "book_by_doctor":
        await begin_booking(user_id, context, "doctor"); return
    if data == "book_nearest":
        await begin_booking(user_id, context, "nearest"); return
    if data == "my_visits":
        await show_my_visits(user_id, context); return
    if data == "visits_request":
        result = create_support_task(user_id, "Запрос по визитам", "Пациент просит предоставить информацию о своих визитах.", 2)
        task_id = result.get("result", {}).get("task", {}).get("id")
        if task_id:
            await send(user_id, f"Запрос передан администратору. Номер задачи: #{esc(task_id)}.", context)
        else:
            delivered = await send_admin_fallback(context, "Запрос по визитам", user_id, f"Телефон: {user.get('phone')}")
            await send(user_id, "Запрос отправлен администраторам." if delivered else f"Не удалось подтвердить доставку. Позвоните {esc(CLINIC_PHONE)}.", context)
        return
    if data == "emergency":
        await show_emergency(user_id, context); return
    if data.startswith("emergency_"):
        try: idx = int(data.split("_", 1)[1])
        except ValueError: idx = -1
        problem = EMERGENCY_PROBLEMS[idx] if 0 <= idx < len(EMERGENCY_PROBLEMS) else "Экстренное обращение"
        result = create_support_task(user_id, f"ЭКСТРЕННОЕ обращение: {problem}",
            f"Проблема: {problem}\nВремя обращения: {datetime.now().isoformat(timespec='minutes')}", 1)
        task_id = result.get("result", {}).get("task", {}).get("id")
        if task_id:
            msg = f"Обращение «{esc(problem)}» передано администратору. Номер задачи: #{esc(task_id)}."
        else:
            delivered = await send_admin_fallback(context, f"ЭКСТРЕННОЕ обращение: {problem}", user_id,
                f"Телефон: {user.get('phone') or 'не указан'}\nВремя: {datetime.now():%d.%m.%Y %H:%M}")
            msg = "Обращение отправлено администраторам резервным способом." if delivered else f"Не удалось подтвердить доставку. Позвоните в клинику: {CLINIC_PHONE}."
        await send(user_id, f"🚨 {msg}\n\nПри критическом состоянии не ждите ответа в чате — позвоните в клинику или экстренную службу.", context,
            keyboard([[("⬅️ В главное меню", "back_menu")], [("📞 Позвонить в клинику", "clinic_phone")]]))
        return
    if data.startswith("doctors_"):
        try: page = int(data.split("_", 1)[1])
        except ValueError: page = 0
        await show_doctors(user_id, context, page, appointment_flow=False); return
    if data == "doctors":
        await show_doctors(user_id, context, 0); return
    if data == "services":
        await show_services(user_id, context); return
    if data == "promo":
        await show_promotions(user_id, context); return
    if data.startswith("promo_view_"):
        await show_promotion_detail(user_id, context, data.rsplit("_", 1)[-1]); return
    if data.startswith("promo_book_"):
        await start_promotion_booking(user_id, context, data.rsplit("_", 1)[-1]); return
    if data == "about":
        await show_about(user_id, context); return
    if data == "reviews":
        await show_reviews(user_id, context); return
    if data == "referral":
        await show_referral(user_id, context); return
    if data == "support":
        await begin_support(user_id, context); return
    if data == "open_map":
        await context.bot.send_message(user_id, "Карта клиники:", reply_markup=keyboard([[("Открыть Яндекс Карты", "unused", YANDEX_MAPS_URL)]])); return
    if data == "clinic_phone":
        await send(user_id, f"Телефон клиники: {esc(CLINIC_PHONE)}", context); return
    if data == "share_referral":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{user_id}" if me.username else ""
        await send(user_id, esc(link) if link else "Ссылка временно недоступна.", context); return
    if data == "restart_auth":
        user["auth"] = False; await start_auth(user_id, context); return

    # Booking flow
    if data.startswith("problem_"):
        idx = int(data.split("_", 1)[1]); user["appointment"]["problem"] = PROBLEMS[idx]
        await show_services(user_id, context, appointment_flow=True); return
    if data.startswith("svc_info_"):
        idx = int(data.rsplit("_", 1)[1]); service = SERVICES[idx]
        await send(user_id, f"<b>{esc(service)}</b>\n\nСтоимость, длительность и список врачей уточняются по актуальным данным клиники. Можно оставить заявку на запись.", context,
            keyboard([[("🦷 Записаться на услугу", f"select_service_{idx}")], [("⬅️ К услугам", "services")], [("🏠 В меню", "back_menu")]])); return
    if data.startswith("select_service_"):
        idx = int(data.rsplit("_", 1)[1])
        previous = user.get("appointment", {})
        carry = {k: previous.get(k) for k in ("promotion", "promotion_id") if previous.get(k)}
        user["appointment"] = {"service": SERVICES[idx], "mode": "service", **carry}
        await show_booking_doctor_step(user_id, context)
        return
    if data.startswith("svc_"):
        idx = int(data.split("_", 1)[1])
        user["appointment"]["service"] = SERVICES[idx]
        # В сценарии «к конкретному врачу» врач уже выбран — не спрашиваем его повторно.
        if user["appointment"].get("doctor"):
            await show_date_choices(user_id, context)
        else:
            await show_booking_doctor_step(user_id, context)
        return
    if data.startswith("doc_"):
        idx = int(data.split("_", 1)[1])
        if not 0 <= idx < len(DOCTORS):
            await send(user_id, "Не удалось выбрать врача. Попробуйте ещё раз.", context); return
        user["appointment"]["doctor"] = DOCTORS[idx]
        if not user["appointment"].get("service"):
            await show_services(user_id, context, appointment_flow=True)
        else:
            await show_date_choices(user_id, context)
        return
    if data.startswith("date_"):
        raw = data[5:]
        try: user["appointment"]["date"] = datetime.strptime(raw, "%Y%m%d").strftime("%d.%m.%Y")
        except ValueError:
            await send(user_id, "Некорректная дата. Выберите дату ещё раз.", context); return
        await show_time_choices(user_id, context); return
    if data.startswith("time_"):
        raw = data[5:]
        user["appointment"]["time"] = "Не важно" if raw == "any" else f"{raw[:2]}:{raw[2:]}"
        await show_booking_confirmation(user_id, context); return
    if data == "booking_submit":
        await submit_booking(user_id, context); return
    if data == "back_service":
        await show_services(user_id, context, appointment_flow=True); return
    if data == "back_date":
        await show_date_choices(user_id, context); return

    await send(user_id, "Не удалось распознать действие. Вернитесь в меню и попробуйте снова.", context,
        keyboard([[("🏠 В главное меню", "back_menu")]]))


def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("PASTE_NEW_"):
        raise RuntimeError("Укажите новый TELEGRAM_TOKEN в этом файле (или через переменную окружения). Старый токен был раскрыт и должен быть заменён.")
    if not BITRIX24_WEBHOOK:
        logger.warning("BITRIX24_WEBHOOK не настроен: заявки не смогут попасть в Bitrix24.")
    if not WEBAPP_URL.startswith("https://"):
        logger.warning("WEBAPP_URL не задан или не HTTPS: кнопка Mini App останется информационной.")
    if (not TELEGRAM_PROXY.startswith(("socks5://", "socks5h://"))
            or "LOGIN:PASSWORD@IP:PORT" in TELEGRAM_PROXY
            or "@IP:PORT" in TELEGRAM_PROXY):
        raise RuntimeError("Укажите новые действующие SOCKS5-данные в TELEGRAM_PROXY перед запуском.")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .proxy(TELEGRAM_PROXY)
        .get_updates_proxy(TELEGRAM_PROXY)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    logger.info("Запуск Telegram-бота АльфаДент")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
