from flask import Flask, request, jsonify
import requests, os, threading, time

app = Flask(__name__)
tg_to_mc = []
lock = threading.Lock()

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
SECRET_KEY = os.environ.get("SECRET_KEY", "change_this_secret")

def poll_telegram():
    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35
            )
            updates = r.json().get("result", [])
            for upd in updates:
                if "message" in upd and "text" in upd["message"]:
                    name = upd["message"]["from"].get("username", "TGUser")
                    text = upd["message"]["text"]
                    with lock:
                        tg_to_mc.append({"player": name, "message": text})
                offset = max(offset, upd["update_id"] + 1)
        except Exception as e:
            print(f"Poll error: {e}")
            time.sleep(5)

threading.Thread(target=poll_telegram, daemon=True).start()

@app.route("/to-tg", methods=["POST"])
def to_tg():
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401
    data = request.json
    if not data or "player" not in data or "message" not in data:
        return jsonify({"error": "invalid payload"}), 400
    text = f"*{data['player']}*: {data['message']}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
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
