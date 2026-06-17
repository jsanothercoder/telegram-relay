from flask import Flask, request, jsonify
import requests
import os
import threading
import time
import re
import atexit
import asyncio
from datetime import datetime, timezone
from urllib.parse import quote
from telethon import TelegramClient
from telethon import functions
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

app = Flask(__name__)

tg_to_mc = []
lock = threading.Lock()

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = str(os.environ.get("CHAT_ID", "")).strip().rstrip("L")
SECRET_KEY = os.environ.get("SECRET_KEY", "change_this_secret")
CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
CONTROL_KEY = os.environ.get("CONTROL_KEY", "")

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
FORCE_SMS = os.environ.get("TG_FORCE_SMS", "true").strip().lower() in ("1", "true", "yes", "on")

SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "./tg_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)
QR_IMAGE_BASE = os.environ.get(
    "QR_IMAGE_BASE",
    "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=",
)

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")

auth_sessions = {}
auth_lock = threading.Lock()

WL_ADD_RE = re.compile(r"^[!/]?whitelist\s+add\s+(\S+)$", re.IGNORECASE)
WL_REMOVE_RE = re.compile(r"^[!/]?whitelist\s+remove\s+(\S+)$", re.IGNORECASE)

poller_thread = None
poller_started = False
poller_stop = threading.Event()


def require_secret():
    return request.headers.get("X-Secret-Key") == SECRET_KEY


def disable_webhook():
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=15,
        )
        print("deleteWebhook:", r.status_code, r.text, flush=True)
    except Exception as e:
        print("deleteWebhook error:", repr(e), flush=True)


def send_tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print("send_tg error:", repr(e), flush=True)


def handle_whitelist(action: str, name: str) -> str:
    if not CONTROL_URL or not CONTROL_KEY:
        return "⛔ CONTROL_URL или CONTROL_KEY не заданы в env"
    try:
        r = requests.get(
            f"{CONTROL_URL}/api/whitelist/{action}",
            params={"name": name},
            headers={"X-Api-Key": CONTROL_KEY},
            timeout=10,
        )
        data = r.json()
        return data.get("message", str(data))
    except Exception as e:
        return f"⛔ Ошибка связи с сервером: {e}"


def poll_telegram():
    offset = 0
    print(f"Telegram polling started (pid={os.getpid()})", flush=True)
    while not poller_stop.is_set():
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": [
                        "message",
                        "edited_message",
                        "channel_post",
                        "edited_channel_post",
                    ],
                },
                timeout=35,
            )
            data = r.json()
            if not data.get("ok"):
                print("getUpdates not ok:", data, flush=True)
                time.sleep(2)
                continue

            for upd in data.get("result", []):
                msg = upd.get("message") or upd.get("channel_post")
                if msg and "text" in msg:
                    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
                    if chat_id == CHAT_ID:
                        frm = msg.get("from", {}) or {}
                        name = frm.get("username") or frm.get("first_name") or "TGUser"
                        text = msg.get("text", "")

                        m_add = WL_ADD_RE.match(text)
                        m_rem = WL_REMOVE_RE.match(text)

                        if m_add:
                            send_tg(handle_whitelist("add", m_add.group(1)))
                        elif m_rem:
                            send_tg(handle_whitelist("remove", m_rem.group(1)))
                        else:
                            with lock:
                                tg_to_mc.append({"player": name, "message": text})
                            print(f"Queued TG->MC: {name}: {text}", flush=True)
                    else:
                        print(f"Skipped chat_id={chat_id}", flush=True)

                offset = max(offset, upd["update_id"] + 1)
        except Exception as e:
            print("Poll error:", repr(e), flush=True)
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
    with auth_lock:
        for sess in auth_sessions.values():
            try:
                client = sess.get("client")
                loop = sess.get("loop")
                if client and loop:
                    asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
            except Exception:
                pass


def get_or_create_loop(player_id: str):
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

        auth_sessions.setdefault(player_id, {})["loop"] = loop
        return loop


def run_async(player_id: str, coro):
    loop = get_or_create_loop(player_id)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=60)


def sent_code_debug(result):
    code_type = type(getattr(result, "type", None)).__name__
    next_type_obj = getattr(result, "next_type", None)
    next_type = type(next_type_obj).__name__ if next_type_obj else "none"
    timeout = getattr(result, "timeout", None)
    return code_type, next_type, timeout


def save_wait_code_session(player_id, client, phone, result, loop):
    phone_code_hash = result.phone_code_hash
    code_type, next_type, timeout = sent_code_debug(result)

    with auth_lock:
        auth_sessions[player_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": phone_code_hash,
            "state": "wait_code",
            "loop": loop,
            "code_type": code_type,
            "next_type": next_type,
            "timeout": timeout,
        }

    return code_type, next_type, timeout


def make_qr_open_url(tg_url: str) -> str:
    """HTTPS-ссылка на QR-картинку, которую можно открыть в браузере."""
    if not tg_url:
        return ""
    return QR_IMAGE_BASE + quote(tg_url, safe="")


def qr_response_fields(sess: dict) -> dict:
    return {
        "qr_url": sess.get("qr_open_url", ""),
        "qr_open_url": sess.get("qr_open_url", ""),
        "qr_token_url": sess.get("qr_token_url", ""),
        "qr_expires": sess.get("qr_expires", ""),
    }


def _utc_iso(dt):
    if not dt:
        return ""
    if isinstance(dt, (int, float)):
        return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat()
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(dt)


def save_wait_qr_session(player_id, client, qr_login, loop):
    token_url = getattr(qr_login, "url", "")
    with auth_lock:
        auth_sessions[player_id] = {
            "client": client,
            "state": "wait_qr",
            "loop": loop,
            "qr_login": qr_login,
            "qr_token_url": token_url,
            "qr_open_url": make_qr_open_url(token_url),
            "qr_expires": _utc_iso(getattr(qr_login, "expires", None)),
        }


def start_qr_waiter(player_id: str):
    """
    Starts (or restarts) a background coroutine that:
    - waits for QR scan confirmation
    - refreshes QR when it expires
    """
    with auth_lock:
        sess = auth_sessions.get(player_id, {})
        if sess.get("state") != "wait_qr":
            return
        loop = sess.get("loop")
        qr_login = sess.get("qr_login")
        client = sess.get("client")

    if not loop or not qr_login or not client:
        return

    async def _waiter():
        while True:
            with auth_lock:
                current = auth_sessions.get(player_id, {})
                if current.get("state") != "wait_qr":
                    return
                qr = current.get("qr_login")
                cl = current.get("client")

            if not qr or not cl:
                return

            try:
                await qr.wait(timeout=30)
                with auth_lock:
                    if player_id in auth_sessions:
                        auth_sessions[player_id]["state"] = "authorized"
                        auth_sessions[player_id].pop("qr_login", None)
                        auth_sessions[player_id].pop("qr_token_url", None)
                        auth_sessions[player_id].pop("qr_open_url", None)
                        auth_sessions[player_id].pop("qr_expires", None)
                print(f"Player {player_id} authorized via QR", flush=True)
                return
            except TimeoutError:
                # QR probably expired, recreate it and continue waiting.
                try:
                    await qr.recreate()
                    with auth_lock:
                        if player_id in auth_sessions and auth_sessions[player_id].get("state") == "wait_qr":
                            token_url = getattr(qr, "url", "")
                            auth_sessions[player_id]["qr_token_url"] = token_url
                            auth_sessions[player_id]["qr_open_url"] = make_qr_open_url(token_url)
                            auth_sessions[player_id]["qr_expires"] = _utc_iso(getattr(qr, "expires", None))
                except Exception as e:
                    print(f"qr recreate error for player={player_id!r}: {repr(e)}", flush=True)
                    await asyncio.sleep(3)
            except Exception as e:
                print(f"qr wait error for player={player_id!r}: {repr(e)}", flush=True)
                await asyncio.sleep(3)

    asyncio.run_coroutine_threadsafe(_waiter(), loop)


@app.route("/auth/start", methods=["POST"])
def auth_start():
    if not require_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id", "").strip()
    phone = data.get("phone", "").strip()

    print(
        f"/auth/start called: player_id={player_id!r}, phone={phone!r}, "
        f"api_id_set={bool(API_ID)}, api_hash_set={bool(API_HASH)}, force_sms={FORCE_SMS}",
        flush=True,
    )

    if not player_id or not phone:
        return jsonify({"error": "player_id and phone required"}), 400
    if not API_ID or not API_HASH:
        return jsonify({"error": "TG_API_ID / TG_API_HASH не заданы на сервере"}), 500

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
            return await client.send_code_request(phone, force_sms=FORCE_SMS)

        result = run_async(player_id, do_start())
        code_type, next_type, timeout = save_wait_code_session(player_id, client, phone, result, loop)

        print(
            f"Auth started for player {player_id}, phone={phone!r}, "
            f"code_type={code_type}, next_type={next_type}, timeout={timeout}",
            flush=True,
        )
        return jsonify({
            "status": "code_sent",
            "code_type": code_type,
            "next_type": next_type,
            "timeout": timeout,
        })
    except Exception as e:
        print(f"auth_start error for player={player_id!r}, phone={phone!r}: {repr(e)}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/auth/qr/start", methods=["POST"])
def auth_qr_start():
    if not require_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id", "").strip()

    print(f"/auth/qr/start called: player_id={player_id!r}", flush=True)

    if not player_id:
        return jsonify({"error": "player_id required"}), 400
    if not API_ID or not API_HASH:
        return jsonify({"error": "TG_API_ID / TG_API_HASH не заданы на сервере"}), 500

    with auth_lock:
        sess = auth_sessions.get(player_id, {})
        if sess.get("state") == "authorized":
            return jsonify({"status": "already_authorized"})
        if sess.get("state") == "wait_qr":
            return jsonify({
                "status": "qr_ready",
                **qr_response_fields(sess),
            })

    try:
        session_path = os.path.join(SESSIONS_DIR, player_id)
        loop = get_or_create_loop(player_id)
        client = TelegramClient(session_path, API_ID, API_HASH, loop=loop)

        async def do_start_qr():
            await client.connect()
            return await client.qr_login()

        qr_login = run_async(player_id, do_start_qr())
        save_wait_qr_session(player_id, client, qr_login, loop)
        start_qr_waiter(player_id)

        with auth_lock:
            sess = auth_sessions.get(player_id, {})

        return jsonify({
            "status": "qr_ready",
            **qr_response_fields(sess),
        })
    except Exception as e:
        print(f"auth_qr_start error for player={player_id!r}: {repr(e)}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/auth/qr/status", methods=["GET"])
def auth_qr_status():
    if not require_secret():
        return jsonify({"error": "unauthorized"}), 401

    player_id = request.args.get("player_id", "").strip()
    if not player_id:
        return jsonify({"error": "player_id required"}), 400

    with auth_lock:
        sess = auth_sessions.get(player_id)

    if not sess:
        return jsonify({"player_id": player_id, "state": "none"})

    state = sess.get("state", "none")
    out = {"player_id": player_id, "state": state}
    if state == "wait_qr":
        out.update(qr_response_fields(sess))
    return jsonify(out)


@app.route("/auth/resend", methods=["POST"])
def auth_resend():
    if not require_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id", "").strip()

    if not player_id:
        return jsonify({"error": "player_id required"}), 400

    with auth_lock:
        sess = auth_sessions.get(player_id)

    if not sess:
        return jsonify({"error": "no_session", "hint": "Сначала /tgauth number"}), 400
    if sess.get("state") == "authorized":
        return jsonify({"status": "already_authorized"})
    if sess.get("state") not in ("wait_code", "wait_password"):
        return jsonify({"error": "unexpected_state", "state": sess.get("state")}), 400

    phone = sess.get("phone", "")
    old_hash = sess.get("phone_code_hash", "")
    client = sess.get("client")
    loop = sess.get("loop")

    print(
        f"/auth/resend called: player_id={player_id!r}, phone={phone!r}, "
        f"old_code_type={sess.get('code_type')!r}, old_next_type={sess.get('next_type')!r}",
        flush=True,
    )

    try:
        async def do_resend():
            try:
                return await client(functions.auth.ResendCodeRequest(phone, old_hash))
            except Exception as resend_error:
                print(
                    f"resend_code failed for player={player_id!r}: {repr(resend_error)}; "
                    "trying send_code_request(force_sms=True)",
                    flush=True,
                )
                return await client.send_code_request(phone, force_sms=True)

        result = run_async(player_id, do_resend())
        code_type, next_type, timeout = save_wait_code_session(player_id, client, phone, result, loop)

        print(
            f"Auth code resent for player {player_id}, phone={phone!r}, "
            f"code_type={code_type}, next_type={next_type}, timeout={timeout}",
            flush=True,
        )
        return jsonify({
            "status": "code_sent",
            "code_type": code_type,
            "next_type": next_type,
            "timeout": timeout,
        })
    except Exception as e:
        print(f"auth_resend error for player={player_id!r}, phone={phone!r}: {repr(e)}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/auth/code", methods=["POST"])
def auth_code():
    if not require_secret():
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
    print(f"/auth/code called: player_id={player_id!r}, state={state!r}", flush=True)

    if state == "authorized":
        return jsonify({"status": "already_authorized"})

    if state == "wait_code":
        try:
            async def do_sign_in():
                return await sess["client"].sign_in(
                    phone=sess["phone"],
                    code=code,
                    phone_code_hash=sess["phone_code_hash"],
                )

            run_async(player_id, do_sign_in())
            with auth_lock:
                auth_sessions[player_id]["state"] = "authorized"
            print(f"Player {player_id} authorized via code", flush=True)
            return jsonify({"status": "authorized"})
        except SessionPasswordNeededError:
            with auth_lock:
                auth_sessions[player_id]["state"] = "wait_password"
            print(f"Player {player_id} needs 2FA password", flush=True)
            return jsonify({"status": "need_password"})
        except PhoneCodeInvalidError:
            print(f"Invalid code for player {player_id}", flush=True)
            return jsonify({"error": "invalid_code"}), 400
        except Exception as e:
            print(f"auth_code error for player={player_id!r}: {repr(e)}", flush=True)
            return jsonify({"error": str(e)}), 500

    if state == "wait_password":
        try:
            async def do_password():
                return await sess["client"].sign_in(password=code)

            run_async(player_id, do_password())
            with auth_lock:
                auth_sessions[player_id]["state"] = "authorized"
            print(f"Player {player_id} authorized via 2FA", flush=True)
            return jsonify({"status": "authorized"})
        except Exception as e:
            print(f"auth_password error for player={player_id!r}: {repr(e)}", flush=True)
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "unexpected_state", "state": state}), 400


@app.route("/auth/status", methods=["GET"])
def auth_status():
    if not require_secret():
        return jsonify({"error": "unauthorized"}), 401

    player_id = request.args.get("player_id", "").strip()
    if not player_id:
        return jsonify({"error": "player_id required"}), 400

    with auth_lock:
        sess = auth_sessions.get(player_id)

    state = sess.get("state", "none") if sess else "none"
    out = {"player_id": player_id, "state": state}
    if sess and state == "wait_qr":
        out.update(qr_response_fields(sess))
    return jsonify(out)


@app.route("/to-tg-user", methods=["POST"])
def to_tg_user():
    if not require_secret():
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
        print(f"to-tg-user error for player={player_id!r}: {repr(e)}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/to-tg", methods=["POST"])
def to_tg():
    if not require_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player = data.get("player", "Unknown").strip() or "Unknown"
    message = data.get("message", "").strip()
    player_id = data.get("player_id", "").strip()

    if not message:
        return jsonify({"error": "message required"}), 400

    if player_id:
        with auth_lock:
            sess = auth_sessions.get(player_id)

        if sess and sess.get("state") == "authorized":
            try:
                chat_id_int = int(CHAT_ID)

                async def do_send():
                    await sess["client"].send_message(chat_id_int, message)

                run_async(player_id, do_send())
                print(f"to-tg via user session: player_id={player_id!r}, player={player!r}", flush=True)
                return jsonify({"ok": True, "via": "user"})
            except Exception as e:
                print(
                    f"to-tg user session failed for player_id={player_id!r}: {repr(e)}; "
                    "falling back to bot",
                    flush=True,
                )

    text = f"*{player}*: {message}"
    try:
        rr = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=20,
        )
        if rr.status_code != 200:
            return jsonify({"error": "telegram send failed", "body": rr.text}), 502
        print(f"to-tg via bot: player={player!r}, player_id={player_id!r}", flush=True)
        return jsonify({"ok": True, "via": "bot"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/from-tg", methods=["GET"])
def from_tg():
    if not require_secret():
        return jsonify({"error": "unauthorized"}), 401

    with lock:
        out = tg_to_mc.copy()
        tg_to_mc.clear()
    return jsonify(out)


@app.route("/health", methods=["GET"])
def health():
    with auth_lock:
        authorized_count = sum(1 for s in auth_sessions.values() if s.get("state") == "authorized")
        waiting_count = sum(1 for s in auth_sessions.values() if s.get("state") in ("wait_code", "wait_password"))
    return jsonify({
        "status": "ok",
        "pid": os.getpid(),
        "authorized_users": authorized_count,
        "waiting_users": waiting_count,
    })


@app.before_request
def _boot_once():
    ensure_single_poller()


if __name__ == "__main__":
    ensure_single_poller()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
