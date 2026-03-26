import json
import os
import threading
import time

import redis
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = str(os.environ.get("CHAT_ID", "")).strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "change_this_secret")
REDIS_URL = os.environ.get("REDIS_URL")
SERVICE_MODE = os.environ.get("SERVICE_MODE", "web").strip().lower()  # web | worker
QUEUE_KEY = os.environ.get("QUEUE_KEY", "tg_to_mc_queue")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")
if CHAT_ID.endswith("L"):
    CHAT_ID = CHAT_ID[:-1]  # защита от случайного Java-суффикса
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

rdb = redis.from_url(REDIS_URL, decode_responses=True)


def tg_get(method: str, params=None, timeout=35):
    return requests.get(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        params=params or {},
        timeout=timeout
    )


def tg_post(method: str, payload: dict, timeout=20):
    return requests.post(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        json=payload,
        timeout=timeout
    )


def check_secret() -> bool:
    return request.headers.get("X-Secret-Key") == SECRET_KEY


def disable_webhook():
    try:
        resp = tg_post("deleteWebhook", {"drop_pending_updates": False}, timeout=15)
        print("deleteWebhook:", resp.status_code, resp.text)
    except Exception as e:
        print("deleteWebhook error:", e)


def enqueue_message(player: str, message: str):
    item = json.dumps({"player": player, "message": message}, ensure_ascii=False)
    rdb.lpush(QUEUE_KEY, item)


def dequeue_messages(max_items: int = 50):
    out = []
    for _ in range(max_items):
        raw = rdb.rpop(QUEUE_KEY)
        if raw is None:
            break
        try:
            out.append(json.loads(raw))
        except Exception:
            pass
    return out


def poll_telegram():
    offset = 0
    print("Telegram polling started")
    while True:
        try:
            resp = tg_get(
                "getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message", "edited_message", "channel_post", "edited_channel_post"]
                },
                timeout=35
            )
            data = resp.json()
            if not data.get("ok"):
                print("getUpdates not ok:", data)
                time.sleep(2)
                continue

            updates = data.get("result", [])
            if updates:
                print(f"getUpdates: {len(updates)} updates")

            for upd in updates:
                msg = upd.get("message") or upd.get("channel_post")
                if msg and "text" in msg:
                    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
                    if chat_id == CHAT_ID:
                        frm = msg.get("from", {}) or {}
                        name = frm.get("username") or frm.get("first_name") or "TGUser"
                        text = msg.get("text", "")
                        enqueue_message(name, text)
                        print(f"Queued TG->MC: {name}: {text}")
                    else:
                        print(f"Skipped message from chat_id={chat_id}, expected={CHAT_ID}")

                offset = max(offset, upd["update_id"] + 1)

        except Exception as e:
            print("Poll error:", e)
            time.sleep(3)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "mode": SERVICE_MODE})


@app.route("/to-tg", methods=["POST"])
def to_tg():
    if not check_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "player" not in data or "message" not in data:
        return jsonify({"error": "invalid payload"}), 400

    text = f"*{data['player']}*: {data['message']}"
    try:
        resp = tg_post("sendMessage", {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=20)
        if resp.status_code != 200:
            return jsonify({"error": "telegram send failed", "body": resp.text}), 502
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/from-tg", methods=["GET"])
def from_tg():
    if not check_secret():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(dequeue_messages(100))


if __name__ == "__main__":
    disable_webhook()
    if SERVICE_MODE == "worker":
        poll_telegram()
    else:
        app.run(host="0.0.0.0", port=5000)
else:
    # Для gunicorn
    if SERVICE_MODE == "worker":
        disable_webhook()
        threading.Thread(target=poll_telegram, daemon=True).start()
