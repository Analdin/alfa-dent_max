#!/usr/bin/env bash
set -Eeuo pipefail

# AlfaDent: install auto-restarting systemd services for the existing Telegram bot,
# MAX bot, and Cloudflare Quick Tunnel. Does not create .env files or rewrite bot secrets.

TELEGRAM_DIR="/opt/alfadent_repo"
TELEGRAM_SCRIPT="$TELEGRAM_DIR/bot_telegram.py"
TELEGRAM_PY="$TELEGRAM_DIR/.venv/bin/python"
MAX_DIR="/opt/alfa-dent_max/alfa-dent_max"
MAX_SCRIPT="$MAX_DIR/max_bot/max_bot.py"
MAX_PY="$MAX_DIR/.venv/bin/python"
MAX_MANAGER="/usr/local/sbin/alfadent-max-tunnel-supervisor.py"
CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"

log() { printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "Run as root: sudo bash setup_alfadent_services.sh"
command -v systemctl >/dev/null || die "systemd is not available on this server."
command -v cloudflared >/dev/null || die "cloudflared was not found. Install it first."

# Validate everything first, before stopping any running bot.
[[ -f "$TELEGRAM_SCRIPT" ]] || die "Telegram script not found: $TELEGRAM_SCRIPT"
[[ -x "$TELEGRAM_PY" ]] || die "Telegram virtualenv Python not found: $TELEGRAM_PY"
[[ -f "$MAX_SCRIPT" ]] || die "MAX script not found: $MAX_SCRIPT"
[[ -x "$MAX_PY" ]] || die "MAX virtualenv Python not found: $MAX_PY"
[[ -f "$CA_BUNDLE" ]] || die "System CA bundle missing: $CA_BUNDLE"

log "Checking Python syntax"
"$TELEGRAM_PY" -m py_compile "$TELEGRAM_SCRIPT"
"$MAX_PY" -m py_compile "$MAX_SCRIPT"

log "Checking MAX token is readable without displaying it"
cd "$MAX_DIR"
"$MAX_PY" - <<'PY'
from max_bot.max_bot import MAX_API_BASE, MAX_BOT_TOKEN
if not isinstance(MAX_API_BASE, str) or not MAX_API_BASE.startswith("https://"):
    raise SystemExit("MAX_API_BASE is not a valid HTTPS URL")
if not isinstance(MAX_BOT_TOKEN, str) or len(MAX_BOT_TOKEN.strip()) < 20:
    raise SystemExit("MAX_BOT_TOKEN is missing or too short in max_bot/max_bot.py")
print("MAX API/token configuration found (value hidden).")
PY

# Write a supervisor which launches the Quick Tunnel and renews the MAX webhook
# subscription whenever Cloudflare gives the tunnel a new temporary URL.
install -d -m 0755 /usr/local/sbin
cat > "$MAX_MANAGER" <<'PY'
#!/opt/alfa-dent_max/alfa-dent_max/.venv/bin/python
from __future__ import annotations
import ast
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import requests

MAX_DIR = Path("/opt/alfa-dent_max/alfa-dent_max")
BOT_FILE = MAX_DIR / "max_bot" / "max_bot.py"
TUNNEL_TARGET = "http://127.0.0.1:5000"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)
stop_event = threading.Event()
state_lock = threading.Lock()
current_url: str | None = None
registered_url: str | None = None
registered_base_url: str | None = None


def log(message: str) -> None:
    print(f"[alfadent-cloudflared] {message}", flush=True)


def load_constant_assignments() -> dict[str, ast.AST]:
    tree = ast.parse(BOT_FILE.read_text(encoding="utf-8"), filename=str(BOT_FILE))
    result: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            result[node.target.id] = node.value
    return result


def resolve_value(name: str, assignments: dict[str, ast.AST], seen: set[str] | None = None):
    seen = set() if seen is None else seen
    if name in seen or name not in assignments:
        return None
    seen.add(name)
    node = assignments[name]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return resolve_value(node.id, assignments, seen)
    if isinstance(node, ast.Call):
        # Support simple os.getenv("NAME", "hard-coded fallback") without creating .env files.
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "getenv" and node.args:
            env_name = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
            if isinstance(env_name, str) and os.getenv(env_name):
                return os.environ[env_name]
            if len(node.args) > 1:
                default = node.args[1]
                if isinstance(default, ast.Constant):
                    return default.value
                if isinstance(default, ast.Name):
                    return resolve_value(default.id, assignments, seen)
    return None


assignments = load_constant_assignments()
API_BASE = resolve_value("MAX_API_BASE", assignments) or "https://platform-api2.max.ru"
TOKEN = resolve_value("MAX_BOT_TOKEN", assignments) or resolve_value("TOKEN", assignments)
WEBHOOK_SECRET = resolve_value("MAX_WEBHOOK_SECRET", assignments) or ""
if not isinstance(TOKEN, str) or len(TOKEN.strip()) < 20:
    log("ERROR: Could not read MAX_BOT_TOKEN from max_bot/max_bot.py. Tunnel will stay up, but webhook registration cannot proceed.")
    TOKEN = ""
API_BASE = str(API_BASE).rstrip("/")
WEBHOOK_PATH = "/webhook" + (f"/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "")
HEADERS = {"Authorization": TOKEN, "Content-Type": "application/json"}


def register_subscription(public_base: str) -> bool:
    global registered_url, registered_base_url
    if not TOKEN:
        return False
    webhook_url = public_base.rstrip("/") + WEBHOOK_PATH
    payload = {"url": webhook_url, "update_types": ["message_created", "bot_started", "message_callback"]}
    try:
        response = requests.post(f"{API_BASE}/subscriptions", headers=HEADERS, json=payload, timeout=20)
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code != 200 or data.get("success") is not True:
            # Do not log the token or request headers.
            msg = data.get("message", "") if isinstance(data, dict) else ""
            log(f"Webhook registration failed: HTTP {response.status_code}; {str(msg)[:200]}")
            return False
        log(f"MAX webhook registered for the current Quick Tunnel (HTTP {response.status_code}).")
        registered_url = webhook_url
        registered_base_url = public_base.rstrip("/")
        cleanup_stale_quick_tunnels(webhook_url)
        return True
    except requests.RequestException as exc:
        log(f"MAX webhook registration request failed: {type(exc).__name__}: {exc}")
        return False


def cleanup_stale_quick_tunnels(current_webhook: str) -> None:
    # Remove only old Cloudflare Quick Tunnel subscriptions. Preserve any other integrations.
    try:
        response = requests.get(f"{API_BASE}/subscriptions", headers=HEADERS, timeout=15)
        if response.status_code != 200:
            log(f"Could not list subscriptions for stale-tunnel cleanup (HTTP {response.status_code}).")
            return
        data = response.json()
        items = data.get("subscriptions", []) if isinstance(data, dict) else []
        for item in items:
            old_url = item.get("url", "") if isinstance(item, dict) else ""
            if old_url and ".trycloudflare.com" in old_url and old_url != current_webhook:
                deleted = requests.delete(f"{API_BASE}/subscriptions", headers=HEADERS, params={"url": old_url}, timeout=15)
                log(f"Removed stale Quick Tunnel subscription (HTTP {deleted.status_code}).")
    except (requests.RequestException, ValueError) as exc:
        log(f"Could not clean stale Quick Tunnel subscriptions: {type(exc).__name__}")


def registration_worker() -> None:
    global registered_url, registered_base_url
    while not stop_event.is_set():
        with state_lock:
            url = current_url
        if url and url != registered_base_url:
            register_subscription(url)
        stop_event.wait(20 if url != registered_base_url else 60)


def wait_for_local_bot() -> None:
    deadline = time.time() + 120
    while time.time() < deadline and not stop_event.is_set():
        try:
            with socket.create_connection(("127.0.0.1", 5000), timeout=2):
                log("MAX Flask listener is reachable on 127.0.0.1:5000.")
                return
        except OSError:
            time.sleep(2)
    log("WARNING: MAX listener on port 5000 was not confirmed after 120 seconds; continuing anyway.")


def handle_signal(signum, _frame) -> None:
    stop_event.set()


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

cloudflared = shutil.which("cloudflared")
if not cloudflared:
    raise SystemExit("cloudflared executable is not in PATH")
wait_for_local_bot()
threading.Thread(target=registration_worker, daemon=True).start()
command = [cloudflared, "tunnel", "--no-autoupdate", "--url", TUNNEL_TARGET]
log("Starting Cloudflare Quick Tunnel to http://127.0.0.1:5000.")
process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
try:
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="" if line.endswith("\n") else "\n", flush=True)
        match = URL_RE.search(line)
        if match:
            url = match.group(0)
            with state_lock:
                if url != current_url:
                    current_url = url
                    log(f"Public Quick Tunnel URL detected: {url}")
                    registered_base_url = None
    if not stop_event.is_set():
        raise SystemExit(process.wait() or 1)
finally:
    stop_event.set()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
PY
chmod 0755 "$MAX_MANAGER"

cat > /etc/systemd/system/alfadent-telegram.service <<EOF
[Unit]
Description=AlfaDent Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$TELEGRAM_DIR
Environment=PYTHONUNBUFFERED=1
Environment=REQUESTS_CA_BUNDLE=$CA_BUNDLE
ExecStart=$TELEGRAM_PY -u $TELEGRAM_SCRIPT
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30
StandardOutput=append:$TELEGRAM_DIR/bot_telegram.log
StandardError=append:$TELEGRAM_DIR/bot_telegram.log

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/alfadent-max.service <<EOF
[Unit]
Description=AlfaDent MAX Bot Webhook Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$MAX_DIR
Environment=PYTHONUNBUFFERED=1
Environment=REQUESTS_CA_BUNDLE=$CA_BUNDLE
ExecStart=$MAX_PY -u $MAX_SCRIPT
Restart=always
RestartSec=5
TimeoutStopSec=30
StandardOutput=append:$MAX_DIR/max_bot.log
StandardError=append:$MAX_DIR/max_bot.log

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/alfadent-max-tunnel.service <<EOF
[Unit]
Description=AlfaDent Cloudflare Quick Tunnel and MAX webhook subscription manager
After=network-online.target alfadent-max.service
Wants=network-online.target
Requires=alfadent-max.service

[Service]
Type=simple
WorkingDirectory=$MAX_DIR
Environment=PYTHONUNBUFFERED=1
Environment=REQUESTS_CA_BUNDLE=$CA_BUNDLE
ExecStart=$MAX_PY -u $MAX_MANAGER
Restart=always
RestartSec=5
KillMode=control-group
StandardOutput=append:$MAX_DIR/cloudflared.log
StandardError=append:$MAX_DIR/cloudflared.log

[Install]
WantedBy=multi-user.target
EOF

log "Stopping older systemd units if present"
for unit in alfadent-telegram.service alfadent-max.service alfadent-max-tunnel.service; do
  systemctl stop "$unit" 2>/dev/null || true
done

log "Stopping old nohup instances of these exact applications, if present"
python3 - <<'PY'
import os, signal, time
rules = [
    ("telegram", "/opt/alfadent_repo", "bot_telegram.py"),
    ("max", "/opt/alfa-dent_max/alfa-dent_max", "max_bot/max_bot.py"),
]
target_pids = []
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    pid = int(entry)
    if pid == os.getpid():
        continue
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", "ignore")
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except (OSError, PermissionError):
        continue
    for label, expected_cwd, marker in rules:
        if cwd == expected_cwd and marker in cmd and "python" in cmd:
            target_pids.append((pid, label))
            break
    if "cloudflared" in cmd and "tunnel" in cmd and "127.0.0.1:5000" in cmd:
        target_pids.append((pid, "cloudflared"))
for pid, label in target_pids:
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped old {label} process PID {pid}.")
    except ProcessLookupError:
        pass
time.sleep(2)
for pid, label in target_pids:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        continue
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"Force-stopped old {label} process PID {pid} after timeout.")
    except ProcessLookupError:
        pass
PY

systemctl daemon-reload
log "Enabling and starting Telegram bot"
systemctl enable alfadent-telegram.service >/dev/null
if ! systemctl restart alfadent-telegram.service; then
  journalctl -u alfadent-telegram.service -n 40 --no-pager || true
  die "Telegram service failed to start. See journalctl -u alfadent-telegram."
fi

log "Enabling and starting MAX bot"
systemctl enable alfadent-max.service >/dev/null
if ! systemctl restart alfadent-max.service; then
  journalctl -u alfadent-max.service -n 40 --no-pager || true
  die "MAX service failed to start. See journalctl -u alfadent-max."
fi

log "Enabling Cloudflare tunnel manager (will obtain URL and register webhook automatically)"
systemctl enable alfadent-max-tunnel.service >/dev/null
if ! systemctl restart alfadent-max-tunnel.service; then
  journalctl -u alfadent-max-tunnel.service -n 60 --no-pager || true
  die "Cloudflare tunnel service failed to start. See journalctl -u alfadent-max-tunnel."
fi

sleep 4
log "Service status"
systemctl --no-pager --full status alfadent-telegram.service alfadent-max.service alfadent-max-tunnel.service || true

log "MAX local health (does not test the external MAX chat end-to-end)"
if command -v curl >/dev/null; then
  curl --max-time 5 -fsS http://127.0.0.1:5000/health || true
  printf '\n'
fi

log "Latest tunnel output"
tail -n 8 "$MAX_DIR/cloudflared.log" 2>/dev/null || true

log "Setup complete. Services are enabled at boot. Use: systemctl status alfadent-telegram alfadent-max alfadent-max-tunnel"
log "No .env files were created and no bot source/configuration secrets were changed."
log "If /health reports bitrix_configured=false, the current MAX source still lacks a usable Bitrix webhook; this script deliberately does not invent or replace that secret."
