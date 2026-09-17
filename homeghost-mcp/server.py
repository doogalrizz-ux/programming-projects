"""
homeghost-mcp — a practice MCP server.

This exposes a read-only view of HomeGhost's own SQLite database (laundry
timers + family schedule) as MCP tools, a resource, and a prompt, so an
MCP client (Claude Desktop, Claude Code, the MCP Inspector, etc.) can
query "what's going on at home" without knowing anything about Discord
or the bot's internals.

Deliberately read-only: it opens the database with SQLite's `mode=ro`
URI flag, so this process is structurally incapable of writing to the
same file the live bot is using — no risk of two writers stepping on
each other. HomeGhost's own database.py already puts the DB in WAL mode,
which is specifically designed to let readers like this run concurrently
with the bot's writes, so this can run at the same time as the real bot
with no lock contention.

This intentionally duplicates a little date/time logic that also lives
in HomeGhost/cogs/scheduler.py rather than importing it, to keep this a
self-contained, single-file example to learn from. In a larger real
project you'd factor that into a shared package instead.
"""
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv()

DB_PATH = Path(os.getenv("HOMEGHOST_DB_PATH", "../HomeGhost/homeghost.db")).resolve()
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

server = MCPServer(name="homeghost")


# ------------------------------------------------------------------ helpers

def _connect_ro() -> sqlite3.Connection:
    """Open the HomeGhost database strictly read-only via a SQLite URI —
    this process can never accidentally write to the bot's live data."""
    uri = DB_PATH.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tz() -> ZoneInfo:
    return ZoneInfo(TIMEZONE)


def _today() -> date:
    return datetime.now(_tz()).date()


def _epoch_to_local_str(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=_tz()).strftime("%a %b %-d, %-I:%M %p") \
        if os.name != "nt" else datetime.fromtimestamp(epoch, tz=_tz()).strftime("%a %b %#d, %#I:%M %p")


def _local_dt(d: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_tz())


def _occurrences_in_range(rule: sqlite3.Row, range_start: date, range_end: date):
    """Expand a recurring rule into concrete (date, start_dt, end_dt)
    occurrences within a date range — same approach as scheduler.py."""
    starts_on = date.fromisoformat(rule["starts_on"]) if rule["starts_on"] else None
    ends_on = date.fromisoformat(rule["ends_on"]) if rule["ends_on"] else None
    lo = max(range_start, starts_on) if starts_on else range_start
    hi = min(range_end, ends_on) if ends_on else range_end
    if lo > hi:
        return
    days_ahead = (rule["weekday"] - lo.weekday()) % 7
    d = lo + timedelta(days=days_ahead)
    while d <= hi:
        yield d, _local_dt(d, rule["start_time"]), _local_dt(d, rule["end_time"])
        d += timedelta(days=7)


def _schedule_lines(range_start: date, range_end: date) -> list[str]:
    """Merge one-off events + expanded recurring occurrences within a
    date range into human-readable lines, sorted chronologically."""
    rows = []
    with _connect_ro() as conn:
        for event in conn.execute("SELECT * FROM schedule_events"):
            d = datetime.fromtimestamp(event["start_time"], tz=_tz()).date()
            if range_start <= d <= range_end:
                rows.append((event["start_time"], d, event["person"], event["category"],
                             event["title"], event["start_time"], event["end_time"]))
        for rule in conn.execute("SELECT * FROM schedule_recurring"):
            for occ_date, start_dt, end_dt in _occurrences_in_range(rule, range_start, range_end):
                rows.append((start_dt.timestamp(), occ_date, rule["person"], rule["category"],
                             rule["title"], start_dt.timestamp(), end_dt.timestamp()))
    rows.sort(key=lambda r: r[0])
    return [
        f"{d.strftime('%a %b')} {d.day} — {person}: {category} \"{title}\" "
        f"({_epoch_to_local_str(start_ts)}–{_epoch_to_local_str(end_ts).split(', ')[-1]})"
        for _, d, person, category, title, start_ts, end_ts in rows
    ]


# --------------------------------------------------------------------- tools

@server.tool()
def get_laundry_status() -> str:
    """Get every laundry timer that's currently running or finished and
    awaiting acknowledgment."""
    with _connect_ro() as conn:
        rows = conn.execute(
            "SELECT * FROM laundry_timers WHERE status IN ('running', 'done') ORDER BY start_time"
        ).fetchall()
    if not rows:
        return "No laundry timers running right now."
    lines = []
    for row in rows:
        if row["status"] == "running":
            finish = row["start_time"] + row["duration_minutes"] * 60
            lines.append(f"{row['appliance'].title()}: running, finishes at {_epoch_to_local_str(finish)}")
        else:
            lines.append(
                f"{row['appliance'].title()}: done, awaiting acknowledgment "
                f"({row['reminder_count']} reminder(s) sent)"
            )
    return "\n".join(lines)


@server.tool()
def get_todays_schedule() -> str:
    """Get every family event scheduled for today, one-off and recurring."""
    today = _today()
    lines = _schedule_lines(today, today)
    return "\n".join(lines) if lines else "Nothing on the schedule today."


@server.tool()
def get_week_schedule() -> str:
    """Get every family event scheduled for the next 7 days, one-off and recurring."""
    today = _today()
    lines = _schedule_lines(today, today + timedelta(days=6))
    return "\n".join(lines) if lines else "Nothing on the schedule this week."


@server.tool()
def search_events(query: str) -> str:
    """Search upcoming events (next 60 days) by person or title, case-insensitive substring match."""
    today = _today()
    lines = _schedule_lines(today, today + timedelta(days=60))
    needle = query.lower()
    matches = [line for line in lines if needle in line.lower()]
    return "\n".join(matches) if matches else f"No upcoming events match '{query}'."


# ------------------------------------------------------------------ resource

@server.resource("homeghost://status")
def household_status() -> str:
    """A standing snapshot of laundry + today's schedule, readable without
    explicitly calling a tool — this is what a Resource is for: passive
    context an MCP client can pull in, rather than an action it invokes."""
    return f"LAUNDRY:\n{get_laundry_status()}\n\nTODAY'S SCHEDULE:\n{get_todays_schedule()}"


# -------------------------------------------------------------------- prompt

@server.prompt()
def daily_briefing() -> str:
    """A reusable prompt template: summarize the household's current
    status in a short, friendly way."""
    return (
        "Using the homeghost://status resource and the get_week_schedule tool, "
        "give me a short, friendly morning briefing: anything in the laundry that "
        "needs attention, and today's + this week's family schedule highlights."
    )


if __name__ == "__main__":
    server.run(transport="stdio")
