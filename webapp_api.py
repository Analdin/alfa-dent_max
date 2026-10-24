#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP API for the existing АльфаДент Mini App, shared by Telegram and MAX.

Run behind HTTPS/reverse proxy. Both platform launch signatures are validated
before any CRM write. Requests create CRM leads, not real MIS appointments.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
import bot_telegram as bot

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("alfadent_webapp_api")
WEB_DIR = Path(__file__).resolve().parent / "web"
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", getattr(bot, "TELEGRAM_TOKEN", "")).strip()
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
INITDATA_MAX_AGE_SECONDS = int(os.getenv("INITDATA_MAX_AGE_SECONDS", "3600"))

app = FastAPI(title="АльфаДент Mini App API", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

class RequestBody(BaseModel):
    platform: str = Field(default="telegram", max_length=20)
    initData: str = Field(default="", max_length=10000)
    payload: dict[str, Any]


def validated_telegram_user(init_data: str) -> dict[str, Any]:
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_NEW_"):
        raise HTTPException(status_code=503, detail="API не настроен: укажите новый TELEGRAM_TOKEN в конфигурации бота.")
    if not init_data:
        raise HTTPException(status_code=401, detail="Откройте Mini App из Telegram, чтобы отправить запрос.")
    try:
        parts = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise HTTPException(status_code=401, detail="Некорректные данные запуска Telegram.")
    received_hash = parts.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="В данных запуска отсутствует подпись Telegram.")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(parts.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Не удалось проверить подпись Telegram.")
    try:
        user = json.loads(parts.get("user", "{}"))
        auth_date = int(parts.get("auth_date", "0"))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Некорректные данные пользователя Telegram.")
    now = int(time.time())
    if not auth_date or auth_date > now + 60 or now - auth_date > INITDATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Данные запуска устарели. Закройте и снова откройте Mini App.")
    if not isinstance(user, dict) or not str(user.get("id", "")).isdigit():
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя Telegram.")
    return user


def _parse_max_init_data(raw_init_data: str) -> dict[str, str]:
    """Parse MAX WebAppData or window.WebApp.initData, rejecting duplicate keys.

    MAX documents that WebAppData may be wrapped in the URL fragment. When the
    Bridge has already extracted the inner string, that form is accepted too.
    """
    raw = (raw_init_data or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="Откройте Mini App из MAX, чтобы отправить заявку.")
    if "#" in raw and (raw.startswith("http://") or raw.startswith("https://")):
        raw = raw.split("#", 1)[1]
    raw = raw.lstrip("#")

    # If the caller provided the entire hash fragment, extract WebAppData once.
    outer_pairs: list[tuple[str, str]] = []
    for item in raw.split("&"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        outer_pairs.append((unquote(key), value))
    wrappers = [value for key, value in outer_pairs if key == "WebAppData"]
    if wrappers:
        if len(wrappers) != 1:
            raise HTTPException(status_code=401, detail="В данных запуска MAX дублируется WebAppData.")
        raw = unquote(wrappers[0])

    parts: dict[str, str] = {}
    for item in raw.split("&"):
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = unquote(key)
        value = unquote(value)
        if not key or key in parts:
            raise HTTPException(status_code=401, detail="В данных запуска MAX есть повторяющиеся параметры.")
        parts[key] = value
    if not parts.get("hash"):
        raise HTTPException(status_code=401, detail="В данных запуска отсутствует подпись MAX.")
    return parts


def validated_max_user(init_data: str) -> dict[str, Any]:
    if not MAX_BOT_TOKEN or MAX_BOT_TOKEN.startswith("PASTE_NEW_"):
        raise HTTPException(status_code=503, detail="API не настроен: укажите новый MAX_BOT_TOKEN в конфигурации сервера.")
    parts = _parse_max_init_data(init_data)
    received_hash = parts.pop("hash", "")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(parts.items()))
    secret_key = hmac.new(b"WebAppData", MAX_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Не удалось проверить подпись MAX. Откройте приложение через кнопку бота в MAX.")
    try:
        user = json.loads(parts.get("user", "{}"))
        auth_date = int(parts.get("auth_date", "0"))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Некорректные данные пользователя MAX.")
    now = int(time.time())
    if not auth_date or auth_date > now + 60 or now - auth_date > INITDATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Данные запуска MAX устарели. Закройте и снова откройте Mini App.")
    if not isinstance(user, dict) or not str(user.get("id", "")).isdigit():
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя MAX.")
    return user


def validated_user(body: RequestBody) -> tuple[dict[str, Any], str]:
    platform = (body.platform or "telegram").strip().lower()
    if platform == "max":
        return validated_max_user(body.initData), "MAX"
    if platform == "telegram":
        return validated_telegram_user(body.initData), "Telegram"
    raise HTTPException(status_code=400, detail="Неизвестная платформа запуска Mini App.")


def clean(value: Any, limit: int = 300) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]


def normalize_payload_phone(raw: str) -> str:
    normalized = bot.normalize_phone(raw)
    if not normalized:
        raise HTTPException(status_code=422, detail="Укажите корректный российский номер телефона.")
    return "+7" + normalized


def write_general_lead(user_id: int, name: str, phone: str, title: str, comments: str) -> dict[str, Any]:
    if not bot.BITRIX24_WEBHOOK:
        bot.save_pending_request({"type": title, "user_id": user_id, "description": comments, "phone": phone})
        raise HTTPException(status_code=503, detail="Интеграция с CRM не настроена. Обращение сохранено локально, но не передано администратору.")
    result = bot.bitrix_call("crm.item.add", {"entityTypeId": 1, "fields": {
        "title": title[:255], "name": name or "Пациент Mini App", "comments": comments,
        "sourceId": "WEB", "assignedById": int(os.getenv("BITRIX_ASSIGNED_BY_ID", "33")),
        "OPENED": "Y", "observers": getattr(bot, "BITRIX_ADMIN_IDS", []),
    }})
    lead_id = result.get("result", {}).get("item", {}).get("id")
    if not lead_id:
        bot.save_pending_request({"type": title, "user_id": user_id, "description": comments, "phone": phone, "bitrix_error": result.get("error", "unknown")})
        raise HTTPException(status_code=502, detail="CRM не подтвердила создание обращения. Данные сохранены в резервной очереди; пожалуйста, позвоните в клинику.")
    if phone:
        bot.bitrix_call("crm.item.update", {"entityTypeId": 1, "id": lead_id, "fields": {
            "fm": [{"typeId": "PHONE", "valueType": "WORK", "value": phone}]
        }})
    return {"lead_id": str(lead_id)}


@app.get("/")
def home():
    return FileResponse(WEB_DIR / "miniapp.html")

@app.get("/miniapp.html")
def miniapp_page():
    return FileResponse(WEB_DIR / "miniapp.html")

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "alfadent-miniapp-api",
        "crm_configured": bool(bot.BITRIX24_WEBHOOK),
        "telegram_validation_configured": bool(BOT_TOKEN and not BOT_TOKEN.startswith("PASTE_NEW_")),
        "max_validation_configured": bool(MAX_BOT_TOKEN and not MAX_BOT_TOKEN.startswith("PASTE_NEW_")),
    }

@app.post("/api/appointment")
def create_appointment(body: RequestBody):
    platform_user, channel = validated_user(body)
    user_id = int(platform_user["id"])
    # Keep Telegram and MAX sessions separate if their numeric user IDs ever overlap.
    session_user_id = user_id if channel == "Telegram" else -user_id
    p = body.payload
    name = clean(p.get("name"), 100) or clean(platform_user.get("first_name"), 100) or "Пациент"
    phone = normalize_payload_phone(clean(p.get("phone"), 40))
    appointment = {
        "problem": clean(p.get("problem"), 200),
        "service": clean(p.get("service"), 200),
        "doctor": clean(p.get("doctor"), 200),
        "date": clean(p.get("date"), 40),
        "time": clean(p.get("time"), 40),
        "promotion": clean(p.get("promotion"), 250),
        "mode": "Mini App",
    }
    if not any(appointment.get(k) for k in ("problem", "service", "doctor")):
        raise HTTPException(status_code=422, detail="Выберите проблему, услугу или врача.")
    session = bot.session_for(session_user_id, name)
    session["name"] = name
    session["phone"] = phone
    result = bot.create_appointment_lead(user_id, appointment, channel=channel, session_user_id=session_user_id)
    lead_id = result.get("result", {}).get("item", {}).get("id")
    if not lead_id:
        raise HTTPException(status_code=502, detail="CRM не подтвердила создание заявки. Данные сохранены в резервной очереди, но запись не создана; свяжитесь с клиникой по телефону " + bot.CLINIC_PHONE + ".")
    return {"ok": True, "lead_id": str(lead_id), "message": "Заявка принята. Администратор свяжется с вами для подтверждения времени. Это ещё не подтверждённая запись."}

@app.post("/api/support")
def create_support(body: RequestBody):
    platform_user, channel = validated_user(body)
    user_id = int(platform_user["id"])
    p = body.payload
    name = clean(p.get("name"), 100) or clean(platform_user.get("first_name"), 100) or "Пациент"
    phone = normalize_payload_phone(clean(p.get("phone"), 40)) if clean(p.get("phone"), 40) else ""
    message = clean(p.get("message"), 2000)
    urgent = message.lower().startswith("срочное обращение") or "экстренно" in message.lower()
    if len(message) < 3:
        raise HTTPException(status_code=422, detail="Напишите вопрос администратору.")
    if urgent and not phone:
        raise HTTPException(status_code=422, detail="Для экстренного обращения укажите телефон для связи.")
    session_user_id = user_id if channel == "Telegram" else -user_id
    session = bot.session_for(session_user_id, name)
    session["name"] = name
    session["phone"] = phone
    title = (f"[{channel} Mini App] ЭКСТРЕННОЕ обращение" if urgent else f"[{channel} Mini App] Вопрос пациента")
    comments = (f"Пациент: {name}\nТелефон: {phone or 'не указан'}\nID пользователя {channel}: {user_id}\n"
                f"Канал: {channel} Mini App\nВремя обращения: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n{message}")
    result = write_general_lead(user_id, name, phone, title, comments)
    return {"ok": True, "lead_id": result["lead_id"], "message": "Экстренное обращение передано в CRM." if urgent else "Обращение передано в CRM. Администратор свяжется с вами."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("API_HOST", "127.0.0.1"), port=int(os.getenv("API_PORT", "8001")), log_level=os.getenv("LOG_LEVEL", "info").lower())
