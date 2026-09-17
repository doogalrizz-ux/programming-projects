"""
Family scheduler skill: a shared calendar for appointments, practices,
work shifts, and anything else, entered entirely through slash commands
in one dedicated channel — no email/text parsing, no NLP.

Design choices worth knowing about:
- Two tables: `schedule_events` for one-off entries (a dentist visit),
  `schedule_recurring` for standing weekly rules (karate every Tuesday).
  Recurring occurrences are never pre-generated into rows — they're
  expanded on the fly from the rule whenever something needs to look at
  a date range (listing, conflict-checking, reminders). That means
  editing a standing rule's time instantly applies to every future
  week with no sync step.
- All times are entered/interpreted in the household TIMEZONE (config)
  and stored as UTC epoch seconds. Discord then renders <t:...> timestamps
  in each viewer's own local time automatically.
- Conflict checking is household-wide, not per-person: two different
  kids' events overlapping matters just as much as one person double
  booked, since either way someone has to be in two places at once.
  It's a warning, not a block — plenty of real conflicts (two parents
  splitting up) are fine.
- One-off events and recurring rules live in separate ID spaces, so
  listings tag them distinctly: "#12" (one-off, use with
  /schedule remove or /schedule edit) vs "R#12" (recurring rule, use
  with /schedule remove-recurring or /schedule edit-recurring).
- Reminders fire once per occurrence (tracked via `reminder_sent` for
  one-off events, `last_reminder_occurrence_date` for recurring ones)
  so a restart or a slow tick never double-pings.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import discord
from dateutil import parser as dateutil_parser
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger("homeghost.scheduler")

MENTION_RE = re.compile(r"<@!?(\d+)>")
MAX_REMINDER_MINUTES = 24 * 60

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_TO_INT = {name: i for i, name in enumerate(WEEKDAY_NAMES)}

CategoryLiteral = Literal["Doctor", "Karate", "Practice", "Meet", "Work", "School", "Other"]
WeekdayLiteral = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

PERSON_CHOICES = [app_commands.Choice(name=m, value=m) for m in config.FAMILY_MEMBERS]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schedule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    person TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    location TEXT,
    remind_minutes_before INTEGER,
    remind_user_ids TEXT,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_recurring (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    person TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    location TEXT,
    starts_on TEXT,
    ends_on TEXT,
    remind_minutes_before INTEGER,
    remind_user_ids TEXT,
    last_reminder_occurrence_date TEXT,
    created_by INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""


# ------------------------------------------------------------------ helpers

def _tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def _today() -> date:
    return datetime.now(_tz()).date()


def _epoch_to_local_date(epoch: float) -> date:
    return datetime.fromtimestamp(epoch, tz=_tz()).date()


def _epoch_to_local_hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=_tz()).strftime("%H:%M")


def _local_dt(d: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_tz())


def _parse_local_datetime(date_str: str, time_str: str) -> datetime:
    naive = dateutil_parser.parse(f"{date_str} {time_str}")
    return naive.replace(tzinfo=_tz())


def _parse_time_to_hhmm(time_str: str) -> str:
    return dateutil_parser.parse(time_str).strftime("%H:%M")


def _parse_date_to_iso(date_str: str) -> str:
    return dateutil_parser.parse(date_str).date().isoformat()


def _parse_remind_users(remind_users: Optional[str]) -> list[int]:
    if not remind_users:
        return []
    return [int(uid) for uid in MENTION_RE.findall(remind_users)]


def _fmt_time_range(start_ts: float, end_ts: float) -> str:
    return f"<t:{int(start_ts)}:t>–<t:{int(end_ts)}:t>"


def _occurrences_in_range(rule: dict, range_start: date, range_end: date):
    """Yield (occurrence_date, start_epoch, end_epoch) for every date in
    [range_start, range_end] that matches the rule's weekday and falls
    within its starts_on/ends_on bounds (if set)."""
    starts_on = date.fromisoformat(rule["starts_on"]) if rule["starts_on"] else None
    ends_on = date.fromisoformat(rule["ends_on"]) if rule["ends_on"] else None
    lo = max(range_start, starts_on) if starts_on else range_start
    hi = min(range_end, ends_on) if ends_on else range_end
    if lo > hi:
        return
    days_ahead = (rule["weekday"] - lo.weekday()) % 7
    d = lo + timedelta(days=days_ahead)
    while d <= hi:
        start_dt = _local_dt(d, rule["start_time"])
        end_dt = _local_dt(d, rule["end_time"])
        yield d, start_dt.timestamp(), end_dt.timestamp()
        d += timedelta(days=7)


def in_schedule_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != config.SCHEDULE_CHANNEL_ID:
            await interaction.response.send_message(
                f"Please use scheduling commands in <#{config.SCHEDULE_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return False
        return True

    return app_commands.check(predicate)


class Scheduler(commands.Cog):
    """Slash-command family calendar with conflict warnings and reminders."""

    schedule_group = app_commands.Group(
        name="schedule", description="Family scheduling commands"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------------------------------------------------------- setup

    async def cog_load(self):
        # sqlite3/aiosqlite reject multiple statements in one execute() call,
        # so run each CREATE TABLE separately.
        for statement in SCHEMA_SQL.strip().split(";"):
            statement = statement.strip()
            if statement:
                await self.bot.db.execute(statement)
        await self.bot.db.commit()
        self.check_reminders.start()

    async def cog_unload(self):
        self.check_reminders.cancel()

    # --------------------------------------------------------------- shared

    def _validate_reminder(
        self, remind_minutes_before: Optional[int], remind_ids: list[int]
    ) -> Optional[str]:
        has_time = remind_minutes_before is not None
        has_users = bool(remind_ids)
        if has_time and not has_users:
            return "You set `remind_minutes_before` but didn't mention anyone in `remind_users`."
        if has_users and not has_time:
            return "You mentioned reminder recipients but didn't set `remind_minutes_before`."
        if has_time and not (0 < remind_minutes_before <= MAX_REMINDER_MINUTES):
            return f"`remind_minutes_before` must be between 1 and {MAX_REMINDER_MINUTES}."
        return None

    async def _find_conflicts(
        self,
        guild_id: int,
        occ_date: date,
        start_ts: float,
        end_ts: float,
        exclude_event_id: Optional[int] = None,
        exclude_recurring_id: Optional[int] = None,
    ) -> list[str]:
        conflicts = []

        async with self.bot.db.execute(
            "SELECT * FROM schedule_events WHERE guild_id=?", (guild_id,)
        ) as cur:
            events = await cur.fetchall()
        for row in events:
            if exclude_event_id is not None and row["id"] == exclude_event_id:
                continue
            if _epoch_to_local_date(row["start_time"]) != occ_date:
                continue
            if start_ts < row["end_time"] and end_ts > row["start_time"]:
                conflicts.append(
                    f"{row['person']}'s {row['title']} "
                    f"({_fmt_time_range(row['start_time'], row['end_time'])})"
                )

        async with self.bot.db.execute(
            "SELECT * FROM schedule_recurring WHERE guild_id=?", (guild_id,)
        ) as cur:
            rules = await cur.fetchall()
        for rule in rules:
            if exclude_recurring_id is not None and rule["id"] == exclude_recurring_id:
                continue
            for _, occ_start, occ_end in _occurrences_in_range(rule, occ_date, occ_date):
                if start_ts < occ_end and end_ts > occ_start:
                    conflicts.append(
                        f"{rule['person']}'s {rule['title']} ({_fmt_time_range(occ_start, occ_end)})"
                    )

        return conflicts

    async def _gather_occurrences(
        self, guild_id: int, range_start: date, range_end: date, person: Optional[str] = None
    ) -> list[dict]:
        occurrences: list[dict] = []

        query = "SELECT * FROM schedule_events WHERE guild_id=?"
        params: list = [guild_id]
        if person:
            query += " AND person=?"
            params.append(person)
        async with self.bot.db.execute(query, params) as cur:
            events = await cur.fetchall()
        for row in events:
            d = _epoch_to_local_date(row["start_time"])
            if range_start <= d <= range_end:
                occurrences.append(
                    {
                        "date": d,
                        "start_ts": row["start_time"],
                        "end_ts": row["end_time"],
                        "person": row["person"],
                        "category": row["category"],
                        "title": row["title"],
                        "location": row["location"],
                        "remind_minutes_before": row["remind_minutes_before"],
                        "kind": "event",
                        "ref_id": row["id"],
                    }
                )

        query = "SELECT * FROM schedule_recurring WHERE guild_id=?"
        params = [guild_id]
        if person:
            query += " AND person=?"
            params.append(person)
        async with self.bot.db.execute(query, params) as cur:
            rules = await cur.fetchall()
        for rule in rules:
            for d, start_ts, end_ts in _occurrences_in_range(rule, range_start, range_end):
                occurrences.append(
                    {
                        "date": d,
                        "start_ts": start_ts,
                        "end_ts": end_ts,
                        "person": rule["person"],
                        "category": rule["category"],
                        "title": rule["title"],
                        "location": rule["location"],
                        "remind_minutes_before": rule["remind_minutes_before"],
                        "kind": "recurring",
                        "ref_id": rule["id"],
                    }
                )

        occurrences.sort(key=lambda o: o["start_ts"])
        return occurrences

    @staticmethod
    def _render_line(occ: dict) -> str:
        tag = f"#{occ['ref_id']}" if occ["kind"] == "event" else f"R#{occ['ref_id']}"
        bell = " 🔔" if occ["remind_minutes_before"] else ""
        loc = f" @ {occ['location']}" if occ["location"] else ""
        return (
            f"`{tag}` {_fmt_time_range(occ['start_ts'], occ['end_ts'])} "
            f"**{occ['person']}** — {occ['category']}: {occ['title']}{loc}{bell}"
        )

    def _render_day_grouped(self, occurrences: list[dict]) -> str:
        if not occurrences:
            return "Nothing scheduled."
        lines = []
        current_day = None
        for occ in occurrences:
            if occ["date"] != current_day:
                current_day = occ["date"]
                lines.append(f"\n**{current_day.strftime('%A, %B')} {current_day.day}**")
            lines.append(self._render_line(occ))
        return "\n".join(lines).strip()

    # -------------------------------------------------------------- add/edit

    @schedule_group.command(name="add", description="Add a one-off event")
    @app_commands.describe(
        person="Who this event is for",
        category="Event type",
        title="Short description, e.g. 'Dentist checkup'",
        date="Date, e.g. 2026-09-15",
        start="Start time, e.g. 3:00pm",
        end="End time, e.g. 4:00pm",
        location="Optional location",
        remind_minutes_before="Minutes before start to send a reminder",
        remind_users="Mention everyone who should be pinged, e.g. @Mom @Dad",
    )
    @app_commands.choices(person=PERSON_CHOICES)
    @in_schedule_channel()
    async def add(
        self,
        interaction: discord.Interaction,
        person: str,
        category: CategoryLiteral,
        title: str,
        date: str,
        start: str,
        end: str,
        location: Optional[str] = None,
        remind_minutes_before: Optional[int] = None,
        remind_users: Optional[str] = None,
    ):
        remind_ids = _parse_remind_users(remind_users)
        error = self._validate_reminder(remind_minutes_before, remind_ids)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        try:
            start_dt = _parse_local_datetime(date, start)
            end_dt = _parse_local_datetime(date, end)
        except (ValueError, OverflowError):
            await interaction.response.send_message(
                "Couldn't understand that date/time. Try formats like `2026-09-15` and `3:00pm`.",
                ephemeral=True,
            )
            return

        if end_dt <= start_dt:
            await interaction.response.send_message("End time must be after start time.", ephemeral=True)
            return

        start_ts, end_ts = start_dt.timestamp(), end_dt.timestamp()
        now = time.time()

        cursor = await self.bot.db.execute(
            "INSERT INTO schedule_events "
            "(guild_id, person, category, title, start_time, end_time, location, "
            "remind_minutes_before, remind_user_ids, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                interaction.guild_id,
                person,
                category,
                title,
                start_ts,
                end_ts,
                location,
                remind_minutes_before,
                ",".join(str(i) for i in remind_ids) if remind_ids else None,
                interaction.user.id,
                now,
            ),
        )
        await self.bot.db.commit()
        new_id = cursor.lastrowid

        conflicts = await self._find_conflicts(
            interaction.guild_id, start_dt.date(), start_ts, end_ts, exclude_event_id=new_id
        )
        msg = f"📅 Added **{person}** — {category}: {title} ({_fmt_time_range(start_ts, end_ts)})"
        if conflicts:
            msg += "\n⚠️ Heads up — this overlaps with: " + "; ".join(conflicts)
        await interaction.response.send_message(msg)

    @schedule_group.command(name="add-recurring", description="Add a standing weekly event")
    @app_commands.describe(
        person="Who this event is for",
        category="Event type",
        title="Short description, e.g. 'Karate class'",
        weekday="Day of the week this repeats on",
        start="Start time, e.g. 5:00pm",
        end="End time, e.g. 6:00pm",
        starts_on="Optional date this rule starts applying",
        ends_on="Optional date this rule stops applying (e.g. season end)",
        location="Optional location",
        remind_minutes_before="Minutes before start to send a reminder",
        remind_users="Mention everyone who should be pinged, e.g. @Mom @Dad",
    )
    @app_commands.choices(person=PERSON_CHOICES)
    @in_schedule_channel()
    async def add_recurring(
        self,
        interaction: discord.Interaction,
        person: str,
        category: CategoryLiteral,
        title: str,
        weekday: WeekdayLiteral,
        start: str,
        end: str,
        starts_on: Optional[str] = None,
        ends_on: Optional[str] = None,
        location: Optional[str] = None,
        remind_minutes_before: Optional[int] = None,
        remind_users: Optional[str] = None,
    ):
        remind_ids = _parse_remind_users(remind_users)
        error = self._validate_reminder(remind_minutes_before, remind_ids)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        try:
            start_hhmm = _parse_time_to_hhmm(start)
            end_hhmm = _parse_time_to_hhmm(end)
            starts_on_iso = _parse_date_to_iso(starts_on) if starts_on else None
            ends_on_iso = _parse_date_to_iso(ends_on) if ends_on else None
        except (ValueError, OverflowError):
            await interaction.response.send_message(
                "Couldn't understand one of the times/dates given.", ephemeral=True
            )
            return

        if end_hhmm <= start_hhmm:
            await interaction.response.send_message("End time must be after start time.", ephemeral=True)
            return

        now = time.time()
        cursor = await self.bot.db.execute(
            "INSERT INTO schedule_recurring "
            "(guild_id, person, category, title, weekday, start_time, end_time, location, "
            "starts_on, ends_on, remind_minutes_before, remind_user_ids, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                interaction.guild_id,
                person,
                category,
                title,
                WEEKDAY_TO_INT[weekday],
                start_hhmm,
                end_hhmm,
                location,
                starts_on_iso,
                ends_on_iso,
                remind_minutes_before,
                ",".join(str(i) for i in remind_ids) if remind_ids else None,
                interaction.user.id,
                now,
            ),
        )
        await self.bot.db.commit()
        rule_id = cursor.lastrowid

        msg = f"📅 Added recurring `R#{rule_id}` — **{person}**: {category}: {title}, {weekday}s {start_hhmm}–{end_hhmm}"

        next_occ = next(
            iter(_occurrences_in_range(
                {
                    "weekday": WEEKDAY_TO_INT[weekday],
                    "start_time": start_hhmm,
                    "end_time": end_hhmm,
                    "starts_on": starts_on_iso,
                    "ends_on": ends_on_iso,
                },
                _today(),
                _today() + timedelta(days=7),
            )),
            None,
        )
        if next_occ:
            occ_date, occ_start, occ_end = next_occ
            conflicts = await self._find_conflicts(
                interaction.guild_id, occ_date, occ_start, occ_end, exclude_recurring_id=rule_id
            )
            if conflicts:
                msg += "\n⚠️ Heads up — its next occurrence overlaps with: " + "; ".join(conflicts)

        await interaction.response.send_message(msg)

    @schedule_group.command(name="edit", description="Edit a one-off event")
    @app_commands.describe(id="The #id shown in /schedtoday or /schedweek", clear_reminder="Remove any reminder from this event")
    @app_commands.choices(person=PERSON_CHOICES)
    @in_schedule_channel()
    async def edit(
        self,
        interaction: discord.Interaction,
        id: int,
        person: Optional[str] = None,
        category: Optional[CategoryLiteral] = None,
        title: Optional[str] = None,
        date: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        location: Optional[str] = None,
        remind_minutes_before: Optional[int] = None,
        remind_users: Optional[str] = None,
        clear_reminder: bool = False,
    ):
        async with self.bot.db.execute(
            "SELECT * FROM schedule_events WHERE id=? AND guild_id=?", (id, interaction.guild_id)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await interaction.response.send_message(f"No event with id #{id}.", ephemeral=True)
            return

        current_date = date if date is not None else _epoch_to_local_date(row["start_time"]).isoformat()
        current_start = start if start is not None else _epoch_to_local_hhmm(row["start_time"])
        current_end = end if end is not None else _epoch_to_local_hhmm(row["end_time"])
        try:
            start_dt = _parse_local_datetime(current_date, current_start)
            end_dt = _parse_local_datetime(current_date, current_end)
        except (ValueError, OverflowError):
            await interaction.response.send_message("Couldn't understand that date/time.", ephemeral=True)
            return
        if end_dt <= start_dt:
            await interaction.response.send_message("End time must be after start time.", ephemeral=True)
            return

        if clear_reminder:
            new_remind_minutes, new_remind_ids_str = None, None
        else:
            remind_ids = _parse_remind_users(remind_users) if remind_users is not None else None
            effective_minutes = remind_minutes_before if remind_minutes_before is not None else row["remind_minutes_before"]
            effective_ids = remind_ids if remind_ids is not None else (
                [int(x) for x in row["remind_user_ids"].split(",")] if row["remind_user_ids"] else []
            )
            if remind_minutes_before is not None or remind_users is not None:
                error = self._validate_reminder(effective_minutes, effective_ids)
                if error:
                    await interaction.response.send_message(error, ephemeral=True)
                    return
            new_remind_minutes = effective_minutes
            new_remind_ids_str = ",".join(str(i) for i in effective_ids) if effective_ids else None

        await self.bot.db.execute(
            "UPDATE schedule_events SET person=?, category=?, title=?, start_time=?, end_time=?, "
            "location=?, remind_minutes_before=?, remind_user_ids=?, reminder_sent=0 WHERE id=?",
            (
                person if person is not None else row["person"],
                category if category is not None else row["category"],
                title if title is not None else row["title"],
                start_dt.timestamp(),
                end_dt.timestamp(),
                location if location is not None else row["location"],
                new_remind_minutes,
                new_remind_ids_str,
                id,
            ),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(f"✏️ Updated event #{id}.")

    @schedule_group.command(name="edit-recurring", description="Edit a standing weekly event")
    @app_commands.describe(id="The R#id shown in /schedule list-recurring", clear_reminder="Remove any reminder from this rule")
    @app_commands.choices(person=PERSON_CHOICES)
    @in_schedule_channel()
    async def edit_recurring(
        self,
        interaction: discord.Interaction,
        id: int,
        person: Optional[str] = None,
        category: Optional[CategoryLiteral] = None,
        title: Optional[str] = None,
        weekday: Optional[WeekdayLiteral] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        starts_on: Optional[str] = None,
        ends_on: Optional[str] = None,
        location: Optional[str] = None,
        remind_minutes_before: Optional[int] = None,
        remind_users: Optional[str] = None,
        clear_reminder: bool = False,
    ):
        async with self.bot.db.execute(
            "SELECT * FROM schedule_recurring WHERE id=? AND guild_id=?", (id, interaction.guild_id)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await interaction.response.send_message(f"No recurring rule with id R#{id}.", ephemeral=True)
            return

        try:
            new_start = _parse_time_to_hhmm(start) if start is not None else row["start_time"]
            new_end = _parse_time_to_hhmm(end) if end is not None else row["end_time"]
            new_starts_on = _parse_date_to_iso(starts_on) if starts_on is not None else row["starts_on"]
            new_ends_on = _parse_date_to_iso(ends_on) if ends_on is not None else row["ends_on"]
        except (ValueError, OverflowError):
            await interaction.response.send_message("Couldn't understand one of the times/dates given.", ephemeral=True)
            return
        if new_end <= new_start:
            await interaction.response.send_message("End time must be after start time.", ephemeral=True)
            return

        if clear_reminder:
            new_remind_minutes, new_remind_ids_str = None, None
        else:
            remind_ids = _parse_remind_users(remind_users) if remind_users is not None else None
            effective_minutes = remind_minutes_before if remind_minutes_before is not None else row["remind_minutes_before"]
            effective_ids = remind_ids if remind_ids is not None else (
                [int(x) for x in row["remind_user_ids"].split(",")] if row["remind_user_ids"] else []
            )
            if remind_minutes_before is not None or remind_users is not None:
                error = self._validate_reminder(effective_minutes, effective_ids)
                if error:
                    await interaction.response.send_message(error, ephemeral=True)
                    return
            new_remind_minutes = effective_minutes
            new_remind_ids_str = ",".join(str(i) for i in effective_ids) if effective_ids else None

        await self.bot.db.execute(
            "UPDATE schedule_recurring SET person=?, category=?, title=?, weekday=?, start_time=?, "
            "end_time=?, location=?, starts_on=?, ends_on=?, remind_minutes_before=?, remind_user_ids=?, "
            "last_reminder_occurrence_date=NULL WHERE id=?",
            (
                person if person is not None else row["person"],
                category if category is not None else row["category"],
                title if title is not None else row["title"],
                WEEKDAY_TO_INT[weekday] if weekday is not None else row["weekday"],
                new_start,
                new_end,
                location if location is not None else row["location"],
                new_starts_on,
                new_ends_on,
                new_remind_minutes,
                new_remind_ids_str,
                id,
            ),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(f"✏️ Updated recurring event R#{id}.")

    # ------------------------------------------------------------- remove

    @schedule_group.command(name="remove", description="Remove a one-off event")
    @app_commands.describe(id="The #id shown in /schedtoday or /schedweek")
    @in_schedule_channel()
    async def remove(self, interaction: discord.Interaction, id: int):
        cursor = await self.bot.db.execute(
            "DELETE FROM schedule_events WHERE id=? AND guild_id=?", (id, interaction.guild_id)
        )
        await self.bot.db.commit()
        if cursor.rowcount == 0:
            await interaction.response.send_message(f"No event with id #{id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"🗑️ Removed event #{id}.")

    @schedule_group.command(name="remove-recurring", description="End a standing weekly event")
    @app_commands.describe(id="The R#id shown in /schedule list-recurring")
    @in_schedule_channel()
    async def remove_recurring(self, interaction: discord.Interaction, id: int):
        cursor = await self.bot.db.execute(
            "DELETE FROM schedule_recurring WHERE id=? AND guild_id=?", (id, interaction.guild_id)
        )
        await self.bot.db.commit()
        if cursor.rowcount == 0:
            await interaction.response.send_message(f"No recurring rule with id R#{id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"🗑️ Removed recurring event R#{id}.")

    # -------------------------------------------------------------- viewing

    @schedule_group.command(name="list-recurring", description="List all standing weekly events")
    @in_schedule_channel()
    async def list_recurring(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM schedule_recurring WHERE guild_id=? ORDER BY weekday, start_time",
            (interaction.guild_id,),
        ) as cur:
            rules = await cur.fetchall()
        if not rules:
            await interaction.response.send_message("No standing weekly events set up.", ephemeral=True)
            return
        lines = []
        for rule in rules:
            bell = " 🔔" if rule["remind_minutes_before"] else ""
            loc = f" @ {rule['location']}" if rule["location"] else ""
            lines.append(
                f"`R#{rule['id']}` **{rule['person']}** — {rule['category']}: {rule['title']} — "
                f"{WEEKDAY_NAMES[rule['weekday']]}s {rule['start_time']}–{rule['end_time']}{loc}{bell}"
            )
        embed = discord.Embed(title="Standing weekly events", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed)

    @schedule_group.command(name="person", description="Show one person's upcoming events")
    @app_commands.choices(person=PERSON_CHOICES)
    @in_schedule_channel()
    async def person_schedule(self, interaction: discord.Interaction, person: str):
        occurrences = await self._gather_occurrences(
            interaction.guild_id, _today(), _today() + timedelta(days=30), person=person
        )
        embed = discord.Embed(
            title=f"{person}'s next 30 days",
            description=self._render_day_grouped(occurrences),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="schedtoday", description="Show today's family schedule")
    @in_schedule_channel()
    async def schedtoday(self, interaction: discord.Interaction):
        today = _today()
        occurrences = await self._gather_occurrences(interaction.guild_id, today, today)
        embed = discord.Embed(
            title=f"Today — {today.strftime('%A, %B')} {today.day}",
            description=self._render_day_grouped(occurrences),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="schedweek", description="Show this week's family schedule")
    @in_schedule_channel()
    async def schedweek(self, interaction: discord.Interaction):
        today = _today()
        occurrences = await self._gather_occurrences(interaction.guild_id, today, today + timedelta(days=6))
        embed = discord.Embed(
            title="This week",
            description=self._render_day_grouped(occurrences),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------- background

    @tasks.loop(seconds=60)
    async def check_reminders(self):
        now = time.time()

        async with self.bot.db.execute(
            "SELECT * FROM schedule_events WHERE remind_minutes_before IS NOT NULL AND reminder_sent=0"
        ) as cur:
            events = await cur.fetchall()
        for row in events:
            remind_at = row["start_time"] - row["remind_minutes_before"] * 60
            if now >= remind_at:
                if now <= row["end_time"]:
                    await self._send_reminder(row["remind_user_ids"], row["person"], row["title"], row["start_time"])
                await self.bot.db.execute(
                    "UPDATE schedule_events SET reminder_sent=1 WHERE id=?", (row["id"],)
                )
                await self.bot.db.commit()

        async with self.bot.db.execute(
            "SELECT * FROM schedule_recurring WHERE remind_minutes_before IS NOT NULL"
        ) as cur:
            rules = await cur.fetchall()
        today = _today()
        for rule in rules:
            occ = next(iter(_occurrences_in_range(rule, today, today + timedelta(days=7))), None)
            if occ is None:
                continue
            occ_date, start_ts, end_ts = occ
            if rule["last_reminder_occurrence_date"] == occ_date.isoformat():
                continue
            remind_at = start_ts - rule["remind_minutes_before"] * 60
            if now >= remind_at and now <= end_ts:
                await self._send_reminder(rule["remind_user_ids"], rule["person"], rule["title"], start_ts)
                await self.bot.db.execute(
                    "UPDATE schedule_recurring SET last_reminder_occurrence_date=? WHERE id=?",
                    (occ_date.isoformat(), rule["id"]),
                )
                await self.bot.db.commit()

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    async def _send_reminder(self, remind_user_ids: Optional[str], person: str, title: str, start_ts: float):
        if not remind_user_ids:
            return
        channel = self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(config.SCHEDULE_CHANNEL_ID)
            except discord.HTTPException:
                log.error("Could not fetch SCHEDULE_CHANNEL_ID=%s", config.SCHEDULE_CHANNEL_ID)
                return
        mentions = " ".join(f"<@{uid}>" for uid in remind_user_ids.split(","))
        await channel.send(
            f"⏰ {mentions} — reminder: **{person}**'s \"{title}\" starts <t:{int(start_ts)}:R> "
            f"at <t:{int(start_ts)}:t>."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
