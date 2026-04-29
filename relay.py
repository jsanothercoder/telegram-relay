from flask import Flask, request, jsonify
import requests
import os
import threading
import time
import re
import atexit

app = Flask(__name__)

tg_to_mc = []
lock = threading.Lock()

TOKEN        = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID      = str(os.environ.get("CHAT_ID", "")).strip().rstrip("L")
SECRET_KEY   = os.environ.get("SECRET_KEY", "change_this_secret")

# === НОВОЕ: адрес main.py на сервере и его API ключ ===
CONTROL_URL  = os.environ.get("CONTROL_URL", "").rstrip("/")   # напр. http://1.2.3.4:25583
CONTROL_KEY  = os.environ.get("CONTROL_KEY", "")               # MC_API_KEY из main.py

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")

# Паттерн для whitelist команд из TG
WL_ADD_RE    = re.compile(r"^[!/]?whitelist\s+add\s+(\S+)$",    re.IGNORECASE)
WL_REMOVE_RE = re.compile(r"^[!/]?whitelist\s+remove\s+(\S+)$", re.IGNORECASE)

poller_thread  = None
poller_started = False
poller_stop    = threading.Event()


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
    """Отправить сообщение в TG чат."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print("send_tg error:", e)


def handle_whitelist(action: str, name: str) -> str:
    """Вызвать whitelist add/remove на main.py напрямую (минуя MC команду)."""
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

            updates = data.get("result", [])

            for upd in updates:
                msg = upd.get("message") or upd.get("channel_post")
                if msg and "text" in msg:
                    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
                    if chat_id == CHAT_ID:
                        frm  = msg.get("from", {}) or {}
                        name = frm.get("username") or frm.get("first_name") or "TGUser"
                        text = msg.get("text", "")

                        # === Перехват whitelist команд ===
                        m_add = WL_ADD_RE.match(text)
                        m_rem = WL_REMOVE_RE.match(text)

                        if m_add:
                            player = m_add.group(1)
                            result = handle_whitelist("add", player)
                            send_tg(result)
                            print(f"Whitelist add intercepted: {player} → {result}")

                        elif m_rem:
                            player = m_rem.group(1)
                            result = handle_whitelist("remove", player)
                            send_tg(result)
                            print(f"Whitelist remove intercepted: {player} → {result}")

                        else:
                            # Обычное сообщение — в очередь для чата
                            with lock:
                                tg_to_mc.append({"player": name, "message": text})
                            print(f"Queued TG->MC: {name}: {text}")
                    else:
                        print(f"Skipped message from chat_id={chat_id}, expected={CHAT_ID}")

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


@app.before_request
def _boot_once():
    ensure_single_poller()


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
    return jsonify({"status": "ok", "pid": os.getpid()})


if __name__ == "__main__":
    ensure_single_poller()
    app.run(host="0.0.0.0", port=5000)
