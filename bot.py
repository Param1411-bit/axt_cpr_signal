"""
AxT CPR signal relay  v2  —  TradingView -> (filter) -> Telegram group

Adds on top of v1:
  * Per-ASSET on/off toggles (BTC, ETH, SOL, XAU, XAG)
  * A 30-minute FRESHNESS WINDOW: a signal is only forwarded if it arrives
    within WINDOW_MIN minutes of that asset's scheduled entry time. Late = dropped.

Scheduled entry times (IST), by asset group:
    crypto (BTC/ETH/SOL): 09:30, 13:30, 17:30, 21:30
    metals (XAU/XAG):     10:30, 14:30, 18:30, 22:30
The signal's scheduled slot is inferred from its ARRIVAL time (no Pine change
needed): we take the most recent scheduled time at/before arrival for that asset
group; if arrival is more than WINDOW_MIN minutes past it, the signal is stale.

Controls (DM the bot, admin only):  /alerts
Redis holds all state so it survives Railway restarts.

Env vars (Railway -> Variables):
    BOT_TOKEN, ADMIN_ID, TARGET_CHAT, REDIS_URL, TV_SECRET, TG_SECRET
    PUBLIC_URL   (or Railway's RAILWAY_PUBLIC_DOMAIN)
    WINDOW_MIN   (optional, default 30)
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

import redis
import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay")
IST = timezone(timedelta(hours=5, minutes=30))


# --- config ---------------------------------------------------------------
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
WINDOW_MIN  = int(os.environ.get("WINDOW_MIN", "30"))

PUBLIC_URL = os.environ.get("PUBLIC_URL")
if not PUBLIC_URL:
    dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    PUBLIC_URL = f"https://{dom}" if dom else ""
PUBLIC_URL = PUBLIC_URL.rstrip("/")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# --- assets & schedules ---------------------------------------------------
ASSETS = {
    "BTC": {"label": "BTC",    "group": "crypto", "match": ("BTC",)},
    "ETH": {"label": "ETH",    "group": "crypto", "match": ("ETH",)},
    "SOL": {"label": "SOL",    "group": "crypto", "match": ("SOL",)},
    "XAU": {"label": "Gold",   "group": "metals", "match": ("XAU", "GOLD")},
    "XAG": {"label": "Silver", "group": "metals", "match": ("XAG", "SILVER")},
}
import datetime as _dt

def _nth_sunday(year, month, n):
    w = _dt.date(year, month, 1).weekday()      # Mon=0 .. Sun=6
    first_sun = 1 + (6 - w) % 7
    return first_sun + (n - 1) * 7

def _us_dst(now_ist):
    # US daylight time: 2nd Sun March .. 1st Sun November
    y = now_ist.year
    start = _dt.date(y, 3, _nth_sunday(y, 3, 2))
    end   = _dt.date(y, 11, _nth_sunday(y, 11, 1))
    return start <= now_ist.date() < end

def schedule_for(group, now_ist):
    if group == "crypto":
        # Coinbase is UTC-anchored -> fixed year round
        return [9*60+30, 13*60+30, 17*60+30, 21*60+30]   # 09:30 13:30 17:30 21:30
    # OANDA metals are NY-session anchored -> shift +1h in US winter
    if _us_dst(now_ist):
        return [10*60+30, 14*60+30, 18*60+30, 22*60+30]  # summer 10:30 14:30 18:30 22:30
    return [11*60+30, 15*60+30, 19*60+30, 23*60+30]      # winter 11:30 15:30 19:30 23:30

def asset_key_from_symbol(sym):
    s = (sym or "").upper()
    for key, cfg in ASSETS.items():
        if any(m in s for m in cfg["match"]):
            return key
    return None


# --- redis state (fail-OPEN on read errors) -------------------------------
rdb = redis.from_url(REDIS_URL, decode_responses=True)

def _get_bool(key, default=True):
    try:
        v = rdb.get(key)
        return default if v is None else (v == "on")
    except Exception as e:
        log.error("redis read %s failed (%s) - default %s", key, e, default)
        return default

def asset_on(key):        return _get_bool(f"asset:{key}")
def set_asset(key, on):   rdb.set(f"asset:{key}", "on" if on else "off")
def window_on():          return _get_bool("cfg:window", True)
def set_window(on):       rdb.set("cfg:window", "on" if on else "off")


# --- telegram helpers -----------------------------------------------------
def tg(method, **params):
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=15).json()
    except Exception as e:
        log.error("telegram %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}

def dm_admin(text):
    tg("sendMessage", chat_id=ADMIN_ID, text=text, disable_web_page_preview=True)


# --- freshness check ------------------------------------------------------
def minutes_late(group, now_ist):
    now_min = now_ist.hour * 60 + now_ist.minute
    sched = schedule_for(group, now_ist)
    earlier = [t for t in sched if t <= now_min]
    if earlier:
        t = max(earlier)
        late = now_min - t
    else:
        t = max(sched)               # before first slot today -> yesterday's last
        late = now_min + (1440 - t)
    hh, mm = divmod(t, 60)
    return late, f"{hh:02d}:{mm:02d}"


# --- control panel --------------------------------------------------------
def build_keyboard():
    rows = [[{"text": "-- Assets --", "callback_data": "noop"}]]
    for key in ("BTC", "ETH", "SOL", "XAU", "XAG"):
        mark = "\u2705" if asset_on(key) else "\u2b1c"
        rows.append([{"text": f"{mark}  {ASSETS[key]['label']}",
                      "callback_data": f"asset:{key}"}])
    wmark = "\u2705" if window_on() else "\u2b1c"
    rows.append([{"text": f"{wmark}  {WINDOW_MIN}-min freshness filter",
                  "callback_data": "window"}])
    rows.append([{"text": "Refresh", "callback_data": "refresh"},
                 {"text": "All ON",  "callback_data": "all_on"},
                 {"text": "All OFF",  "callback_data": "all_off"}])
    return {"inline_keyboard": rows}

def panel_text():
    a_on = [ASSETS[k]["label"] for k in ("BTC","ETH","SOL","XAU","XAG") if asset_on(k)]
    win  = "ON" if window_on() else "OFF"
    return ("<b>Relay controls</b>\n"
            "Tap an asset to send/mute it. Applies instantly - no TradingView changes.\n"
            f"Assets ON: <b>{', '.join(a_on) or 'none'}</b>\n"
            f"Freshness filter ({WINDOW_MIN} min after scheduled time): <b>{win}</b>")


# --- app & routes ---------------------------------------------------------
app = Flask(__name__)

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
    key = asset_key_from_symbol(text)
    if key is None:
        dm_admin(f"Could not identify asset in signal - dropped:\n{text[:200]}")
        return "unknown asset", 200

    if not asset_on(key):
        log.info("asset %s OFF - dropped", key)
        return "asset off", 200

    if window_on():
        group = ASSETS[key]["group"]
        late, sched = minutes_late(group, datetime.now(IST))
        if late > WINDOW_MIN:
            log.info("%s stale: %d min past %s - dropped", key, late, sched)
            return "stale", 200

    res = tg("sendMessage", chat_id=TARGET_CHAT, text=text,
             parse_mode="HTML", disable_web_page_preview=True)
    if not res.get("ok"):
        dm_admin(f"Failed to forward {ASSETS[key]['label']} signal:\n{res}")
        return "forward failed", 200

    log.info("%s forwarded", key)
    return "sent", 200

@app.post(f"/telegram/{TG_SECRET}")
def telegram():
    upd = request.get_json(force=True, silent=True) or {}

    msg = upd.get("message")
    if msg:
        if msg.get("from", {}).get("id") != ADMIN_ID:
            return "ignored", 200
        txt = (msg.get("text") or "").strip()
        if txt.startswith("/alerts") or txt.startswith("/start"):
            tg("sendMessage", chat_id=ADMIN_ID, text=panel_text(),
               parse_mode="HTML", reply_markup=build_keyboard())
        return "ok", 200

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
        if data.startswith("asset:"):
            k = data.split(":")[1]
            set_asset(k, not asset_on(k))
            note = f"{ASSETS[k]['label']} -> {'ON' if asset_on(k) else 'OFF'}"
        elif data == "window":
            set_window(not window_on())
            note = f"Freshness -> {'ON' if window_on() else 'OFF'}"
        elif data == "all_on":
            for k in ASSETS: set_asset(k, True)
            note = "All assets ON"
        elif data == "all_off":
            for k in ASSETS: set_asset(k, False)
            note = "All assets OFF"
        elif data == "noop":
            note = " "

        tg("answerCallbackQuery", callback_query_id=cbid, text=note)
        if data != "noop":
            tg("editMessageText", chat_id=chat_id, message_id=mid,
               text=panel_text(), parse_mode="HTML", reply_markup=build_keyboard())
        return "ok", 200

    return "ok", 200


# --- boot -----------------------------------------------------------------
def on_boot():
    if not PUBLIC_URL:
        log.error("PUBLIC_URL/RAILWAY_PUBLIC_DOMAIN missing - cannot set webhook.")
        return
    hook = f"{PUBLIC_URL}/telegram/{TG_SECRET}"
    res = tg("setWebhook", url=hook, allowed_updates=["message", "callback_query"])
    log.info("setWebhook -> %s : %s", hook, res)
    a_on = [ASSETS[k]["label"] for k in ASSETS if asset_on(k)]
    dm_admin(f"Relay v2 online.\nAssets ON: {a_on}\n"
             f"Freshness: {'ON' if window_on() else 'OFF'} ({WINDOW_MIN} min)\n"
             f"Send /alerts to manage.")

on_boot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
