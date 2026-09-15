"""
Central configuration + the platform registry.

Adding a new trading platform later = add one entry to PLATFORMS below.
No other code needs to change.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---- Telegram / infra (read lazily; only bot.py requires the token) ----
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
COMMUNITY_CHAT_ID = os.getenv("COMMUNITY_CHAT_ID", "")
ADMIN_ALERT_CHAT_ID = os.getenv("ADMIN_ALERT_CHAT_ID", "")
ADMIN_TELEGRAM_IDS = {
    x.strip() for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()
}
# The private admin group where admins drop the Excel/CSV to import IDs.
# The bot must be an ADMIN of this group to see uploaded files.
ADMIN_GROUP_CHAT_ID = os.getenv("ADMIN_GROUP_CHAT_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///verify_bot.db")

# The "Contact Support" button links here. Change via env to update without code.
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/OfficialAxT")

# ---- Broker sync credentials (set in Railway; never commit or log these) ----
COSMIC_AUTH = os.getenv("COSMIC_AUTH", "")     # full Authorization value, e.g. "Bearer eyJ..."
COSMIC_COOKIE = os.getenv("COSMIC_COOKIE", "")
SHARK_COOKIE = os.getenv("SHARK_COOKIE", "")   # initial value; /shark_session updates it in the DB
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "3600"))

RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_MAX_ATTEMPTS", "5"))
RATE_LIMIT_WINDOW_MINUTES = int(os.getenv("RATE_LIMIT_WINDOW_MINUTES", "10"))

SHARK_REMAP_TEMPLATE_ENV = os.getenv("SHARK_REMAP_TEMPLATE", "")

# ---- Referral links (public; env-overridable so you can rotate them without code changes) ----
DELTA_REFERRAL_LINK = os.getenv("DELTA_REFERRAL_LINK", "https://www.delta.exchange/?code=YTJQNI")
COSMIC_REFERRAL_LINK = os.getenv("COSMIC_REFERRAL_LINK", "https://cosmic.trade/register?ref=EX-93V6B1")
SHARK_REFERRAL_LINK = os.getenv("SHARK_REFERRAL_LINK", "https://sharkexchange.in/referral?code=XAP917")

# ---- Welcome message (broker selection). Disclosure folded in as the safety floor. ----
WELCOME_TEXT = (
    "👋 Welcome to the AxT Community! 🎉\n\n"
    "Access ke liye neeche apna broker select karo jahan aapka trading account hai 👇"
)

# ---- Community success message (shown to VERIFIED users) ----
# If set, verified users get THIS fixed link. Works even if the bot can't create links.
# A fixed link is shareable, so it does not stop forwarding to unverified people.
COMMUNITY_INVITE_LINK = os.getenv("COMMUNITY_INVITE_LINK", "")

DEFAULT_COMMUNITY_SUCCESS_TEXT = (
    "✅ Verify ho gaya! 🎉\n\n"
    "AxT Community me aapka swagat hai! Yahan aapko milega:\n"
    "• Market discussion aur updates 📈\n"
    "• Educational insights aur learning resources 📚\n"
    "• Community support 🤝\n\n"
    "Neeche button se abhi join karo 👇\n\n"
    "Note: Community benefits program ke terms aur activity par depend karte hain."
)
# Override the wording via env if you like; {invite_link} is filled in automatically.
COMMUNITY_SUCCESS_TEXT = os.getenv("COMMUNITY_SUCCESS_TEXT", "") or DEFAULT_COMMUNITY_SUCCESS_TEXT

# ---- Platform registry (the adapter system) ----
# not_found_action:
#   "ADMIN_ALERT"     -> notify admin only, generic "we'll follow up" to the user
#   "REMAP_TEMPLATE"  -> send the user the configured remap message (Shark)
#   "REFERRAL_PROMPT" -> show referral link + "already have one? we'll link it" AND alert admin
PLATFORMS = {
    "shark": {
        "display_name": "Shark",
        "uuid_label": "Shark User ID",
        "example": "737339",
        # digits only
        "pattern": r"^\d{3,}$",
        "not_found_action": "REMAP_TEMPLATE",
        "referral_link": SHARK_REFERRAL_LINK,
    },
    "cosmic": {
        "display_name": "Cosmic Trade",
        "uuid_label": "Cosmic User ID",
        "example": "I1004600",
        # optional single leading letter + digits
        "pattern": r"^[A-Z]?\d{3,}$",
        "not_found_action": "REFERRAL_PROMPT",
        "referral_link": COSMIC_REFERRAL_LINK,
    },
}


def resolve_platform_key(raw: str) -> str | None:
    """Map a messy platform label from Excel ('COSMIC EXCHANGE', 'Cosmic Trade')
    to an internal key ('cosmic'). Returns None if unrecognised."""
    if not raw:
        return None
    t = raw.strip().lower()
    if "delta" in t:
        return "delta"
    if "shark" in t:
        return "shark"
    if "cosmic" in t:
        return "cosmic"
    return None
