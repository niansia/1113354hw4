#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Bot + Gemini-1.5-flash + Stock 查價 (Alpha Vantage)
指令：
  /help              ‧ 指令說明
  /history           ‧ 最近 10 筆聊天紀錄
  /clear             ‧ 清空聊天紀錄
  /stock <代號>      ‧ 即時股價
其他文字 → 交給 Gemini 生成回覆
"""

import os, sqlite3, configparser, requests
from datetime import datetime
from flask import Flask, request, abort, jsonify

from linebot import LineBotApi, WebhookParser
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    StickerMessage, StickerSendMessage,
    ImageMessage, VideoMessage, LocationMessage
)

from google import genai

config = configparser.ConfigParser()
if os.path.exists("config.ini"):
    config.read("config.ini", encoding="utf-8")
else:
    config.read_dict({"Line": {}, "Gemini": {}, "API": {}})

# Line
CHANNEL_SECRET       = config["Line"].get("CHANNEL_SECRET")       or os.getenv("CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = config["Line"].get("CHANNEL_ACCESS_TOKEN") or os.getenv("CHANNEL_ACCESS_TOKEN")
# Gemini
GEMINI_API_KEY       = config["Gemini"].get("API_KEY")            or os.getenv("GEMINI_API_KEY")
# Alpha Vantage
STOCK_KEY            = config["API"].get("STOCK_KEY")             or os.getenv("STOCK_KEY")

if not all([CHANNEL_SECRET, CHANNEL_ACCESS_TOKEN, GEMINI_API_KEY, STOCK_KEY]):
    raise RuntimeError(" 缺少必要金鑰：CHANNEL_SECRET / CHANNEL_ACCESS_TOKEN / GEMINI_API_KEY / STOCK_KEY")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
parser       = WebhookParser(CHANNEL_SECRET)

genai_client = genai.Client(api_key=GEMINI_API_KEY)

DB = "chat_history.db"

def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_history(
                            id        INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id   TEXT,
                            role      TEXT,
                            msg_type  TEXT,
                            content   TEXT,
                            timestamp TEXT
                        )""")
        conn.commit()

def save_msg(uid, role, mtype, content):
    with sqlite3.connect(DB) as conn:
        conn.execute(
            "INSERT INTO chat_history(user_id, role, msg_type, content, timestamp) "
            "VALUES (?,?,?,?,?)",
            (uid, role, mtype, content, datetime.utcnow().isoformat())
        )
        conn.commit()

def fetch_history(uid, limit=10):
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM chat_history "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)
        ).fetchall()
    rows.reverse()
    return rows

def delete_history(uid):
    with sqlite3.connect(DB) as conn:
        conn.execute("DELETE FROM chat_history WHERE user_id=?", (uid,))
        conn.commit()

init_db()

def get_stock(symbol: str) -> str:
    url = (
        "https://www.alphavantage.co/query"
        f"?function=GLOBAL_QUOTE&symbol={symbol}&apikey={STOCK_KEY}"
    )
    try:
        data = requests.get(url, timeout=10).json().get("Global Quote", {})
        if not data:
            return " 查不到股價，請確認股票代號"
        price   = float(data["05. price"])
        change  = float(data["09. change"])
        changeP = float(data["10. change percent"].rstrip("%"))
        return (f" {symbol.upper()} 現價 ${price:,.2f}\n"
                f"漲跌 {change:+.2f}（{changeP:+.2f}％）")
    except Exception as e:
        return f" 讀取失敗：{e}"

HELP_TEXT = (
    " **可用指令**\n"
    "/help            ‧ 指令說明\n"
    "/history         ‧ 最近 10 筆聊天紀錄\n"
    "/clear           ‧ 清空聊天紀錄\n"
    "/stock <代號>    ‧ 即時股價查詢\n"
    "（其他文字交給 AI 回覆）"
)

def handle_command(cmd: str, args: list[str], uid: str):
    """返回 (handled:bool, reply:str)"""
    if cmd in ("/help", "/指令"):
        return True, HELP_TEXT

    if cmd == "/history":
        rows = fetch_history(uid)
        if not rows:
            return True, "📭 尚無聊天紀錄"
        lines = [f"{r[2][:19]} | {r[0]}: {r[1]}" for r in rows]
        return True, "🗂 最近 10 筆紀錄\n" + "\n".join(lines)

    if cmd == "/clear":
        delete_history(uid)
        return True, " 聊天紀錄已清空"

    if cmd == "/stock":
        if not args:
            return True, "格式：/stock <股票代號>（如 /stock AAPL）"
        return True, get_stock(args[0])

    return False, " 未識別指令，輸入 /help 查看所有指令"

app = Flask(__name__)

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")

    for event in events:
        if not isinstance(event, MessageEvent):
            continue

        uid = event.source.user_id
        incoming = event.message

        if isinstance(incoming, TextMessage):
            text = incoming.text.strip()
            save_msg(uid, "user", "text", text)

            if text.startswith("/"):
                parts = text.split()
                handled, reply = handle_command(parts[0].lower(), parts[1:], uid)
                if handled:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
                    save_msg(uid, "bot", "text", reply)
                    continue

            try:
                resp = genai_client.models.generate_content(
                    model="gemini-1.5-flash-latest",
                    contents=[text]
                )
                reply = resp.text.strip()
            except Exception as e:
                print("Gemini error:", e)
                reply = f"AI 回覆失敗，請稍後再試  {e}"

            line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
            save_msg(uid, "bot", "text", reply)

        elif isinstance(incoming, StickerMessage):
            save_msg(uid, "user", "sticker", f"{incoming.package_id}:{incoming.sticker_id}")
            sticker = StickerSendMessage(package_id="11537", sticker_id="52002734")
            line_bot_api.reply_message(event.reply_token, sticker)
            save_msg(uid, "bot", "sticker", "default sticker")

        elif isinstance(incoming, (ImageMessage, VideoMessage, LocationMessage)):
            save_msg(uid, "user", incoming.type, "(binary)")
            reply = f"已收到 {incoming.type}！"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
            save_msg(uid, "bot", "text", reply)

    return "OK", 200

@app.route("/history/<uid>", methods=["GET", "DELETE"])
def history(uid):
    if request.method == "GET":
        return jsonify(fetch_history(uid, 50)), 200
    delete_history(uid)
    return jsonify({"status": "deleted"}), 200

@app.route("/")
def index():
    return "LINE Bot 已上線！", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
