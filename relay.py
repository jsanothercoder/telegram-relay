from flask import Flask, request, jsonify
import requests
import os
import threading
import time

app = Flask(__name__)
tg_to_mc = []
lock = threading.Lock()

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")  # string, compare as string
SECRET_KEY = os.environ.get("SECRET_KEY", "change_this_secret")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")


def tg_api(method: str, **kwargs):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    return requests.post(url, timeout=20, **kwargs)


def disable_webhook():
    # getUpdates + webhook together won't work
    try:
        r = tg_api("deleteWebhook", json={"drop_pending_updates": False})
        print("deleteWebhook:", r.status_code, r.text)
    except Exception as e:
        print("deleteWebhook error:", e)


def poll_telegram():
    offset = 0
    print("Telegram polling started")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": ["message", "channel_post"]},
                timeout=35
            )
            data = r.json()
            if not data.get("ok"):
                print("getUpdates not ok:", data)
                time.sleep(3)
                continue

            updates = data.get("result", [])
            if updates:
                print(f"getUpdates: {len(updates)} updates")

            for upd in updates:
                msg = upd.get("message") or upd.get("channel_post")
                if msg and "text" in msg:
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id == str(CHAT_ID):
                        user = msg.get("from", {}) or {}
                        name = user.get("username") or user.get("first_name") or "TGUser"
                        text = msg["text"]
                        with lock:
                            tg_to_mc.append({"player": name, "message": text})
                        print(f"Queued TG->MC: {name}: {text}")
                    else:
                        print(f"Skipped message from chat_id={chat_id}, expected={CHAT_ID}")

                offset = max(offset, upd["update_id"] + 1)

        except Exception as e:
            print(f"Poll error: {e}")
            time.sleep(5)


disable_webhook()
threading.Thread(target=poll_telegram, daemon=True).start()


@app.route("/to-tg", methods=["POST"])
def to_tg():
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "player" not in data or "message" not in data:
        return jsonify({"error": "invalid payload"}), 400

    text = f"*{data['player']}*: {data['message']}"
    try:
        r = tg_api(
            "sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
        if r.status_code != 200:
            return jsonify({"error": "telegram send failed", "body": r.text}), 502
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
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
