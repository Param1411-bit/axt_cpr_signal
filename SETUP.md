# AxT CPR Signal Relay — Setup

TradingView sends **every** signal to this bot. The bot forwards only the slots
you have turned **ON** to your Telegram group. You toggle slots by DMing the bot
`/alerts`. After this is set up, **you never touch TradingView again** to mute a slot.

```
TradingView (5 alerts) --webhook--> Railway bot --(if slot ON)--> Telegram group
                                         ^
                                    you: /alerts  (tap ✅/⬜)
```

---

## 1. Create the bot (if you haven't)
- @BotFather → `/newbot` → save the **BOT_TOKEN**.

## 2. Get the values you'll need
- **ADMIN_ID** — DM @userinfobot → it replies with your numeric id (e.g. `123456789`).
- **TARGET_CHAT** — your group id, the `-100…` number (you already have `-1002268449482`).
- **TV_SECRET** and **TG_SECRET** — invent two random strings (e.g. `k7Qp2xR9` and `m3Zt8LbW`). They just make the URLs unguessable.

## 3. Deploy on Railway
1. Put these four files in a GitHub repo (or use `railway up` from the folder):
   `bot.py`, `requirements.txt`, `Procfile`, `SETUP.md`.
2. Railway → **New Project → Deploy from GitHub repo** (or Empty → push).
3. In the project, **+ New → Database → Add Redis**. Railway creates a `REDIS_URL`.
4. Open your **service → Variables** and add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | your bot token |
| `ADMIN_ID` | your numeric Telegram id |
| `TARGET_CHAT` | `-1002268449482` |
| `TV_SECRET` | your random string #1 |
| `TG_SECRET` | your random string #2 |
| `REDIS_URL` | **reference** the Redis service's `REDIS_URL` (Railway: "Add reference") |

5. Under **Settings → Networking**, make sure a **public domain** is generated
   (e.g. `your-app.up.railway.app`).
6. Add one more variable so the bot can register its Telegram webhook:

| Variable | Value |
|---|---|
| `PUBLIC_URL` | `https://your-app.up.railway.app` |

   *(If you skip this, the bot tries Railway's built-in `RAILWAY_PUBLIC_DOMAIN`;
   setting `PUBLIC_URL` explicitly is the reliable path.)*

7. **Redeploy.** On boot the bot registers its Telegram webhook and DMs you
   **"✅ Relay online."** If you don't get that DM, check the deploy logs.

## 4. Point TradingView at the relay (last alert rebuild ever)
On **each** of the 5 charts (BTC, ETH, SOL, XAUUSD, XAGUSD):
- Delete the old alert, create a new one:
  - Condition: **AxT CPR ⒻⒷ → Any alert() function call**
  - **Message box: EMPTY**
  - Notifications → **Webhook URL**:
    ```
    https://your-app.up.railway.app/tv/YOUR_TV_SECRET
    ```
- Also paste the new indicator script (`AxT_CPR_relay_v14.txt`) and Save first,
  so the payload includes the slot number.

## 5. Use it
- DM the bot **`/alerts`** → six buttons appear. Tap any to flip ✅ (send) / ⬜ (mute).
- Also **All ON** / **All OFF** / **Refresh** buttons.
- Changes are instant and saved in Redis (survive restarts). No TradingView edits.

---

## Health & reliability
- **Uptime check:** point UptimeRobot (or similar) at
  `https://your-app.up.railway.app/health` — it returns `ok`. If it goes down,
  you get alerted instead of discovering it from a missed signal.
- **Deploys drop signals:** while the service restarts (a few seconds on redeploy),
  a signal arriving in that window is lost — TradingView fires once and does not
  retry. Deploy during quiet hours. The startup DM tells you when it's back.
- **Redis down = fail-open:** if Redis is unreachable, the bot forwards signals
  (better than silently dropping all of them) and logs the error.
- **Only you can toggle:** the bot ignores `/alerts` and button taps from anyone
  whose id ≠ `ADMIN_ID`.

## Security note
The `/tv/<TV_SECRET>` path is the only thing protecting your group from spoofed
signals. Keep `TV_SECRET` private (it lives only in your TradingView webhook URL).
Rotate it by changing the env var and updating the 5 webhook URLs.
