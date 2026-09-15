"""
AxT CPR signal relay  —  TradingView → (filter) → Telegram group

What it does
------------
* TradingView fires EVERY signal to  POST /tv/<TV_SECRET>  with body {"slot":N,"text":"..."}
* This bot checks slot N's ON/OFF state (stored in Redis) and:
      - forwards the message to your group if the slot is ON
      - drops it silently if the slot is OFF
* You DM the bot  /alerts  → six tappable buttons (✅/⬜) to flip slots on/off.
  Changes apply instantly. You NEVER touch TradingView again to mute a slot.

Why Redis: Railway restarts the process on every deploy. In-memory state would
reset to all-ON silently. Redis keeps the six toggles across restarts.

Env vars (set in Railway → Variables):
    BOT_TOKEN     your bot token from @BotFather
    ADMIN_ID      your Telegram numeric id (from @userinfobot) — only you can toggle
    TARGET_CHAT   the group id, e.g. -1002268449482
    REDIS_URL     provided automatically by the Railway Redis add-on
    TV_SECRET     any random string — becomes part of the /tv/<...> path
    TG_SECRET     any random string — becomes part of the Telegram webhook path
    PUBLIC_URL    (optional) https://your-app.up.railway.app
                  If omitted, the bot uses Railway's RAILWAY_PUBLIC_DOMAIN.
"""

import os
import json
import logging

import redis
import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay")


# ── config ────────────────────────────────────────────────────────────────
def _req(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

BOT_TOKEN   = _req("BOT_TOKEN")
ADMIN_ID    = int(_req("ADMIN_ID"))
TARGET_CHAT = _req("TARGET_CHAT")
REDIS_URL   = _req("REDIS_URL")
TV_SECRET   = os.environ.get("TV_SECRET", "tv-change-me")
TG_SECRET   = os.environ.get("TG_SECRET", "tg-change-me")

PUBLIC_URL = os.environ.get("PUBLIC_URL")
if not PUBLIC_URL:
    dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    PUBLIC_URL = f"https://{dom}" if dom else ""
PUBLIC_URL = PUBLIC_URL.rstrip("/")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# display labels only — the actual slot is decided in Pine and sent in the payload
SLOT_LABELS = {
    1: "1:30 crypto · 3:30 gold",
    2: "5:30 crypto · 6:30 gold",
    3: "9:30 crypto · 10:30 gold",
    4: "13:30 crypto · 14:30 gold",
    5: "17:30 crypto · 18:30 gold",
    6: "21:30 crypto · 22:30 gold",
}

rdb = redis.from_url(REDIS_URL, decode_responses=True)
app = Flask(__name__)


# ── redis helpers (fail-OPEN: if Redis is down, send rather than silently drop) ──
def slot_on(n: int) -> bool:
    try:
        v = rdb.get(f"slot:{n}")
        return True if v is None else (v == "on")   # default ON
    except Exception as e:
        log.error("redis read failed (%s) — defaulting slot %s to ON", e, n)
        return True

def set_slot(n: int, on: bool):
    rdb.set(f"slot:{n}", "on" if on else "off")


# ── telegram helpers ──────────────────────────────────────────────────────
def tg(method: str, **params):
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=15).json()
    except Exception as e:
        log.error("telegram %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}

def dm_admin(text: str):
    tg("sendMessage", chat_id=ADMIN_ID, text=text, disable_web_page_preview=True)


# ── control panel (inline keyboard) ───────────────────────────────────────
def build_keyboard():
    rows = []
    for n in range(1, 7):
        mark = "✅" if slot_on(n) else "⬜"
        rows.append([{"text": f"{mark}  {SLOT_LABELS[n]}",
                      "callback_data": f"toggle:{n}"}])
    rows.append([{"text": "🔄 Refresh",     "callback_data": "refresh"},
                 {"text": "All ON",         "callback_data": "all_on"},
                 {"text": "All OFF",         "callback_data": "all_off"}])
    return {"inline_keyboard": rows}

def panel_text():
    on = [n for n in range(1, 7) if slot_on(n)]
    return ("<b>📡 Signal slots</b>\n"
            "Tap a slot to turn it ON/OFF. Applies instantly — no TradingView changes.\n"
            f"Currently ON: <b>{len(on)}/6</b>")


# ── routes ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return "ok", 200

@app.get("/")
def root():
    return "relay up", 200

@app.post(f"/tv/{TV_SECRET}")
def tv():
    raw = request.get_data(as_text=True)
    try:
        data = json.loads(raw)
    except Exception:
        log.warning("bad TV payload: %s", raw[:200])
        return "bad json", 400

    text = data.get("text", "")
    try:
        slot = int(data.get("slot"))
    except Exception:
        dm_admin(f"⚠️ Signal with no/invalid slot — dropped:\n{raw[:300]}")
        return "no slot", 200

    if slot < 1 or slot > 6:
        dm_admin(f"⚠️ Signal with out-of-range slot {slot} — dropped.")
        return "bad slot", 200

    if not slot_on(slot):
        log.info("slot %s OFF — dropped", slot)
        return "dropped", 200

    res = tg("sendMessage", chat_id=TARGET_CHAT, text=text,
             parse_mode="HTML", disable_web_page_preview=True)
    if not res.get("ok"):
        dm_admin(f"❌ Failed to forward slot {slot} to the group:\n{res}")
        return "forward failed", 200

    log.info("slot %s ON — forwarded", slot)
    return "sent", 200

@app.post(f"/telegram/{TG_SECRET}")
def telegram():
    upd = request.get_json(force=True, silent=True) or {}

    # /alerts or /start
    msg = upd.get("message")
    if msg:
        if msg.get("from", {}).get("id") != ADMIN_ID:
            return "ignored", 200                       # only admin
        txt = (msg.get("text") or "").strip()
        if txt.startswith("/alerts") or txt.startswith("/start"):
            tg("sendMessage", chat_id=ADMIN_ID, text=panel_text(),
               parse_mode="HTML", reply_markup=build_keyboard())
        return "ok", 200

    # button taps
    cq = upd.get("callback_query")
    if cq:
        cbid = cq.get("id")
        if cq.get("from", {}).get("id") != ADMIN_ID:
            tg("answerCallbackQuery", callback_query_id=cbid, text="Not allowed")
            return "ignored", 200
        data = cq.get("data", "")
        m = cq.get("message", {})
        chat_id = m.get("chat", {}).get("id")
        mid = m.get("message_id")

        note = "Updated"
        if data.startswith("toggle:"):
            n = int(data.split(":")[1])
            set_slot(n, not slot_on(n))
            note = f"Slot {n} → {'ON' if slot_on(n) else 'OFF'}"
        elif data == "all_on":
            for n in range(1, 7): set_slot(n, True)
            note = "All ON"
        elif data == "all_off":
            for n in range(1, 7): set_slot(n, False)
            note = "All OFF"

        tg("answerCallbackQuery", callback_query_id=cbid, text=note)
        tg("editMessageText", chat_id=chat_id, message_id=mid,
           text=panel_text(), parse_mode="HTML", reply_markup=build_keyboard())
        return "ok", 200

    return "ok", 200


# ── boot: register the Telegram webhook + ping the admin ───────────────────
def on_boot():
    if not PUBLIC_URL:
        log.error("PUBLIC_URL not set and RAILWAY_PUBLIC_DOMAIN missing — "
                  "cannot register Telegram webhook. Set PUBLIC_URL and redeploy.")
        return
    hook = f"{PUBLIC_URL}/telegram/{TG_SECRET}"
    res = tg("setWebhook", url=hook, allowed_updates=["message", "callback_query"])
    log.info("setWebhook -> %s : %s", hook, res)
    on = sorted(n for n in range(1, 7) if slot_on(n))
    dm_admin(f"✅ Relay online.\nSlots ON: {on}\nSend /alerts to manage.")

on_boot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
