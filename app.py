import os
import json
import sqlite3
import logging
from pathlib import Path
from flask import Flask, request, abort, jsonify
import requests

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/tmp/propiska_bot.sqlite3")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Render provides RENDER_EXTERNAL_URL automatically. Register the webhook on startup
# once the bot token and deployment URL are configured.
def register_webhook():
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not (BOT_TOKEN and WEBHOOK_SECRET and base_url):
        logging.info("Webhook not auto-registered: configure bot token, secret, and Render URL.")
        return
    try:
        response = requests.post(
            f"{TG_API}/setWebhook",
            json={
                "url": base_url + "/telegram/webhook",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=20,
        )
        response.raise_for_status()
        logging.info("Telegram webhook registration: %s", response.text)
    except Exception:
        logging.exception("Could not register Telegram webhook")

register_webhook()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        username TEXT,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn

def tg(method, payload):
    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is not configured")
        return {}
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        logging.exception("Telegram API request failed: %s", method)
        return {}

def send(chat_id, text, **kwargs):
    return tg("sendMessage", {"chat_id": chat_id, "text": text, **kwargs})

def ai_text(prompt):
    if not GEMINI_API_KEY:
        return None
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    try:
        r = requests.post(url, params={"key": GEMINI_API_KEY},
            json={"contents":[{"parts":[{"text": prompt}]}],
                  "generationConfig":{"temperature":0.6}},
            timeout=35)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        logging.exception("AI generation failed")
        return None

def save_lead(user, message):
    conn = db()
    conn.execute("INSERT INTO leads(user_id, username, message) VALUES(?,?,?)",
                 (str(user.get("id","")), user.get("username",""), message))
    conn.commit()
    conn.close()

def faq_reply(text):
    """Reliable approved FAQ responses when no external AI key is configured."""
    q = text.lower().replace("ё", "е")
    if any(k in q for k in ("сколько", "цена", "стоимость", "прайс", "стоит")):
        if any(k in q for k in ("3 года", "три года", "на 3 года", "на три года")):
            return ("Стоимость при единовременной оплате за 3 года: взрослый — 40 000 ₽, "
                    "несовершеннолетний — 30 000 ₽. Точные условия и возможность оформления "
                    "уточнит менеджер @PropIskaMNG.")
        if any(k in q for k in ("иностран", "иностранец", "иностранца", "гражданин другой страны")):
            return ("По имеющимся условиям регистрация иностранного гражданина на 6 месяцев — "
                    "13 000 ₽. Подбор адреса, если требуется, — 3 000 ₽. Возможность оформления "
                    "зависит от ситуации и документов; уточните детали у @PropIskaMNG.")
        if any(k in q for k in ("ребен", "несовершеннолет", "дет")):
            return ("По текущему прайсу регистрация несовершеннолетнего на 1 год — 12 000 ₽; "
                    "при единовременной оплате за 3 года — 30 000 ₽. Ребёнка до 14 лет отдельно "
                    "не регистрируют. Условия конкретного случая уточнит @PropIskaMNG.")
        if any(k in q for k in ("взросл", "год", "12 месяцев", "на год", "на 1 год")):
            return ("По текущему прайсу регистрация взрослого на 1 год — 17 000 ₽. "
                    "При единовременной оплате за 3 года — 40 000 ₽. Подбор адреса, если требуется, "
                    "— 3 000 ₽. Итоговые условия подтвердит менеджер @PropIskaMNG.")
        return ("По текущему прайсу: взрослый на 1 год — 17 000 ₽; несовершеннолетний на 1 год — "
                "12 000 ₽; при единовременной оплате за 3 года — 40 000 ₽ для взрослого и "
                "30 000 ₽ для несовершеннолетнего. Регистрация иностранного гражданина на 6 месяцев "
                "— 13 000 ₽. Подбор адреса — 3 000 ₽. Напишите @PropIskaMNG для уточнения.")
    if any(k in q for k in ("договор", "оферта", "оплата", "предоплат")):
        return ("Работа оформляется по договору-оферте; порядок и условия оплаты указываются в договоре. "
                "Перед оплатой рекомендуем внимательно ознакомиться с условиями. Уточнить детали можно у "
                "@PropIskaMNG.")
    if any(k in q for k in ("гарант", "точно оформ", "точно получ", "обещаете")):
        return ("Заранее гарантировать результат нельзя: возможность оформления зависит от обстоятельств "
                "и проверки документов. Условия и порядок работы можно уточнить у @PropIskaMNG.")
    if any(k in q for k in ("адрес", "подбор")):
        return ("Подбор адреса, если требуется, — 3 000 ₽ по текущим условиям. Возможность оформления "
                "и подходящий вариант нужно подтвердить с менеджером @PropIskaMNG.")
    return None

def create_draft(text):
    conn = db()
    cur = conn.execute("INSERT INTO drafts(text) VALUES(?)", (text,))
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()
    return draft_id

def handle_message(message):
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    user = message.get("from", {})
    text = (message.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/id"):
        send(chat_id, f"Ваш Telegram ID: {chat_id}")
        return
    if text.startswith("/start"):
        send(chat_id,
             "Здравствуйте! Я помощник канала о регистрации в Москве и Московской области.\n\n"
             "Могу подсказать ориентировочные цены и условия, а при необходимости передать вопрос менеджеру.\n"
             "Взрослый на 1 год — 17 000 ₽; несовершеннолетний на 1 год — 12 000 ₽.\n\n"
             "Задайте вопрос сообщением. Менеджер: @PropIskaMNG")
        return
    if ADMIN_ID and chat_id == ADMIN_ID and text.startswith("/draft"):
        prompt = text[len("/draft"):].strip()
        if not prompt:
            send(chat_id, "Напишите тему после команды, например: /draft пост о семейной скидке")
            return
        draft = ai_text(
            "Напиши аккуратный Telegram-пост на русском для канала @Moskva_propiska. "
            "Не обещай гарантированный результат и не утверждай юридические факты без оснований. "
            "Не выдумывай цены и условия. Если данных не хватает, оставь пометку [ПРОВЕРИТЬ]. "
            "Верни только текст поста. Тема: " + prompt)
        if not draft:
            draft = "Черновик по теме: " + prompt + "\n\n[Добавьте текст поста вручную: AI API не подключён.]"
        draft_id = create_draft(draft)
        keyboard = {"inline_keyboard":[
            [{"text":"✅ Опубликовать","callback_data":f"approve:{draft_id}"},
             {"text":"🗑 Отклонить","callback_data":f"reject:{draft_id}"}]
        ]}
        send(chat_id, f"ЧЕРНОВИК №{draft_id}\n\n{draft}",
             reply_markup=keyboard)
        return
    if ADMIN_ID and chat_id == ADMIN_ID and text == "/stats":
        conn = db()
        leads = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        pending = conn.execute("SELECT COUNT(*) n FROM drafts WHERE status='pending'").fetchone()["n"]
        conn.close()
        send(chat_id, f"Заявок сохранено: {leads}\nЧерновиков ожидают решения: {pending}")
        return
    save_lead(user, text)
    admin_notified = False
    if ADMIN_ID:
        uname = user.get("username")
        who = ("@" + uname) if uname else f"user_id={user.get('id')}"
        notice = f"📩 Новое обращение от {who}\nID: {user.get('id')}\n\n{text}"
        notify_result = send(ADMIN_ID, notice)
        admin_notified = bool(notify_result.get("ok"))
        if not admin_notified:
            logging.error("Lead notification failed for configured TELEGRAM_ADMIN_ID=%s", ADMIN_ID)
    else:
        logging.error("Lead received but TELEGRAM_ADMIN_ID is not configured")
    reply = faq_reply(text)
    if not reply:
        reply = ai_text(
        "Ты помощник сервиса, который помогает организовать оформление регистрации по месту пребывания в Москве и Московской области. "
        "Отвечай по-русски кратко, вежливо, не обещай гарантированный результат, не выдумывай правовые нормы, цены или сроки. "
        "Если вопрос требует проверки документов, оплаты или индивидуального решения, предложи написать менеджеру @PropIskaMNG. "
        "Не запрашивай паспортные данные и другие чувствительные документы в этом чате. "
        "Используй только переданные цены: взрослый 17 000 ₽/год, несовершеннолетний 12 000 ₽/год, "
        "при единовременной оплате за 3 года взрослый 40 000 ₽, несовершеннолетний 30 000 ₽, "
        "иностранный гражданин 13 000 ₽/6 месяцев, подбор адреса 3 000 ₽. "
        "Если данных недостаточно, предложи менеджера. Вопрос клиента: " + text)
    if not reply:
        reply = ("Спасибо за сообщение! Для уточнения условий напишите менеджеру @PropIskaMNG. "
                 "Ваш вопрос сохранён.")
    if not admin_notified:
        reply += "\n\nВажно: автоматическое уведомление менеджеру сейчас не подтверждено. "
        reply += "Пожалуйста, напишите напрямую @PropIskaMNG, чтобы не ждать ответа."
    send(chat_id, reply)

@app.get("/")
def index():
    return "Propiska assistant is running. Configure Telegram webhook to activate it."

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.post("/telegram/webhook")
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)
    update = request.get_json(silent=True) or {}
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        user_id = str((cq.get("from") or {}).get("id", ""))
        tg("answerCallbackQuery", {"callback_query_id": cq.get("id")})
        if not ADMIN_ID or user_id != ADMIN_ID:
            return jsonify({"ok": True})
        action, _, draft_id = data.partition(":")
        if not draft_id.isdigit():
            return jsonify({"ok": True})
        conn = db()
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (int(draft_id),)).fetchone()
        if not row or row["status"] != "pending":
            conn.close()
            return jsonify({"ok": True})
        if action == "approve":
            if not CHANNEL_ID:
                send(ADMIN_ID, "Не задан TELEGRAM_CHANNEL_ID. Пост не опубликован.")
            else:
                result = tg("sendMessage", {"chat_id": CHANNEL_ID, "text": row["text"]})
                if result.get("ok"):
                    conn.execute("UPDATE drafts SET status='published' WHERE id=?", (int(draft_id),))
                    send(ADMIN_ID, f"✅ Черновик №{draft_id} опубликован.")
                else:
                    send(ADMIN_ID, "Telegram не подтвердил публикацию. Проверьте, что бот — администратор канала.")
        elif action == "reject":
            conn.execute("UPDATE drafts SET status='rejected' WHERE id=?", (int(draft_id),))
            send(ADMIN_ID, f"Черновик №{draft_id} отклонён. Публикации не было.")
        conn.commit()
        conn.close()
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
