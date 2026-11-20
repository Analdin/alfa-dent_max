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


# ---------------- PRIVATE PYTHON CONFIGURATION ----------------
# alfadent_config.py is deployed from the private config repository.
import alfadent_config as config

MAX_API_BASE = config.MAX_API_BASE
MAX_API = MAX_API_BASE
TOKEN = config.MAX_BOT_TOKEN.strip()
MAX_BOT_TOKEN = TOKEN
BITRIX24_WEBHOOK = config.BITRIX24_WEBHOOK
BITRIX_ASSIGNED_BY_ID = int(config.BITRIX_ASSIGNED_BY_ID)
BITRIX_OBSERVER_IDS = []
MAX_ADMIN_IDS = [15, 33, 35, 37, 39, 727, 7821]
ADMIN_IDS = MAX_ADMIN_IDS
# В исходнике соответствие MAX ID -> Bitrix ID не задавалось.
MAX_TO_BITRIX_ADMIN_MAP = {}

AUTH_API_URL = config.AUTH_API_URL.rstrip("/")
AUTH_API_SECRET = config.INTERNAL_AUTH_SECRET
MAX_MINIAPP_LINK = config.MAX_MINIAPP_LINK.strip()
MAX_BOT_USERNAME = ""
MAX_BOT_CONTACT_ID = None
MAX_WEBHOOK_SECRET = config.MAX_WEBHOOK_SECRET.strip()
MAX_REQUIRE_WEBHOOK_SECRET = bool(config.MAX_REQUIRE_WEBHOOK_SECRET)

CLINIC_PHONE = "+7 (988) 233-77-25"
CLINIC_ADDRESS = "г. Сочи, ул. Северная, 12, корп. 2, 2 этаж. Ориентир: с торца здания, рядом с магазином «Коляски»."
CLINIC_HOURS = "Ежедневно, 09:00–20:00"
YANDEX_MAPS_URL = "https://yandex.ru/medicine/clinic/alfa_dent_1617563656"
PROMOTIONS_URL = config.PROMOTIONS_URL.strip()
PROMOTIONS_FILE = BASE_DIR / "web" / "assets" / "promotions.json"
CLINIC_TIMEZONE_NAME = "Europe/Moscow"
try:
    CLINIC_TZ = ZoneInfo(CLINIC_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    CLINIC_TZ = ZoneInfo("Europe/Moscow")
    CLINIC_TIMEZONE_NAME = "Europe/Moscow"

DATA_DIR = Path(config.MAX_DATA_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
if os.name == "posix":
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass
PENDING_FILE = DATA_DIR / "pending_requests.json"  # legacy-compatible emergency queue
DATABASE_FILE = DATA_DIR / "max_bot.sqlite3"

LOG_LEVEL = config.LOG_LEVEL.upper()
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
