"""
Chore list skill: assign chores to people, track completion, entered
via slash commands (and @HG) in a dedicated channel.

Structurally almost identical to shopping.py — same "each active entry
is its own message, no pre-attached reaction, /view command is
interactive with tap-to-complete buttons, completed entries stay
struck-through until cleared" pattern, which already proved out well
there. The one real difference: shopping's list view is household-wide
by default (everyone shares one store's list); a chore list is
personal, so /chlist defaults to showing whoever ran it their own
chores, resolved by matching their server nickname against
FAMILY_MEMBERS — the same trick @HG already uses to know who's talking.

Natural key is (chore, person) rather than shopping's (item, store):
assigning the same chore to several people at once (e.g. "laundry to
each of the kids") is deliberately several separate, independently
completable entries, not one shared entry — see nlp_registry's
core_assign_chore, which lets @HG's multi-action extraction handle the
"one message names several people" case exactly the way it already
handles "one message names several grocery items."
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from pydantic import BaseModel, Field

import config
import nlp_registry
from nlp_registry import ActionResult, NLPContext

log = logging.getLogger("homeghost.chores")

CHECK_EMOJI = "✅"
ALL_PEOPLE = "Everyone"

PERSON_CHOICES = [app_commands.Choice(name=m, value=m) for m in config.FAMILY_MEMBERS]
PERSON_CHOICES_WITH_ALL = PERSON_CHOICES + [app_commands.Choice(name=ALL_PEOPLE, value=ALL_PEOPLE)]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    person TEXT NOT NULL,
    chore TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    message_id INTEGER,
    assigned_by INTEGER NOT NULL,
    assigned_at REAL NOT NULL,
    done_by INTEGER,
    done_at REAL
);
"""


class AssignChoreParams(BaseModel):
    chore: str = Field(description="The chore/task, e.g. 'mow the lawn'")
    person: str = Field(description=f"Who it's assigned to, one of {config.FAMILY_MEMBERS}")


class CompleteChoreParams(BaseModel):
    chore: str = Field(description="The chore to mark done")
    person: Optional[str] = Field(
        default=None,
        description="Who completed it. If not stated, assume it's the person speaking.",
    )


def _fmt_active(chore: str, person: str, user_id: int) -> str:
    return f"📋 **{chore}** — assigned to {person} by <@{user_id}>"


def _fmt_done(chore: str, person: str, done_by: int) -> str:
    return f"~~📋 **{chore}** — assigned to {person}~~ ✅ done by <@{done_by}>"


def in_chore_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != config.CHORE_CHANNEL_ID:
            await interaction.response.send_message(
                f"Please use chore commands in <#{config.CHORE_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return False
        return True

    return app_commands.check(predicate)


def _resolve_person(member: discord.abc.User) -> Optional[str]:
    """Match a Discord member's server nickname against FAMILY_MEMBERS --
    same trick cogs/assistant.py uses to know who's speaking."""
    display = getattr(member, "display_name", "").strip().lower()
    for name in config.FAMILY_MEMBERS:
        if name.strip().lower() == display:
            return name
    return None


MAX_CHECKOFF_BUTTONS = 25  # Discord's hard cap: 5 rows x 5 buttons per message


class CompleteButton(discord.ui.Button):
    """One button per active chore on a /chlist view. Tapping it marks
    that specific chore done and re-renders the list in place."""

    def __init__(self, cog: "Chores", chore_id: int, chore_name: str, guild_id: int, person: str):
        label = chore_name if len(chore_name) <= 80 else chore_name[:77] + "..."
        super().__init__(style=discord.ButtonStyle.secondary, emoji="✅", label=label)
        self.cog = cog
        self.chore_id = chore_id
        self.guild_id = guild_id
        self.person = person

    async def callback(self, interaction: discord.Interaction):
        # Defer first, before any DB/Discord work -- learned this the hard
        # way on the shopping list's identical button (a slow moment among
        # several sequential round-trips blew past Discord's 3s ack window
        # and produced a stale "Unknown interaction" error).
        await interaction.response.defer()

        async with self.cog.bot.db.execute(
            "SELECT * FROM chores WHERE id=? AND status='active'", (self.chore_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await interaction.followup.send("That chore was already handled.", ephemeral=True)
            return

        await self.cog._complete(row, interaction.user.id)
        embed, view = await self.cog._build_list_view(self.guild_id, self.person)
        await interaction.edit_original_response(embed=embed, view=view)


class ChoreListView(discord.ui.View):
    """Interactive /chlist message: one complete-button per active chore.
    timeout=None keeps it live indefinitely while the bot process runs —
    doesn't survive a restart, since the view only lives in memory."""

    def __init__(self, cog: "Chores", guild_id: int, person: str, active_rows: list):
        super().__init__(timeout=None)
        for row in active_rows[:MAX_CHECKOFF_BUTTONS]:
            self.add_item(CompleteButton(cog, row["id"], row["chore"], guild_id, person))


class Chores(commands.Cog):
    """Per-person chore assignments with tap-to-complete checkoff."""

    chore_group = app_commands.Group(name="chore", description="Chore list commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------------------------------------------------------- setup

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA_SQL)
        await self.bot.db.commit()

        nlp_registry.register(
            "chore_assign",
            "Assign a chore to a person. To assign the same chore to several people, "
            "use this action once per person.",
            AssignChoreParams,
            self.core_assign_chore,
            channel_id=config.CHORE_CHANNEL_ID,
        )
        nlp_registry.register(
            "chore_complete",
            "Mark a chore done/completed.",
            CompleteChoreParams,
            self.core_complete_chore,
            channel_id=config.CHORE_CHANNEL_ID,
        )

    # ------------------------------------------------------- @HG actions

    async def core_assign_chore(self, ctx: NLPContext, params: AssignChoreParams) -> ActionResult:
        chore = params.chore.strip()
        if not chore:
            return ActionResult(success=False, summary="No chore name was given.")
        if params.person not in config.FAMILY_MEMBERS:
            return ActionResult(success=False, summary=f"'{params.person}' isn't in the family roster.")

        outcome = await self._assign(ctx.guild_id, ctx.channel, chore, params.person, ctx.speaker_user_id)
        if outcome == "already_active":
            return ActionResult(success=True, summary=f"{chore} was already assigned to {params.person}")
        return ActionResult(success=True, summary=f"assigned {chore} to {params.person}")

    async def core_complete_chore(self, ctx: NLPContext, params: CompleteChoreParams) -> ActionResult:
        chore = params.chore.strip()
        person = params.person or ctx.speaker_name
        if not person:
            return ActionResult(success=False, summary="Couldn't tell who this chore belongs to.")

        rows = await self._find_active(ctx.guild_id, chore, person)
        if not rows:
            return ActionResult(success=False, summary=f"No active chore '{chore}' found for {person}.")

        await self._complete(rows[0], ctx.speaker_user_id)
        return ActionResult(success=True, summary=f"marked {chore} done for {person}")

    # --------------------------------------------------------------- shared

    async def _find_active(self, guild_id: int, chore: str, person: str) -> list:
        async with self.bot.db.execute(
            "SELECT * FROM chores WHERE guild_id=? AND person=? AND status='active' AND LOWER(chore)=LOWER(?)",
            (guild_id, person, chore),
        ) as cur:
            return await cur.fetchall()

    async def _assign(
        self, guild_id: int, channel: discord.abc.Messageable, chore: str, person: str, user_id: int
    ) -> str:
        """Create a new active chore entry, or leave an already-active
        identical one alone. Returns 'assigned' or 'already_active'."""
        existing = await self._find_active(guild_id, chore, person)
        if existing:
            return "already_active"

        now = time.time()
        cursor = await self.bot.db.execute(
            "INSERT INTO chores (guild_id, person, chore, status, assigned_by, assigned_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (guild_id, person, chore, user_id, now),
        )
        await self.bot.db.commit()
        new_id = cursor.lastrowid

        message = await channel.send(_fmt_active(chore, person, user_id))
        await self.bot.db.execute("UPDATE chores SET message_id=? WHERE id=?", (message.id, new_id))
        await self.bot.db.commit()
        return "assigned"

    async def _complete(self, row, user_id: int):
        now = time.time()
        await self.bot.db.execute(
            "UPDATE chores SET status='done', done_by=?, done_at=? WHERE id=?",
            (user_id, now, row["id"]),
        )
        await self.bot.db.commit()
        if row["message_id"]:
            channel = self.bot.get_channel(config.CHORE_CHANNEL_ID)
            if channel is not None:
                try:
                    message = await channel.fetch_message(row["message_id"])
                    await message.edit(content=_fmt_done(row["chore"], row["person"], user_id))
                except (discord.NotFound, discord.HTTPException):
                    pass

    async def _build_list_view(self, guild_id: int, person: str) -> tuple[discord.Embed, "ChoreListView"]:
        query = "SELECT * FROM chores WHERE guild_id=? AND status IN ('active', 'done')"
        params: list = [guild_id]
        if person != ALL_PEOPLE:
            query += " AND person=?"
            params.append(person)
        query += " ORDER BY person, chore COLLATE NOCASE"
        async with self.bot.db.execute(query, params) as cur:
            rows = await cur.fetchall()

        active = [r for r in rows if r["status"] == "active"]
        done = [r for r in rows if r["status"] == "done"]

        def _line(row, struck: bool) -> str:
            text = row["chore"]
            if person == ALL_PEOPLE:
                text += f" ({row['person']})"
            return f"~~{text}~~" if struck else f"• {text}"

        lines = [_line(row, struck=False) for row in active]
        if done:
            lines.append("")
            lines.extend(_line(row, struck=True) for row in done)
        if not lines:
            lines = ["Nothing here right now. 🎉"]

        title = "Everyone's chores" if person == ALL_PEOPLE else f"{person}'s chores"
        embed = discord.Embed(title=title, description="\n".join(lines).strip(), color=discord.Color.green())
        if len(active) > MAX_CHECKOFF_BUTTONS:
            embed.set_footer(text=f"Showing checkoff buttons for the first {MAX_CHECKOFF_BUTTONS} — use /chore done for the rest.")

        view = ChoreListView(self, guild_id, person, active)
        return embed, view

    # ------------------------------------------------------------- commands

    @chore_group.command(name="assign", description="Assign a chore to someone")
    @app_commands.describe(chore="The chore, e.g. 'mow the lawn'", person="Who it's for")
    @app_commands.choices(person=PERSON_CHOICES)
    @in_chore_channel()
    async def assign(self, interaction: discord.Interaction, chore: str, person: str):
        chore = chore.strip()
        if not chore:
            await interaction.response.send_message("Chore name can't be empty.", ephemeral=True)
            return

        outcome = await self._assign(interaction.guild_id, interaction.channel, chore, person, interaction.user.id)
        if outcome == "already_active":
            await interaction.response.send_message(f"**{chore}** is already assigned to {person}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"📋 Assigned **{chore}** to {person}.", ephemeral=True)

    @chore_group.command(name="done", description="Mark a chore done")
    @app_commands.describe(chore="Which chore", person="Whose chore (default: you)")
    @app_commands.choices(person=PERSON_CHOICES)
    async def done(self, interaction: discord.Interaction, chore: str, person: Optional[str] = None):
        person = person or _resolve_person(interaction.user)
        if not person:
            await interaction.response.send_message(
                "Couldn't tell who you are from your server nickname — specify `person` explicitly.",
                ephemeral=True,
            )
            return

        rows = await self._find_active(interaction.guild_id, chore.strip(), person)
        if not rows:
            await interaction.response.send_message(f"No active chore '{chore}' found for {person}.", ephemeral=True)
            return

        await self._complete(rows[0], interaction.user.id)
        await interaction.response.send_message(f"✅ Marked **{rows[0]['chore']}** done for {person}.", ephemeral=True)

    @chore_group.command(name="remove", description="Delete a chore assignment entirely (not completed, just removed)")
    @app_commands.describe(chore="Which chore", person="Whose chore (default: you)")
    @app_commands.choices(person=PERSON_CHOICES)
    @in_chore_channel()
    async def remove(self, interaction: discord.Interaction, chore: str, person: Optional[str] = None):
        person = person or _resolve_person(interaction.user)
        if not person:
            await interaction.response.send_message(
                "Couldn't tell who you are from your server nickname — specify `person` explicitly.",
                ephemeral=True,
            )
            return

        rows = await self._find_active(interaction.guild_id, chore.strip(), person)
        if not rows:
            await interaction.response.send_message(f"No active chore '{chore}' found for {person}.", ephemeral=True)
            return

        row = rows[0]
        if row["message_id"]:
            try:
                message = await interaction.channel.fetch_message(row["message_id"])
                await message.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
        await self.bot.db.execute("DELETE FROM chores WHERE id=?", (row["id"],))
        await self.bot.db.commit()
        await interaction.response.send_message(f"🗑️ Removed **{row['chore']}** for {person}.", ephemeral=True)

    @chore_group.command(name="clear-done", description="Remove all completed chores")
    @app_commands.describe(person="Only clear this person's completed chores (default: everyone)")
    @app_commands.choices(person=PERSON_CHOICES)
    @in_chore_channel()
    async def clear_done(self, interaction: discord.Interaction, person: Optional[str] = None):
        query = "SELECT * FROM chores WHERE guild_id=? AND status='done'"
        params: list = [interaction.guild_id]
        if person:
            query += " AND person=?"
            params.append(person)
        async with self.bot.db.execute(query, params) as cur:
            rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("Nothing completed to clear.", ephemeral=True)
            return

        for row in rows:
            if row["message_id"]:
                try:
                    message = await interaction.channel.fetch_message(row["message_id"])
                    await message.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
            await self.bot.db.execute("DELETE FROM chores WHERE id=?", (row["id"],))
        await self.bot.db.commit()
        await interaction.response.send_message(f"🧹 Cleared {len(rows)} completed chore(s).", ephemeral=True)

    @app_commands.command(name="chlist", description="Show a chore list -- yours by default")
    @app_commands.describe(person=f"Whose list to show (default: you); choose '{ALL_PEOPLE}' to see everything")
    @app_commands.choices(person=PERSON_CHOICES_WITH_ALL)
    @in_chore_channel()
    async def chlist(self, interaction: discord.Interaction, person: Optional[str] = None):
        if person is None:
            person = _resolve_person(interaction.user)
            if not person:
                await interaction.response.send_message(
                    "Couldn't tell who you are from your server nickname — specify `person` explicitly, "
                    f"or choose '{ALL_PEOPLE}'.",
                    ephemeral=True,
                )
                return

        embed, view = await self._build_list_view(interaction.guild_id, person)
        await interaction.response.send_message(embed=embed, view=view)

    # ------------------------------------------------------------ reactions

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != CHECK_EMOJI:
            return

        async with self.bot.db.execute(
            "SELECT * FROM chores WHERE message_id=? AND status='active'", (payload.message_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return

        await self._complete(row, payload.user_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Chores(bot))
