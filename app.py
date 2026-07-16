import os
import time
import sqlite3
import requests
from flask import Flask, request, jsonify, render_template, g

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_URL = os.environ.get("APP_URL", "https://your-app.onrender.com")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = os.environ.get("DB_PATH", "bazar.db")

app = Flask(__name__)


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trips(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            planned_total REAL DEFAULT 0,
            actual_total REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER,
            name TEXT,
            qty REAL,
            unit TEXT,
            planned_price REAL,
            bought INTEGER DEFAULT 0,
            actual_qty REAL,
            actual_price REAL,
            unplanned INTEGER DEFAULT 0,
            note TEXT,
            location TEXT
        );
        """
    )
    db.commit()
    db.close()


init_db()


def tg(method, **params):
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN не задан"}
    r = requests.post(f"{TELEGRAM_API}/{method}", json=params, timeout=10)
    return r.json()


def get_open_trip(db):
    row = db.execute(
        "SELECT * FROM trips WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        cur = db.execute(
            "INSERT INTO trips(started_at) VALUES (?)", (str(int(time.time())),)
        )
        db.commit()
        row = db.execute("SELECT * FROM trips WHERE id=?", (cur.lastrowid,)).fetchone()
    return row


def set_menu_button(chat_id, role):
    url = f"{APP_URL}/app?role={role}"
    tg(
        "setChatMenuButton",
        chat_id=chat_id,
        menu_button={"type": "web_app", "text": "🛒 Список", "web_app": {"url": url}},
    )
    return [r["chat_id"] for r in db.execute("SELECT chat_id FROM users WHERE role=?", (role,)).fetchall()]


# ---------- Telegram webhook ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    db = get_db()

    msg = update.get("message")
    if msg:
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        if text == "/start":
            tg(
                "sendMessage",
                chat_id=chat_id,
                text="Привет! Кто ты, чтобы правильно настроить уведомления?",
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "Я жена", "callback_data": "role_wife"},
                        {"text": "Я муж", "callback_data": "role_husband"},
                    ]]
                },
            )
        elif text == "/list":
            row = db.execute("SELECT role FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            role = row["role"] if row else "husband"
            url = f"{APP_URL}/app?role={role}"
            set_menu_button(chat_id, role)
            tg(
                "sendMessage",
                chat_id=chat_id,
                text="Открыть список покупок:",
                reply_markup={"inline_keyboard": [[{"text": "🛒 Список на базар", "web_app": {"url": url}}]]},
            )

    cb = update.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        data = cb["data"]
        if data == "role_wife":
            db.execute("INSERT OR REPLACE INTO users(chat_id, role) VALUES (?, 'wife')", (chat_id,))
            db.commit()
            set_menu_button(chat_id, "wife")
            tg("sendMessage", chat_id=chat_id, text="Готово! Список открывается кнопкой 🛒 у поля ввода сообщения (слева от скрепки).")
        elif data == "role_husband":
            db.execute("INSERT OR REPLACE INTO users(chat_id, role) VALUES (?, 'husband')", (chat_id,))
            db.commit()
            set_menu_button(chat_id, "husband")
            tg("sendMessage", chat_id=chat_id, text="Готово! Список открывается кнопкой 🛒 у поля ввода сообщения (слева от скрепки).")
        tg("answerCallbackQuery", callback_query_id=cb["id"])

    return jsonify(ok=True)


# ---------- Mini App ----------
@app.route("/app")
def serve_app():
    role = request.args.get("role", "husband")
    return render_template("index.html", role=role)


# ---------- API ----------
@app.route("/api/trip", methods=["GET"])
def api_get_trip():
    db = get_db()
    trip = get_open_trip(db)
    items = db.execute("SELECT * FROM items WHERE trip_id=?", (trip["id"],)).fetchall()
    return jsonify(trip=dict(trip), items=[dict(i) for i in items])


@app.route("/api/items", methods=["POST"])
def api_add_item():
    db = get_db()
    trip = get_open_trip(db)
    data = request.get_json(force=True)
    db.execute(
        """INSERT INTO items(trip_id,name,qty,unit,planned_price,note,location,unplanned)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            trip["id"],
            data.get("name", ""),
            data.get("qty", 1),
            data.get("unit", "шт"),
            data.get("plannedPrice", 0),
            data.get("note", ""),
            data.get("location", ""),
            1 if data.get("unplanned") else 0,
        ),
    )
    db.commit()
    return jsonify(ok=True)


@app.route("/api/items/<int:item_id>", methods=["PATCH"])
def api_update_item(item_id):
    db = get_db()
    data = request.get_json(force=True)
    fields, values = [], []
    mapping = [
        ("name", "name"), ("qty", "qty"), ("unit", "unit"), ("plannedPrice", "planned_price"),
        ("note", "note"), ("location", "location"), ("bought", "bought"),
        ("actualQty", "actual_qty"), ("actualPrice", "actual_price"),
    ]
    for key, col in mapping:
        if key in data:
            fields.append(f"{col}=?")
            values.append(data[key])
    if fields:
        values.append(item_id)
        db.execute(f"UPDATE items SET {', '.join(fields)} WHERE id=?", values)
        db.commit()

    just_bought = "bought" in data and str(data["bought"]) in ("1", "True", "true")
    if just_bought:
        item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item:
            subtotal = (item["actual_qty"] or 0) * (item["actual_price"] or 0)
            text = f"🛒 Куплено: {item['name']} — {item['actual_qty']} {item['unit'] or 'шт'} × {item['actual_price']:.0f} = {subtotal:.0f}"
            for chat_id in role_chat_ids(db, "wife"):
                tg("sendMessage", chat_id=chat_id, text=text)

    return jsonify(ok=True)


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def api_delete_item(item_id):
    db = get_db()
    db.execute("DELETE FROM items WHERE id=?", (item_id,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/notify-husband", methods=["POST"])
def api_notify_husband():
    db = get_db()
    trip = get_open_trip(db)
    items = db.execute("SELECT * FROM items WHERE trip_id=? AND bought=0", (trip["id"],)).fetchall()
    lines = []
    for i in items:
        line = f"• {i['name']} — {i['qty']} {i['unit']} × {i['planned_price']}"
        if i["note"]:
            line += f" ({i['note']})"
        if i["location"]:
            line += f" 📍{i['location']}"
        lines.append(line)
    total = sum(i["qty"] * i["planned_price"] for i in items)
    text = "🛒 Новый список от жены:\n" + "\n".join(lines) + f"\n\nПлан: {total:.0f}"
    for chat_id in role_chat_ids(db, "husband"):
        tg(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup={"inline_keyboard": [[{"text": "Открыть список", "web_app": {"url": f"{APP_URL}/app?role=husband"}}]]},
        )
    return jsonify(ok=True)


@app.route("/api/finish", methods=["POST"])
def api_finish():
    db = get_db()
    trip = get_open_trip(db)
    items = db.execute("SELECT * FROM items WHERE trip_id=?", (trip["id"],)).fetchall()
    planned = sum(i["qty"] * i["planned_price"] for i in items if not i["unplanned"])
    actual = sum((i["actual_qty"] or 0) * (i["actual_price"] or 0) for i in items if i["bought"])
    db.execute(
        "UPDATE trips SET finished_at=?, planned_total=?, actual_total=? WHERE id=?",
        (str(int(time.time())), planned, actual, trip["id"]),
    )
    db.commit()
    diff = actual - planned
    verdict = "перерасход на" if diff > 0 else "экономия"

    bought_items = [i for i in items if i["bought"]]
    not_bought = [i for i in items if not i["bought"]]

    lines = []
    for i in bought_items:
        line = f"✅ {i['name']} — {i['actual_qty']} {i['unit'] or 'шт'} × {i['actual_price']:.0f} = {(i['actual_qty'] or 0)*(i['actual_price'] or 0):.0f}"
        if i["unplanned"]:
            line += " (внепланово)"
        lines.append(line)
    for i in not_bought:
        lines.append(f"❌ {i['name']} — не куплено")

    text = (
        "✅ Поход завершён.\n\n"
        + "\n".join(lines)
        + f"\n\nПлан: {planned:.0f}\nФакт: {actual:.0f}\n{verdict} {abs(diff):.0f}"
    )
    for chat_id in role_chat_ids(db, "wife"):
        tg("sendMessage", chat_id=chat_id, text=text)
    return jsonify(ok=True, planned=planned, actual=actual, diff=diff)


@app.route("/api/history", methods=["GET"])
def api_history():
    db = get_db()
    trips = db.execute("SELECT * FROM trips WHERE finished_at IS NOT NULL ORDER BY id DESC").fetchall()
    result = []
    for t in trips:
        items = db.execute("SELECT * FROM items WHERE trip_id=?", (t["id"],)).fetchall()
        result.append({"trip": dict(t), "items": [dict(i) for i in items]})
    return jsonify(history=result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
