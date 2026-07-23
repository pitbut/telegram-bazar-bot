import os
import time
import string
import random
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
        CREATE TABLE IF NOT EXISTS families(
            code TEXT PRIMARY KEY,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER PRIMARY KEY,
            family_code TEXT,
            name TEXT
        );
        CREATE TABLE IF NOT EXISTS trips(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_code TEXT,
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
            location TEXT,
            added_by TEXT,
            bought_by TEXT
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


def gen_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def get_open_trip(db, family_code):
    row = db.execute(
        "SELECT * FROM trips WHERE family_code=? AND finished_at IS NULL ORDER BY id DESC LIMIT 1",
        (family_code,),
    ).fetchone()
    if row is None:
        cur = db.execute(
            "INSERT INTO trips(family_code, started_at) VALUES (?, ?)",
            (family_code, str(int(time.time()))),
        )
        db.commit()
        row = db.execute("SELECT * FROM trips WHERE id=?", (cur.lastrowid,)).fetchone()
    return row


def set_menu_button(chat_id, family_code):
    url = f"{APP_URL}/app?code={family_code}"
    tg(
        "setChatMenuButton",
        chat_id=chat_id,
        menu_button={"type": "web_app", "text": "🛒 Список", "web_app": {"url": url}},
    )


def family_members(db, family_code):
    return db.execute("SELECT chat_id, name FROM users WHERE family_code=?", (family_code,)).fetchall()


def family_chat_ids(db, family_code, exclude_chat_id=None):
    return [
        m["chat_id"] for m in family_members(db, family_code)
        if m["chat_id"] != exclude_chat_id
    ]


def get_user(db, chat_id):
    return db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


# ---------- Telegram webhook ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    db = get_db()

    msg = update.get("message")
    if msg:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        first_name = msg["from"].get("first_name", "Без имени")

        if text == "/start":
            user = get_user(db, chat_id)
            if user:
                set_menu_button(chat_id, user["family_code"])
                tg(
                    "sendMessage",
                    chat_id=chat_id,
                    text=(
                        f"Ты уже в списке (код {user['family_code']}). Открывай кнопкой 🛒 у поля ввода.\n\n"
                        "Если это тестовый/неправильный список и хочешь начать заново — напиши /reset."
                    ),
                )
            else:
                tg(
                    "sendMessage",
                    chat_id=chat_id,
                    text=(
                        "👋 Привет! Это бот для общего списка покупок.\n\n"
                        "Как это работает:\n"
                        "1. Один человек создаёт список и получает короткий код.\n"
                        "2. Остальные (жена, муж, мама — кто угодно) присоединяются по этому коду.\n"
                        "3. Все видят один и тот же список, могут добавлять товары и отмечать покупки — "
                        "остальные участники сразу получают уведомление.\n\n"
                        "Выбери, что сделать:"
                    ),
                    reply_markup={
                        "inline_keyboard": [[
                            {"text": "Создать новый", "callback_data": "create_family"},
                            {"text": "У меня есть код", "callback_data": "ask_code"},
                        ]]
                    },
                )
        elif text == "/reset":
            user = get_user(db, chat_id)
            if not user:
                tg("sendMessage", chat_id=chat_id, text="Ты и так ни к какому списку не подключён. Напиши /start.")
            else:
                old_code = user["family_code"]
                db.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
                db.commit()
                tg(
                    "sendMessage",
                    chat_id=chat_id,
                    text=(
                        f"Готово, ты отключён от списка с кодом {old_code}. "
                        "Список и остальные участники никуда не делись — просто ты в нём больше не участвуешь.\n\n"
                        "Напиши /start, чтобы создать новый список или присоединиться к другому по коду."
                    ),
                )
        elif text == "/help":
            tg(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "Как пользоваться:\n\n"
                    "/start — начать (создать список или присоединиться по коду)\n"
                    "/join КОД — присоединиться к чужому списку по коду\n"
                    "/code — посмотреть свой текущий код (чтобы подключить ещё кого-то)\n"
                    "/list — открыть список покупок\n"
                    "/reset — отключиться от текущего списка и начать заново\n\n"
                    "Внутри списка: добавляете товары с количеством и ценой, отмечаете купленное — "
                    "остальным участникам сразу приходит уведомление, кто и что купил."
                ),
            )
        elif text.startswith("/join"):
            parts = text.split()
            if len(parts) != 2:
                tg("sendMessage", chat_id=chat_id, text="Формат: /join КОД (код тебе должен прислать тот, кто создал список)")
            else:
                code = parts[1].upper()
                fam = db.execute("SELECT * FROM families WHERE code=?", (code,)).fetchone()
                if not fam:
                    tg("sendMessage", chat_id=chat_id, text="Такого кода не найдено. Проверь и попробуй ещё раз.")
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO users(chat_id, family_code, name) VALUES (?,?,?)",
                        (chat_id, code, first_name),
                    )
                    db.commit()
                    set_menu_button(chat_id, code)
                    members = family_members(db, code)
                    names = ", ".join(m["name"] for m in members)
                    tg("sendMessage", chat_id=chat_id, text=f"Готово! Ты в списке (код {code}). Участники: {names}. Открывай кнопкой 🛒 у поля ввода.")
                    for other_id in family_chat_ids(db, code, exclude_chat_id=chat_id):
                        tg("sendMessage", chat_id=other_id, text=f"👋 {first_name} присоединил(ся/ась) к вашему списку покупок.")
        elif text == "/list":
            user = get_user(db, chat_id)
            if not user:
                tg("sendMessage", chat_id=chat_id, text="Сначала нажми /start, чтобы создать список или присоединиться по коду.")
            else:
                set_menu_button(chat_id, user["family_code"])
                url = f"{APP_URL}/app?code={user['family_code']}"
                tg(
                    "sendMessage",
                    chat_id=chat_id,
                    text="Открыть список покупок:",
                    reply_markup={"inline_keyboard": [[{"text": "🛒 Список на базар", "web_app": {"url": url}}]]},
                )
        elif text == "/code":
            user = get_user(db, chat_id)
            if user:
                tg("sendMessage", chat_id=chat_id, text=f"Код вашего списка: {user['family_code']}\nОтправь его тому, кого хочешь добавить — он должен написать боту /join {user['family_code']}")
            else:
                tg("sendMessage", chat_id=chat_id, text="У тебя пока нет списка. Нажми /start.")

    cb = update.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        first_name = cb["from"].get("first_name", "Без имени")
        data = cb["data"]
        if data == "create_family":
            code = gen_code()
            db.execute("INSERT INTO families(code, created_at) VALUES (?, ?)", (code, str(int(time.time()))))
            db.execute("INSERT OR REPLACE INTO users(chat_id, family_code, name) VALUES (?,?,?)", (chat_id, code, first_name))
            db.commit()
            set_menu_button(chat_id, code)
            tg(
                "sendMessage",
                chat_id=chat_id,
                text=f"Готово! Твой код: {code}\n\nОтправь его остальным (жене, маме и т.д.) — им нужно написать этому боту команду:\n/join {code}\n\nСписок открывается кнопкой 🛒 у поля ввода.",
            )
        elif data == "ask_code":
            tg("sendMessage", chat_id=chat_id, text="Напиши команду /join КОД (код тебе должен прислать тот, кто создал список).")
        tg("answerCallbackQuery", callback_query_id=cb["id"])

    return jsonify(ok=True)


# ---------- Mini App ----------
@app.route("/app")
def serve_app():
    code = request.args.get("code", "")
    return render_template("index.html", code=code)


# ---------- API ----------
@app.route("/api/trip", methods=["GET"])
def api_get_trip():
    db = get_db()
    code = request.args.get("code", "")
    trip = get_open_trip(db, code)
    items = db.execute("SELECT * FROM items WHERE trip_id=?", (trip["id"],)).fetchall()
    members = family_members(db, code)
    return jsonify(trip=dict(trip), items=[dict(i) for i in items], members=[dict(m) for m in members])


@app.route("/api/items", methods=["POST"])
def api_add_item():
    db = get_db()
    code = request.args.get("code", "")
    trip = get_open_trip(db, code)
    data = request.get_json(force=True)
    db.execute(
        """INSERT INTO items(trip_id,name,qty,unit,planned_price,note,location,unplanned,added_by)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            trip["id"],
            data.get("name", ""),
            data.get("qty", 1),
            data.get("unit", "шт"),
            data.get("plannedPrice", 0),
            data.get("note", ""),
            data.get("location", ""),
            1 if data.get("unplanned") else 0,
            data.get("addedBy", ""),
        ),
    )
    db.commit()
    return jsonify(ok=True)


@app.route("/api/items/<int:item_id>", methods=["PATCH"])
def api_update_item(item_id):
    db = get_db()
    code = request.args.get("code", "")
    data = request.get_json(force=True)
    fields, values = [], []
    mapping = [
        ("name", "name"), ("qty", "qty"), ("unit", "unit"), ("plannedPrice", "planned_price"),
        ("note", "note"), ("location", "location"), ("bought", "bought"),
        ("actualQty", "actual_qty"), ("actualPrice", "actual_price"), ("boughtBy", "bought_by"),
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
            who = item["bought_by"] or "кто-то"
            actor_chat_id = data.get("actorChatId")
            text = f"🛒 {who} купил(а): {item['name']} — {item['actual_qty']} {item['unit'] or 'шт'} × {item['actual_price']:.0f} = {subtotal:.0f}"
            for chat_id in family_chat_ids(db, code, exclude_chat_id=actor_chat_id):
                tg("sendMessage", chat_id=chat_id, text=text)

    return jsonify(ok=True)


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def api_delete_item(item_id):
    db = get_db()
    db.execute("DELETE FROM items WHERE id=?", (item_id,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/notify", methods=["POST"])
def api_notify():
    db = get_db()
    code = request.args.get("code", "")
    data = request.get_json(force=True) or {}
    actor_chat_id = data.get("actorChatId")
    actor_name = data.get("actorName", "Кто-то")
    trip = get_open_trip(db, code)
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
    text = f"🛒 {actor_name} обновил(а) список:\n" + "\n".join(lines) + f"\n\nПлан: {total:.0f}"
    for chat_id in family_chat_ids(db, code, exclude_chat_id=actor_chat_id):
        tg(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup={"inline_keyboard": [[{"text": "Открыть список", "web_app": {"url": f"{APP_URL}/app?code={code}"}}]]},
        )
    return jsonify(ok=True)


@app.route("/api/finish", methods=["POST"])
def api_finish():
    db = get_db()
    code = request.args.get("code", "")
    data = request.get_json(force=True) or {}
    actor_chat_id = data.get("actorChatId")
    trip = get_open_trip(db, code)
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
        if i["bought_by"]:
            line += f" ({i['bought_by']})"
        if i["unplanned"]:
            line += " [внепланово]"
        lines.append(line)
    for i in not_bought:
        lines.append(f"❌ {i['name']} — не куплено")

    text = (
        "✅ Поход завершён.\n\n"
        + "\n".join(lines)
        + f"\n\nПлан: {planned:.0f}\nФакт: {actual:.0f}\n{verdict} {abs(diff):.0f}"
    )
    for chat_id in family_chat_ids(db, code, exclude_chat_id=actor_chat_id):
        tg("sendMessage", chat_id=chat_id, text=text)
    return jsonify(ok=True, planned=planned, actual=actual, diff=diff)


@app.route("/api/history", methods=["GET"])
def api_history():
    db = get_db()
    code = request.args.get("code", "")
    trips = db.execute(
        "SELECT * FROM trips WHERE family_code=? AND finished_at IS NOT NULL ORDER BY id DESC", (code,)
    ).fetchall()
    result = []
    for t in trips:
        items = db.execute("SELECT * FROM items WHERE trip_id=?", (t["id"],)).fetchall()
        result.append({"trip": dict(t), "items": [dict(i) for i in items]})
    return jsonify(history=result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
