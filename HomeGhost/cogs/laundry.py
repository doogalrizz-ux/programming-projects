"""
Laundry skill: timers + escalating reminders for washer/dryer (or any
appliance name). Self-contained cog — owns its own table, its own
background loop, and its own commands. A future sensor/smart-plug
integration can replace the manual /washer 45 start with a call into
`LaundryCog.start_timer` without touching anything else in here.

Design choices worth knowing about:
- Timers are stored as an absolute start_time + duration, not a
  "time remaining" counter. That means a bot restart needs zero special
  recovery logic — the background loop just re-evaluates real wall-clock
  time against what's already in the DB on its next tick.
- Only one active (running or awaiting-acknowledgment) timer per
  appliance: starting a new one on a busy appliance simply replaces the
  old row instead of asking for confirmation. Simpler UX and code, at
  the cost of silently discarding whatever the old timer's history was
  (fine for a home laundry bot; would want a confirmation prompt if this
  ever tracked something higher-stakes).
"""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger("homeghost.laundry")

ACK_EMOJI = "✅"
MAX_MINUTES = 24 * 60  # a full day; guards against fat-fingered input

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS laundry_timers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appliance TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    start_time REAL NOT NULL,
    duration_minutes REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running | done | acknowledged
    reminder_count INTEGER NOT NULL DEFAULT 0,
    last_reminder_time REAL,
    message_id INTEGER,
    created_at REAL NOT NULL
);
"""


class Laundry(commands.Cog):
    """Timers + reminders for laundry appliances."""

    laundry_group = app_commands.Group(
        name="laundry", description="Laundry timer commands"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------------------------------------------------------- setup

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA_SQL)
        await self.bot.db.commit()
        self.check_timers.start()

    async def cog_unload(self):
        self.check_timers.cancel()

    # --------------------------------------------------------------- helpers

    async def _get_by_status(self, guild_id: int, statuses: tuple[str, ...], appliance: str | None = None):
        placeholders = ",".join("?" for _ in statuses)
        query = (
            f"SELECT * FROM laundry_timers WHERE guild_id=? AND status IN ({placeholders})"
        )
        params: list = [guild_id, *statuses]
        if appliance:
            query += " AND appliance=?"
            params.append(appliance)
        query += " ORDER BY start_time"
        async with self.bot.db.execute(query, params) as cur:
            return await cur.fetchall()

    async def _reminder_channel(self) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(config.REMINDER_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(config.REMINDER_CHANNEL_ID)
            except discord.HTTPException:
                log.error("Could not fetch REMINDER_CHANNEL_ID=%s", config.REMINDER_CHANNEL_ID)
                return None
        return channel

    async def start_timer(self, interaction: discord.Interaction, appliance: str, minutes: int):
        """Shared entry point for /washer, /dryer and /timer. Also the seam
        a future automatic (sensor-based) trigger would call instead of a
        slash command."""
        appliance = appliance.strip().lower()
        if not appliance:
            await interaction.response.send_message("Appliance name can't be empty.", ephemeral=True)
            return
        if minutes <= 0 or minutes > MAX_MINUTES:
            await interaction.response.send_message(
                f"Minutes must be between 1 and {MAX_MINUTES}.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        now = time.time()

        existing = await self._get_by_status(guild_id, ("running", "done"), appliance)
        if existing:
            await self.bot.db.execute(
                "DELETE FROM laundry_timers WHERE guild_id=? AND appliance=? AND status IN ('running','done')",
                (guild_id, appliance),
            )

        await self.bot.db.execute(
            "INSERT INTO laundry_timers "
            "(appliance, guild_id, user_id, start_time, duration_minutes, status, reminder_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', 0, ?)",
            (appliance, guild_id, interaction.user.id, now, minutes, now),
        )
        await self.bot.db.commit()

        finish_ts = int(now + minutes * 60)
        note = " (replaced the timer already running on this appliance)" if existing else ""
        await interaction.response.send_message(
            f"⏱️ Started **{appliance}** for {minutes} min — finishes <t:{finish_ts}:R> (<t:{finish_ts}:t>).{note}"
        )

    # -------------------------------------------------------------- commands

    @app_commands.command(name="washer", description="Start a washer timer")
    @app_commands.describe(minutes="How many minutes the load needs")
    async def washer(self, interaction: discord.Interaction, minutes: int):
        await self.start_timer(interaction, "washer", minutes)

    @app_commands.command(name="dryer", description="Start a dryer timer")
    @app_commands.describe(minutes="How many minutes the load needs")
    async def dryer(self, interaction: discord.Interaction, minutes: int):
        await self.start_timer(interaction, "dryer", minutes)

    @app_commands.command(name="timer", description="Start a timer for any appliance")
    @app_commands.describe(appliance="Appliance name, e.g. washer, dryer, dishwasher", minutes="How many minutes")
    async def timer(self, interaction: discord.Interaction, appliance: str, minutes: int):
        await self.start_timer(interaction, appliance, minutes)

    @app_commands.command(name="done", description="Acknowledge a finished laundry load and stop reminders")
    @app_commands.describe(appliance="Which appliance (only needed if more than one is awaiting acknowledgment)")
    async def done(self, interaction: discord.Interaction, appliance: str | None = None):
        guild_id = interaction.guild_id
        appliance_norm = appliance.strip().lower() if appliance else None

        rows = await self._get_by_status(guild_id, ("done",), appliance_norm)
        if not rows:
            suffix = f" for **{appliance_norm}**" if appliance_norm else ""
            await interaction.response.send_message(
                f"Nothing is currently waiting to be acknowledged{suffix}.", ephemeral=True
            )
            return

        if len(rows) > 1:
            names = ", ".join(sorted({row["appliance"] for row in rows}))
            await interaction.response.send_message(
                f"More than one load is waiting: **{names}**. Specify which one, e.g. `/done washer`.",
                ephemeral=True,
            )
            return

        row = rows[0]
        await self.bot.db.execute(
            "UPDATE laundry_timers SET status='acknowledged' WHERE id=?", (row["id"],)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"✅ **{row['appliance'].title()}** acknowledged. Reminders stopped."
        )

    @laundry_group.command(name="status", description="Show all laundry timers and time remaining")
    async def laundry_status(self, interaction: discord.Interaction):
        rows = await self._get_by_status(interaction.guild_id, ("running", "done"))
        if not rows:
            await interaction.response.send_message("No laundry timers right now. 🎉", ephemeral=True)
            return

        lines = []
        for row in rows:
            appliance = row["appliance"].title()
            if row["status"] == "running":
                finish_ts = int(row["start_time"] + row["duration_minutes"] * 60)
                lines.append(
                    f"🧺 **{appliance}** — started by <@{row['user_id']}>, finishes <t:{finish_ts}:R>"
                )
            else:
                count = row["reminder_count"]
                lines.append(
                    f"🔔 **{appliance}** — done, awaiting ack from <@{row['user_id']}> "
                    f"({count} reminder{'s' if count != 1 else ''} sent)"
                )
        await interaction.response.send_message("\n".join(lines))

    # ------------------------------------------------------------ reactions

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != ACK_EMOJI:
            return

        async with self.bot.db.execute(
            "SELECT * FROM laundry_timers WHERE message_id=? AND status='done'", (payload.message_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return

        await self.bot.db.execute(
            "UPDATE laundry_timers SET status='acknowledged' WHERE id=?", (row["id"],)
        )
        await self.bot.db.commit()

        channel = self.bot.get_channel(payload.channel_id)
        if channel is not None:
            await channel.send(
                f"✅ **{row['appliance'].title()}** acknowledged by <@{payload.user_id}>. Nice work!"
            )

    # ---------------------------------------------------------- background

    @tasks.loop(seconds=20)
    async def check_timers(self):
        now = time.time()

        async with self.bot.db.execute("SELECT * FROM laundry_timers WHERE status='running'") as cur:
            running = await cur.fetchall()
        for row in running:
            finish_at = row["start_time"] + row["duration_minutes"] * 60
            if now >= finish_at:
                await self._mark_done_and_notify(row)

        async with self.bot.db.execute("SELECT * FROM laundry_timers WHERE status='done'") as cur:
            awaiting = await cur.fetchall()
        interval_seconds = config.REMINDER_INTERVAL_MINUTES * 60
        for row in awaiting:
            last = row["last_reminder_time"] or 0
            if now - last >= interval_seconds:
                await self._send_reminder(row)

    @check_timers.before_loop
    async def before_check_timers(self):
        await self.bot.wait_until_ready()

    async def _mark_done_and_notify(self, row):
        channel = await self._reminder_channel()
        if channel is None:
            return

        now = time.time()
        message = await channel.send(
            f"🔔 <@{row['user_id']}> — **{row['appliance'].title()}** is done! Time to move it / fold it."
        )
        try:
            await message.add_reaction(ACK_EMOJI)
        except discord.HTTPException:
            pass

        await self.bot.db.execute(
            "UPDATE laundry_timers SET status='done', reminder_count=1, last_reminder_time=?, message_id=? "
            "WHERE id=?",
            (now, message.id, row["id"]),
        )
        await self.bot.db.commit()

    async def _send_reminder(self, row):
        channel = await self._reminder_channel()
        if channel is None:
            return

        now = time.time()
        new_count = row["reminder_count"] + 1
        escalate = new_count >= config.ESCALATION_THRESHOLD and config.ESCALATION_USER_ID

        mentions = f"<@{row['user_id']}>"
        if escalate:
            mentions += f" <@{config.ESCALATION_USER_ID}>"

        message = await channel.send(
            f"🔔 {mentions} — **{row['appliance'].title()}** is *still* waiting "
            f"(reminder #{new_count}). React with {ACK_EMOJI} or run `/done {row['appliance']}` once it's handled."
        )
        try:
            await message.add_reaction(ACK_EMOJI)
        except discord.HTTPException:
            pass

        await self.bot.db.execute(
            "UPDATE laundry_timers SET reminder_count=?, last_reminder_time=?, message_id=? WHERE id=?",
            (new_count, now, message.id, row["id"]),
        )
        await self.bot.db.commit()


async def setup(bot: commands.Bot):
    await bot.add_cog(Laundry(bot))
