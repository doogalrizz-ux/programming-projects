"""
Central config loader for HomeGhost.

Everything that could vary between environments (tokens, IDs, tunables)
lives in .env, never hardcoded here or in a skill module. Future skills
should import this module and, if they need their own settings, add them
here rather than reading os.environ directly.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int | None) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Optional: when set, slash commands sync instantly to this one guild
# instead of globally (global sync can take up to an hour to propagate).
GUILD_ID = _int_env("GUILD_ID", None)

REMINDER_CHANNEL_ID = _int_env("REMINDER_CHANNEL_ID", None)
ESCALATION_USER_ID = _int_env("ESCALATION_USER_ID", None)
REMINDER_INTERVAL_MINUTES = _int_env("REMINDER_INTERVAL_MINUTES", 5)
ESCALATION_THRESHOLD = _int_env("ESCALATION_THRESHOLD", 3)
DB_PATH = os.getenv("DB_PATH", "homeghost.db")

# --- Family scheduler ---
SCHEDULE_CHANNEL_ID = _int_env("SCHEDULE_CHANNEL_ID", None)
FAMILY_MEMBERS = [m.strip() for m in os.getenv("FAMILY_MEMBERS", "").split(",") if m.strip()]
# IANA timezone name (e.g. "America/New_York"). All schedule input is
# interpreted in this timezone; Discord then renders it in each viewer's
# own local time automatically via <t:...> timestamps.
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

# --- Shopping list ---
SHOPPING_CHANNEL_ID = _int_env("SHOPPING_CHANNEL_ID", None)
# First store in the list is the default when /shop add doesn't specify one.
SHOPPING_STORES = [s.strip() for s in os.getenv("SHOPPING_STORES", "").split(",") if s.strip()]

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
    )
if not REMINDER_CHANNEL_ID:
    raise RuntimeError(
        "REMINDER_CHANNEL_ID is not set in .env — the bot needs a channel to "
        "post laundry reminders in."
    )
if not SCHEDULE_CHANNEL_ID:
    raise RuntimeError(
        "SCHEDULE_CHANNEL_ID is not set in .env — the bot needs a channel for "
        "the family scheduler."
    )
if not FAMILY_MEMBERS:
    raise RuntimeError(
        "FAMILY_MEMBERS is not set in .env — give it a comma-separated roster, "
        "e.g. FAMILY_MEMBERS=Emma,Jack,Sam,Mom,Dad"
    )
if not SHOPPING_CHANNEL_ID:
    raise RuntimeError(
        "SHOPPING_CHANNEL_ID is not set in .env — the bot needs a channel for "
        "the shopping list."
    )
if not SHOPPING_STORES:
    raise RuntimeError(
        "SHOPPING_STORES is not set in .env — give it a comma-separated list, "
        "e.g. SHOPPING_STORES=ACME,SHOPRITE,BOOZESTORE (first one is the default)"
    )

# --- Chore list ---
CHORE_CHANNEL_ID = _int_env("CHORE_CHANNEL_ID", None)
if not CHORE_CHANNEL_ID:
    raise RuntimeError(
        "CHORE_CHANNEL_ID is not set in .env — the bot needs a channel for the chore list."
    )

# Optional: a subset of FAMILY_MEMBERS. Only used so @HG can expand group
# references like "the kids" or "everyone" into individual assignments --
# leaving it blank just means those phrasings won't resolve on their own.
KIDS = [k.strip() for k in os.getenv("KIDS", "").split(",") if k.strip()]
_unknown_kids = [k for k in KIDS if k not in FAMILY_MEMBERS]
if _unknown_kids:
    raise RuntimeError(
        f"KIDS contains names not in FAMILY_MEMBERS: {_unknown_kids} — check spelling matches exactly."
    )

# --- @HG natural-language assistant (optional) ---
# Unlike the other skills, this one is opt-in: leave GEMINI_API_KEY unset
# and bot.py simply skips loading cogs.assistant, no error. Everything
# else in HomeGhost works fine without it.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
# Cheap safety net against a bug (or abuse) causing a runaway loop of
# paid API calls -- not a realistic cap for normal household use.
ASSISTANT_MAX_DAILY_REQUESTS = _int_env("ASSISTANT_MAX_DAILY_REQUESTS", 500)
