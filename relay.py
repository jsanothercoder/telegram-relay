from flask import Flask, request, jsonify
import requests
import os
import threading
import time
import re
import atexit
import asyncio
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

app = Flask(__name__)

tg_to_mc = []
lock = threading.Lock()

TOKEN        = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID      = str(os.environ.get("CHAT_ID", "")).strip().rstrip("L")
SECRET_KEY   = os.environ.get("SECRET_KEY", "change_this_secret")
CONTROL_URL  = os.environ.get("CONTROL_URL", "").rstrip("/")
CONTROL_KEY  = os.environ.get("CONTROL_KEY", "")

# Telethon (MTProto) — получи на my.telegram.org
API_ID       = int(os.environ.get("TG_API_ID", "0"))
API_HASH     = os.environ.get("TG_API_HASH", "")

# Папка для хранения сессий пользователей
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "./tg_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")

# ── Хранилище сессий авторизации ─────────────────────────────────────────────
# player_id → { "client": TelegramClient, "phone": str,
#               "phone_code_hash": str, "loop": asyncio.Loop,
#               "state": "wait_code" | "wait_password" | "authorized" }
auth_sessions = {}
auth_lock = threading.Lock()

# ── Whitelist паттерны ────────────────────────────────────────────────────────
WL_ADD_RE    = re.compile(r"^[!/]?whitelist\s+add\s+(\S+)$",    re.IGNORECASE)
WL_REMOVE_RE = re.compile(r"^[!/]?whitelist\s+remove\s+(\S+)$", re.IGNORECASE)

poller_thread  = None
poller_started = False
poller_stop    = threading.Event()


# ── Telegram Bot API helpers ──────────────────────────────────────────────────

def disable_webhook():
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=15
        )
        print("deleteWebhook:", r.status_code, r.text)
    except Exception as e:
        print("deleteWebhook error:", e)


def send_tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print("send_tg error:", e)


def handle_whitelist(action: str, name: str) -> str:
    if not CONTROL_URL or not CONTROL_KEY:
        return "⛔ CONTROL_URL или CONTROL_KEY не заданы в env"
    try:
        r = requests.get(
            f"{CONTROL_URL}/api/whitelist/{action}",
            params={"name": name},
            headers={"X-Api-Key": CONTROL_KEY},
            timeout=10
        )
        data = r.json()
        return data.get("message", str(data))
    except Exception as e:
        return f"⛔ Ошибка связи с сервером: {e}"


# ── Polling бота ──────────────────────────────────────────────────────────────

def poll_telegram():
    offset = 0
    print(f"Telegram polling started (pid={os.getpid()})")
    while not poller_stop.is_set():
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message", "edited_message",
                                        "channel_post", "edited_channel_post"]
                },
                timeout=35
            )
            data = r.json()
            if not data.get("ok"):
                print("getUpdates not ok:", data)
                time.sleep(2)
                continue

            for upd in data.get("result", []):
                msg = upd.get("message") or upd.get("channel_post")
                if msg and "text" in msg:
                    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
                    if chat_id == CHAT_ID:
                        frm  = msg.get("from", {}) or {}
                        name = frm.get("username") or frm.get("first_name") or "TGUser"
                        text = msg.get("text", "")

                        m_add = WL_ADD_RE.match(text)
                        m_rem = WL_REMOVE_RE.match(text)

                        if m_add:
                            player = m_add.group(1)
                            result = handle_whitelist("add", player)
                            send_tg(result)
                        elif m_rem:
                            player = m_rem.group(1)
                            result = handle_whitelist("remove", player)
                            send_tg(result)
                        else:
                            with lock:
                                tg_to_mc.append({"player": name, "message": text})
                            print(f"Queued TG->MC: {name}: {text}")
                    else:
                        print(f"Skipped chat_id={chat_id}")

                offset = max(offset, upd["update_id"] + 1)

        except Exception as e:
            print("Poll error:", e)
            time.sleep(3)


def ensure_single_poller():
    global poller_thread, poller_started
    if poller_started:
        return
    poller_started = True
    disable_webhook()
    poller_thread = threading.Thread(target=poll_telegram, daemon=True, name="tg-poller")
    poller_thread.start()


@atexit.register
def _shutdown():
    poller_stop.set()
    # Закрываем все Telethon-сессии
    with auth_lock:
        for pid, sess in auth_sessions.items():
            try:
                client = sess.get("client")
                loop = sess.get("loop")
                if client and loop:
                    asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
            except Exception:
                pass


# ── Telethon helpers ──────────────────────────────────────────────────────────

def get_or_create_loop(player_id: str):
    """Каждый игрок получает свой event loop в отдельном потоке."""
    with auth_lock:
        sess = auth_sessions.get(player_id, {})
        if "loop" in sess:
            return sess["loop"]

        loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True, name=f"loop-{player_id}")
        t.start()

        if player_id not in auth_sessions:
            auth_sessions[player_id] = {}
        auth_sessions[player_id]["loop"] = loop
        return loop


def run_async(player_id: str, coro):
    """Запустить корутину в loop игрока и дождаться результата."""
    loop = get_or_create_loop(player_id)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


# ── Эндпоинты авторизации ─────────────────────────────────────────────────────

@app.route("/auth/start", methods=["POST"])
def auth_start():
    """
    Шаг 1: игрок вводит /tgauth number +79991234567
    Тело: { "player_id": "uuid-игрока", "phone": "+79991234567" }
    """
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id", "").strip()
    phone = data.get("phone", "").strip()

    if not player_id or not phone:
        return jsonify({"error": "player_id and phone required"}), 400
    if not API_ID or not API_HASH:
        return jsonify({"error": "TG_API_ID / TG_API_HASH не заданы на сервере"}), 500

    # Если уже авторизован — отвечаем сразу
    with auth_lock:
        sess = auth_sessions.get(player_id, {})
        if sess.get("state") == "authorized":
            return jsonify({"status": "already_authorized"})

    try:
        session_path = os.path.join(SESSIONS_DIR, player_id)
        loop = get_or_create_loop(player_id)
        client = TelegramClient(session_path, API_ID, API_HASH, loop=loop)

        async def do_start():
            await client.connect()
            result = await client.send_code_request(phone)
            return result.phone_code_hash

        phone_code_hash = run_async(player_id, do_start())

        with auth_lock:
            auth_sessions[player_id] = {
                "client": client,
                "phone": phone,
                "phone_code_hash": phone_code_hash,
                "state": "wait_code",
                "loop": loop,
            }

        print(f"Auth started for player {player_id}, phone {phone}")
        return jsonify({"status": "code_sent"})

    except Exception as e:
        print(f"auth_start error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/auth/code", methods=["POST"])
def auth_code():
    """
    Шаг 2: игрок вводит /tgauth code 123456
    Тело: { "player_id": "uuid", "code": "123456" }
    """
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id", "").strip()
    code = data.get("code", "").strip()

    if not player_id or not code:
        return jsonify({"error": "player_id and code required"}), 400

    with auth_lock:
        sess = auth_sessions.get(player_id)

    if not sess:
        return jsonify({"error": "no_session", "hint": "Сначала /tgauth number"}), 400

    state = sess.get("state")

    if state == "authorized":
        return jsonify({"status": "already_authorized"})

    if state == "wait_code":
        try:
            async def do_sign_in():
                return await sess["client"].sign_in(
                    phone=sess["phone"],
                    code=code,
                    phone_code_hash=sess["phone_code_hash"]
                )

            run_async(player_id, do_sign_in())
            with auth_lock:
                auth_sessions[player_id]["state"] = "authorized"
            print(f"Player {player_id} authorized via code")
            return jsonify({"status": "authorized"})

        except SessionPasswordNeededError:
            with auth_lock:
                auth_sessions[player_id]["state"] = "wait_password"
            return jsonify({"status": "need_password"})

        except PhoneCodeInvalidError:
            return jsonify({"error": "invalid_code"}), 400

        except Exception as e:
            print(f"auth_code error: {e}")
            return jsonify({"error": str(e)}), 500

    if state == "wait_password":
        # code здесь — это 2FA пароль
        try:
            async def do_password():
                return await sess["client"].sign_in(password=code)

            run_async(player_id, do_password())
            with auth_lock:
                auth_sessions[player_id]["state"] = "authorized"
            print(f"Player {player_id} authorized via 2FA")
            return jsonify({"status": "authorized"})

        except Exception as e:
            print(f"auth_password error: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "unexpected_state", "state": state}), 400


@app.route("/auth/status", methods=["GET"])
def auth_status():
    """Проверить статус авторизации игрока."""
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    player_id = request.args.get("player_id", "").strip()
    if not player_id:
        return jsonify({"error": "player_id required"}), 400

    with auth_lock:
        sess = auth_sessions.get(player_id)

    state = sess.get("state", "none") if sess else "none"
    return jsonify({"player_id": player_id, "state": state})


@app.route("/to-tg-user", methods=["POST"])
def to_tg_user():
    """
    Отправить сообщение от имени авторизованного пользователя.
    Тело: { "player_id": "uuid", "message": "текст" }
    """
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id", "").strip()
    message = data.get("message", "").strip()

    if not player_id or not message:
        return jsonify({"error": "player_id and message required"}), 400

    with auth_lock:
        sess = auth_sessions.get(player_id)

    if not sess or sess.get("state") != "authorized":
        return jsonify({"error": "not_authorized"}), 403

    try:
        chat_id_int = int(CHAT_ID)

        async def do_send():
            await sess["client"].send_message(chat_id_int, message)

        run_async(player_id, do_send())
        return jsonify({"ok": True})

    except Exception as e:
        print(f"to-tg-user error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Существующие эндпоинты ────────────────────────────────────────────────────

@app.route("/to-tg", methods=["POST"])
def to_tg():
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "player" not in data or "message" not in data:
        return jsonify({"error": "invalid payload"}), 400

    text = f"*{data['player']}*: {data['message']}"
    try:
        rr = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=20
        )
        if rr.status_code != 200:
            return jsonify({"error": "telegram send failed", "body": rr.text}), 502
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/from-tg", methods=["GET"])
def from_tg():
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    with lock:
        out = tg_to_mc.copy()
        tg_to_mc.clear()
    return jsonify(out)


@app.route("/health", methods=["GET"])
def health():
    with auth_lock:
        authorized_count = sum(
            1 for s in auth_sessions.values() if s.get("state") == "authorized"
        )
    return jsonify({
        "status": "ok",
        "pid": os.getpid(),
        "authorized_users": authorized_count
    })


@app.before_request
def _boot_once():
    ensure_single_poller()


if __name__ == "__main__":
    ensure_single_poller()
    app.run(host="0.0.0.0", port=5000)
