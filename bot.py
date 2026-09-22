"""
AxT CPR signal relay  v3  —  TradingView -> (filter) -> Telegram group

Control model: PER-ASSET x PER-TIME grid.
  Each asset (BTC, ETH, SOL, Gold, Silver) has its own 4 scheduled-time
  toggles. They are independent: BTC 09:30 and ETH 09:30 are separate switches.
  The asset header button toggles all 4 of that asset's times at once.

  A signal is forwarded only if BOTH:
    (1) its asset+time toggle is ON, and
    (2) [if the freshness filter is ON] it arrived within WINDOW_MIN minutes
        of that scheduled time (late = stale = dropped).

Scheduled entry times (IST):
    crypto (BTC/ETH/SOL): 09:30 13:30 17:30 21:30   (UTC-anchored, fixed)
    metals (Gold/Silver): 10:30 14:30 18:30 22:30 summer / +1h in US winter
The slot a signal belongs to is inferred from ARRIVAL time (no Pine change).

Controls (admin DM): /alerts
State in Redis -> survives Railway restarts.

Env vars: BOT_TOKEN, ADMIN_ID, TARGET_CHAT, REDIS_URL, TV_SECRET, TG_SECRET,
          PUBLIC_URL (or RAILWAY_PUBLIC_DOMAIN), WINDOW_MIN (optional, default 30)
"""

import os
import json
import logging
import datetime as _dt
from datetime import datetime, timedelta, timezone

import redis
import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay")
IST = timezone(timedelta(hours=5, minutes=30))
CHK = "\u2705"   # green check
BOX = "\u2b1c"   # empty box


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

# Second destination: an extra group that receives ONLY a chosen slot (default
# slot 3 = crypto 17:30 / metals 18:30). Set INDIA_CHAT in Railway to that
# group's -100... id; leave it unset to disable. INDIA_SLOT_IDX is 0-based
# (0=first trade of day, 2=third). Asset toggles + freshness still apply.
INDIA_CHAT = os.environ.get("INDIA_CHAT", "").strip()
INDIA_SLOT_IDX = int(os.environ.get("INDIA_SLOT_IDX", "2"))   # 2 = slot 3

PUBLIC_URL = os.environ.get("PUBLIC_URL")
if not PUBLIC_URL:
    dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    PUBLIC_URL = f"https://{dom}" if dom else ""
PUBLIC_URL = PUBLIC_URL.rstrip("/")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# --- assets & DST-aware schedules -----------------------------------------
ASSETS = {
    "BTC": {"label": "BTC",    "group": "crypto", "match": ("BTC",)},
    "ETH": {"label": "ETH",    "group": "crypto", "match": ("ETH",)},
    "SOL": {"label": "SOL",    "group": "crypto", "match": ("SOL",)},
    "XAU": {"label": "Gold",   "group": "metals", "match": ("XAU", "GOLD")},
    "XAG": {"label": "Silver", "group": "metals", "match": ("XAG", "SILVER")},
}
ORDER = ("BTC", "ETH", "SOL", "XAU", "XAG")

# probability label per slot index (0..3) - same order for both groups
PROB = ["\U0001F7E2 Low probability trade",
        "\U0001F7E1 Medium probability trade",
        "\U0001F7E0 High probability trade",
        "\U0001F534 Extremely high probability trade"]

def _nth_sunday(year, month, n):
    w = _dt.date(year, month, 1).weekday()      # Mon=0 .. Sun=6
    return 1 + (6 - w) % 7 + (n - 1) * 7

def _us_dst(now_ist):
    y = now_ist.year
    start = _dt.date(y, 3, _nth_sunday(y, 3, 2))
    end   = _dt.date(y, 11, _nth_sunday(y, 11, 1))
    return start <= now_ist.date() < end

def schedule_for(group, now_ist):
    if group == "crypto":
        return [9*60+30, 13*60+30, 17*60+30, 21*60+30]
    if _us_dst(now_ist):
        return [10*60+30, 14*60+30, 18*60+30, 22*60+30]   # summer
    return [11*60+30, 15*60+30, 19*60+30, 23*60+30]       # winter

def slot_and_lateness(group, now_ist):
    now_min = now_ist.hour * 60 + now_ist.minute
    sched = schedule_for(group, now_ist)
    earlier = [(i, t) for i, t in enumerate(sched) if t <= now_min]
    if earlier:
        i, t = max(earlier, key=lambda x: x[1])
        late = now_min - t
    else:
        i = len(sched) - 1
        t = sched[i]
        late = now_min + (1440 - t)
    hh, mm = divmod(t, 60)
    return i, late, f"{hh:02d}:{mm:02d}"

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

def slot_on(asset, idx):        return _get_bool(f"slot:{asset}:{idx}")
def set_slot(asset, idx, on):   rdb.set(f"slot:{asset}:{idx}", "on" if on else "off")
def window_on():                return _get_bool("cfg:window", True)
def set_window(on):             rdb.set("cfg:window", "on" if on else "off")
def india_on():                 return _get_bool("cfg:india", True)   # India group forwarding on/off
def set_india(on):              rdb.set("cfg:india", "on" if on else "off")
def india_slot_on(i):           return _get_bool(f"cfg:india:slot:{i}", i == INDIA_SLOT_IDX)  # default: only slot 3 ON
def set_india_slot(i, on):      rdb.set(f"cfg:india:slot:{i}", "on" if on else "off")


# --- telegram helpers -----------------------------------------------------
def tg(method, **params):
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=15).json()
    except Exception as e:
        log.error("telegram %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}

def dm_admin(text):
    tg("sendMessage", chat_id=ADMIN_ID, text=text, disable_web_page_preview=True)


# --- control panel (per-asset x per-time grid) ----------------------------
def build_keyboard(now_ist):
    rows = []
    for key in ORDER:
        grp = ASSETS[key]["group"]
        sched = schedule_for(grp, now_ist)
        onc = sum(1 for i in range(4) if slot_on(key, i))
        rows.append([{"text": f"-- {ASSETS[key]['label']}  ({onc}/4) --",
                      "callback_data": f"a:{key}"}])
        trow = []
        for i, t in enumerate(sched):
            hh, mm = divmod(t, 60)
            mark = CHK if slot_on(key, i) else BOX
            trow.append({"text": f"{mark} {hh:02d}:{mm:02d}",
                         "callback_data": f"t:{key}:{i}"})
        rows.append(trow)
    wmark = CHK if window_on() else BOX
    rows.append([{"text": f"{wmark} {WINDOW_MIN}-min freshness",
                  "callback_data": "window"}])
    if INDIA_CHAT:
        imark = CHK if india_on() else BOX
        rows.append([{"text": f"{imark} -- India group (master) --",
                      "callback_data": "india"}])
        # one on/off per slot for the India group; labels show crypto / metal time
        c = schedule_for("crypto", now_ist)
        mt = schedule_for("metals", now_ist)
        irow = []
        for i in range(4):
            ch, cm = divmod(c[i], 60)
            mh, mm = divmod(mt[i], 60)
            mark = CHK if india_slot_on(i) else BOX
            irow.append({"text": f"{mark} {ch:02d}:{cm:02d}/{mh:02d}:{mm:02d}",
                         "callback_data": f"islot:{i}"})
        rows.append(irow)
    rows.append([{"text": "Refresh",  "callback_data": "refresh"},
                 {"text": "All ON",   "callback_data": "all_on"},
                 {"text": "All OFF",  "callback_data": "all_off"}])
    return {"inline_keyboard": rows}

def panel_text():
    total = sum(1 for k in ORDER for i in range(4) if slot_on(k, i))
    win = "ON" if window_on() else "OFF"
    india = ("   |   India: <b>" + ("ON" if india_on() else "OFF") + "</b>") if INDIA_CHAT else ""
    return ("<b>Relay controls</b>\n"
            "Tap a time to send/mute that asset's trade. Tap an asset header to "
            "flip all 4 of its times. Applies instantly - no TradingView changes.\n"
            f"Trades ON: <b>{total}/20</b>   |   Freshness ({WINDOW_MIN} min): <b>{win}</b>{india}")


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

    group = ASSETS[key]["group"]
    idx, late, sched = slot_and_lateness(group, datetime.now(IST))

    if window_on() and late > WINDOW_MIN:
        log.info("%s stale: %d min past %s - dropped", key, late, sched)
        return "stale", 200

    if not slot_on(key, idx):
        log.info("%s slot %s (%s) OFF - dropped", key, idx, sched)
        return "slot off", 200

    # append probability label (keyed to the slot index, DST-safe)
    out_text = text
    if 0 <= idx < len(PROB):
        out_text = text + "\n" + PROB[idx]

    res = tg("sendMessage", chat_id=TARGET_CHAT, text=out_text,
             parse_mode="HTML", disable_web_page_preview=True)
    if not res.get("ok"):
        dm_admin(f"Failed to forward {ASSETS[key]['label']} {sched} signal:\n{res}")
        return "forward failed", 200

    log.info("%s slot %s (%s) forwarded", key, idx, sched)

    # Extra destination: send the chosen slot to the India community group too.
    if INDIA_CHAT and india_on() and india_slot_on(idx):
        r2 = tg("sendMessage", chat_id=INDIA_CHAT, text=out_text,
                parse_mode="HTML", disable_web_page_preview=True)
        if not r2.get("ok"):
            dm_admin(f"Failed to forward {ASSETS[key]['label']} {sched} to India group:\n{r2}")
        else:
            log.info("%s slot %s (%s) also sent to India group", key, idx, sched)

    return "sent", 200

@app.post(f"/telegram/{TG_SECRET}")
def telegram():
    upd = request.get_json(force=True, silent=True) or {}
    now = datetime.now(IST)

    msg = upd.get("message")
    if msg:
        if msg.get("from", {}).get("id") != ADMIN_ID:
            return "ignored", 200
        txt = (msg.get("text") or "").strip()
        if txt.startswith("/alerts") or txt.startswith("/start"):
            tg("sendMessage", chat_id=ADMIN_ID, text=panel_text(),
               parse_mode="HTML", reply_markup=build_keyboard(now))
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
        if data.startswith("t:"):
            _, k, i = data.split(":")
            i = int(i)
            set_slot(k, i, not slot_on(k, i))
            note = f"{ASSETS[k]['label']} slot {i+1} -> {'ON' if slot_on(k,i) else 'OFF'}"
        elif data.startswith("a:"):
            k = data.split(":")[1]
            all_on = all(slot_on(k, i) for i in range(4))
            for i in range(4):
                set_slot(k, i, not all_on)
            note = f"{ASSETS[k]['label']} all {'OFF' if all_on else 'ON'}"
        elif data == "window":
            set_window(not window_on())
            note = f"Freshness -> {'ON' if window_on() else 'OFF'}"
        elif data == "india":
            set_india(not india_on())
            note = f"India group -> {'ON' if india_on() else 'OFF'}"
        elif data.startswith("islot:"):
            i = int(data.split(":")[1])
            set_india_slot(i, not india_slot_on(i))
            note = f"India slot {i+1} -> {'ON' if india_slot_on(i) else 'OFF'}"
        elif data == "all_on":
            for k in ORDER:
                for i in range(4): set_slot(k, i, True)
            note = "All ON"
        elif data == "all_off":
            for k in ORDER:
                for i in range(4): set_slot(k, i, False)
            note = "All OFF"

        tg("answerCallbackQuery", callback_query_id=cbid, text=note)
        tg("editMessageText", chat_id=chat_id, message_id=mid,
           text=panel_text(), parse_mode="HTML", reply_markup=build_keyboard(now))
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
    total = sum(1 for k in ORDER for i in range(4) if slot_on(k, i))
    india = f"ON (slot {INDIA_SLOT_IDX+1})" if INDIA_CHAT else "OFF"
    dm_admin(f"Relay v3 online.\nTrades ON: {total}/20\n"
             f"Freshness: {'ON' if window_on() else 'OFF'} ({WINDOW_MIN} min)\n"
             f"India group: {india}\n"
             f"Send /alerts to manage.")

on_boot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
