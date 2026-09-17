"""
Shopping list skill: one shared list per store, entered via slash
commands in a dedicated channel. Replaces a shared spreadsheet.

Design choices worth knowing about:
- Each active item is its own Discord message (posted once, then edited
  in place as its quantity changes). It does NOT carry a pre-attached
  reaction — there's nothing to check off the moment you've just added
  something. /shop list is the primary way to check items off (tap a
  button); /shop got and manually reacting ✅ on an item's own message
  both still work too, as alternatives.
- Adding an item that's already active on the same store's list (case-
  insensitive match) replaces its quantity rather than creating a
  second line or adding the quantities together.
- /shop list is interactive: active items each get a checkoff button
  (tap it, it's gone from the active section immediately); checked-off
  items are listed struck-through at the bottom rather than hidden, so
  a shopping trip's worth of "got it" stays reviewable in the same
  view. /shop clear-checked removes them for good.
  Note: these buttons only work while the bot process that posted them
  keeps running — after a bot restart, an old /shop list message's
  buttons go dead; just run /shop list again.
- Stores are config-driven (SHOPPING_STORES), not hardcoded — the
  first one listed is the default store for /shop add and /shop list
  when no store is given.
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

log = logging.getLogger("homeghost.shopping")

CHECK_EMOJI = "✅"
ALL_STORES = "All Stores"

STORE_CHOICES = [app_commands.Choice(name=s, value=s) for s in config.SHOPPING_STORES]
STORE_CHOICES_WITH_ALL = STORE_CHOICES + [app_commands.Choice(name=ALL_STORES, value=ALL_STORES)]
DEFAULT_STORE = config.SHOPPING_STORES[0]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shopping_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    store TEXT NOT NULL,
    item TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    message_id INTEGER,
    added_by INTEGER NOT NULL,
    added_at REAL NOT NULL,
    checked_by INTEGER,
    checked_at REAL
);
"""


# --- @HG natural-language actions -----------------------------------
# Params the LLM is allowed to fill in. Deliberately excludes anything
# that comes from real Discord context (guild, channel, who's speaking)
# — that's supplied separately via NLPContext, never invented by a model.

class AddItemParams(BaseModel):
    item: str = Field(description="The item name, e.g. 'milk'")
    quantity: int = Field(default=1, ge=1, le=999, description="How many; default 1 if not stated")
    store: Optional[str] = Field(
        default=None,
        description=f"Which store, one of {config.SHOPPING_STORES}. Default ({DEFAULT_STORE}) if not stated.",
    )


class CheckOffItemParams(BaseModel):
    item: str = Field(description="The item name to check off as purchased/gotten")
    store: Optional[str] = Field(
        default=None,
        description="Only needed if the same item name is active on more than one store's list.",
    )


def _fmt_active(item: str, quantity: int, user_id: int) -> str:
    qty = f" x{quantity}" if quantity != 1 else ""
    return f"🛒 **{item}**{qty} — added by <@{user_id}>"


def _fmt_checked(item: str, quantity: int, checked_by: int) -> str:
    qty = f" x{quantity}" if quantity != 1 else ""
    return f"~~🛒 **{item}**{qty}~~ ✅ checked off by <@{checked_by}>"


def in_shopping_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != config.SHOPPING_CHANNEL_ID:
            await interaction.response.send_message(
                f"Please use shopping list commands in <#{config.SHOPPING_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return False
        return True

    return app_commands.check(predicate)


MAX_CHECKOFF_BUTTONS = 25  # Discord's hard cap: 5 rows x 5 buttons per message


class CheckoffButton(discord.ui.Button):
    """One button per active item on a /shop list view. Tapping it checks
    that specific item off and re-renders the whole list in place."""

    def __init__(self, cog: "Shopping", item_id: int, item_name: str, guild_id: int, store: str):
        label = item_name if len(item_name) <= 80 else item_name[:77] + "..."
        super().__init__(style=discord.ButtonStyle.secondary, emoji="✅", label=label)
        self.cog = cog
        self.item_id = item_id
        self.guild_id = guild_id
        self.store = store

    async def callback(self, interaction: discord.Interaction):
        # Defer immediately, before any DB/Discord work -- this acks the
        # click within Discord's strict 3-second window regardless of how
        # long the checkoff + list-rebuild takes (a DB hiccup, a slow API
        # call editing the item's own message, etc.), instead of risking
        # a stale "Unknown interaction" error if that work is ever slow.
        await interaction.response.defer()

        async with self.cog.bot.db.execute(
            "SELECT * FROM shopping_items WHERE id=? AND status='active'", (self.item_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            # Someone else already checked it off (or it was removed) since this view was rendered.
            await interaction.followup.send("That item was already handled.", ephemeral=True)
            return

        await self.cog._check_off(row, interaction.user.id)
        embed, view = await self.cog._build_list_view(self.guild_id, self.store)
        await interaction.edit_original_response(embed=embed, view=view)


class ShoppingListView(discord.ui.View):
    """Interactive /shop list message: one checkoff button per active item.
    timeout=None keeps it live indefinitely while the bot process runs —
    it does not survive a bot restart, since the view instance only lives
    in memory."""

    def __init__(self, cog: "Shopping", guild_id: int, store: str, active_rows: list):
        super().__init__(timeout=None)
        for row in active_rows[:MAX_CHECKOFF_BUTTONS]:
            self.add_item(CheckoffButton(cog, row["id"], row["item"], guild_id, store))


class Shopping(commands.Cog):
    """Shared, per-store shopping list with reaction-based checkoff."""

    shop_group = app_commands.Group(name="shop", description="Shopping list commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------------------------------------------------------- setup

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA_SQL)
        await self.bot.db.commit()

        nlp_registry.register(
            "shop_add_item",
            "Add an item to the shopping list, optionally with a quantity and a specific store.",
            AddItemParams,
            self.core_add_item,
            channel_id=config.SHOPPING_CHANNEL_ID,
        )
        nlp_registry.register(
            "shop_check_off_item",
            "Mark a shopping list item as purchased/gotten.",
            CheckOffItemParams,
            self.core_check_off_item,
            channel_id=config.SHOPPING_CHANNEL_ID,
        )

    # ------------------------------------------------------- @HG actions

    async def core_add_item(self, ctx: NLPContext, params: AddItemParams) -> ActionResult:
        item = params.item.strip()
        if not item:
            return ActionResult(success=False, summary="No item name was given.")
        store = params.store if params.store in config.SHOPPING_STORES else DEFAULT_STORE

        action = await self._upsert_item(ctx.guild_id, ctx.channel, store, item, params.quantity, ctx.speaker_user_id)
        verb = "updated" if action == "updated" else "added"
        return ActionResult(success=True, summary=f"{verb} {item} x{params.quantity} on the {store} list")

    async def core_check_off_item(self, ctx: NLPContext, params: CheckOffItemParams) -> ActionResult:
        item = params.item.strip()
        matches = await self._find_active(ctx.guild_id, item, params.store)

        if not matches:
            return ActionResult(success=False, summary=f"No active item named '{item}' was found on any list.")
        if len(matches) > 1:
            stores = ", ".join(sorted({m["store"] for m in matches}))
            return ActionResult(
                success=False,
                summary=f"'{item}' is active on more than one list ({stores}) — needs a specific store to disambiguate.",
            )

        await self._check_off(matches[0], ctx.speaker_user_id)
        return ActionResult(success=True, summary=f"checked off {matches[0]['item']}")

    # --------------------------------------------------------------- shared

    async def _find_active(
        self, guild_id: int, item: str, store: Optional[str] = None
    ) -> list:
        query = (
            "SELECT * FROM shopping_items WHERE guild_id=? AND status='active' AND LOWER(item)=LOWER(?)"
        )
        params: list = [guild_id, item]
        if store:
            query += " AND store=?"
            params.append(store)
        async with self.bot.db.execute(query, params) as cur:
            return await cur.fetchall()

    async def _upsert_item(
        self,
        guild_id: int,
        channel: discord.abc.Messageable,
        store: str,
        item: str,
        quantity: int,
        user_id: int,
    ) -> str:
        """Insert a new active item, or replace the quantity on an existing
        one for the same store (case-insensitive name match), editing its
        message in place rather than reposting. Returns 'added' or 'updated'."""
        existing_matches = await self._find_active(guild_id, item, store)
        now = time.time()

        if existing_matches:
            existing = existing_matches[0]
            await self.bot.db.execute(
                "UPDATE shopping_items SET item=?, quantity=?, added_by=?, added_at=? WHERE id=?",
                (item, quantity, user_id, now, existing["id"]),
            )
            await self.bot.db.commit()

            message = None
            if existing["message_id"]:
                try:
                    message = await channel.fetch_message(existing["message_id"])
                except (discord.NotFound, discord.HTTPException):
                    message = None
            content = _fmt_active(item, quantity, user_id)
            if message:
                await message.edit(content=content)
            else:
                message = await channel.send(content)
                await self.bot.db.execute(
                    "UPDATE shopping_items SET message_id=? WHERE id=?", (message.id, existing["id"])
                )
                await self.bot.db.commit()
            return "updated"

        cursor = await self.bot.db.execute(
            "INSERT INTO shopping_items (guild_id, store, item, quantity, status, added_by, added_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (guild_id, store, item, quantity, user_id, now),
        )
        await self.bot.db.commit()
        new_id = cursor.lastrowid

        message = await channel.send(_fmt_active(item, quantity, user_id))
        await self.bot.db.execute("UPDATE shopping_items SET message_id=? WHERE id=?", (message.id, new_id))
        await self.bot.db.commit()
        return "added"

    async def _build_list_view(self, guild_id: int, store: str) -> tuple[discord.Embed, "ShoppingListView"]:
        """Render a /shop list embed + its interactive checkoff buttons.
        Shared by the /shop list command and every button's callback, so
        checking an item off re-renders from this exact same logic."""
        query = "SELECT * FROM shopping_items WHERE guild_id=? AND status IN ('active', 'checked')"
        params: list = [guild_id]
        if store != ALL_STORES:
            query += " AND store=?"
            params.append(store)
        query += " ORDER BY store, item COLLATE NOCASE"
        async with self.bot.db.execute(query, params) as cur:
            rows = await cur.fetchall()

        active = [r for r in rows if r["status"] == "active"]
        checked = [r for r in rows if r["status"] == "checked"]

        def _line(row, struck: bool) -> str:
            qty = f" x{row['quantity']}" if row["quantity"] != 1 else ""
            text = f"{row['item']}{qty}"
            if store == ALL_STORES:
                text += f" ({row['store']})"
            return f"~~{text}~~" if struck else f"• {text}"

        lines = [_line(row, struck=False) for row in active]
        if checked:
            lines.append("")
            lines.extend(_line(row, struck=True) for row in checked)
        if not lines:
            lines = ["Nothing on the list right now. 🎉"]

        title = "Shopping list — all stores" if store == ALL_STORES else f"Shopping list — {store}"
        embed = discord.Embed(title=title, description="\n".join(lines).strip(), color=discord.Color.orange())
        if len(active) > MAX_CHECKOFF_BUTTONS:
            embed.set_footer(text=f"Showing checkoff buttons for the first {MAX_CHECKOFF_BUTTONS} items — use /shop got for the rest.")

        view = ShoppingListView(self, guild_id, store, active)
        return embed, view

    async def _check_off(self, row, user_id: int):
        now = time.time()
        await self.bot.db.execute(
            "UPDATE shopping_items SET status='checked', checked_by=?, checked_at=? WHERE id=?",
            (user_id, now, row["id"]),
        )
        await self.bot.db.commit()
        if row["message_id"]:
            channel = self.bot.get_channel(config.SHOPPING_CHANNEL_ID)
            if channel is not None:
                try:
                    message = await channel.fetch_message(row["message_id"])
                    await message.edit(content=_fmt_checked(row["item"], row["quantity"], user_id))
                except (discord.NotFound, discord.HTTPException):
                    pass

    # ------------------------------------------------------------- commands

    @shop_group.command(name="add", description="Add an item to a shopping list")
    @app_commands.describe(
        item="What to add, e.g. milk",
        quantity="How many (default 1)",
        store=f"Which store's list (default: {DEFAULT_STORE})",
    )
    @app_commands.choices(store=STORE_CHOICES)
    @in_shopping_channel()
    async def add(
        self,
        interaction: discord.Interaction,
        item: str,
        quantity: app_commands.Range[int, 1, 999] = 1,
        store: Optional[str] = None,
    ):
        item = item.strip()
        if not item:
            await interaction.response.send_message("Item name can't be empty.", ephemeral=True)
            return
        store = store or DEFAULT_STORE

        action = await self._upsert_item(interaction.guild_id, interaction.channel, store, item, quantity, interaction.user.id)
        verb = "Updated" if action == "updated" else "Added"
        await interaction.response.send_message(f"🛒 {verb} **{item}** x{quantity} on the {store} list.", ephemeral=True)

    @shop_group.command(name="move", description="Move an item to a different store's list")
    @app_commands.describe(
        item="Which item to move",
        store="Destination store",
        from_store="Only needed if the same item is active on more than one list",
    )
    @app_commands.choices(store=STORE_CHOICES, from_store=STORE_CHOICES)
    @in_shopping_channel()
    async def move(
        self,
        interaction: discord.Interaction,
        item: str,
        store: str,
        from_store: Optional[str] = None,
    ):
        item = item.strip()
        matches = await self._find_active(interaction.guild_id, item, from_store)

        if not matches:
            await interaction.response.send_message(f"No active item named '{item}' found.", ephemeral=True)
            return
        if len(matches) > 1:
            stores = ", ".join(sorted({m["store"] for m in matches}))
            await interaction.response.send_message(
                f"'{item}' is active on more than one list: {stores}. Specify from_store to pick one.",
                ephemeral=True,
            )
            return

        existing = matches[0]
        if existing["store"] == store:
            await interaction.response.send_message(f"**{existing['item']}** is already on the {store} list.", ephemeral=True)
            return

        channel = interaction.channel
        if existing["message_id"]:
            try:
                old_message = await channel.fetch_message(existing["message_id"])
                await old_message.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
        await self.bot.db.execute("DELETE FROM shopping_items WHERE id=?", (existing["id"],))
        await self.bot.db.commit()

        await self._upsert_item(
            interaction.guild_id, channel, store, existing["item"], existing["quantity"], interaction.user.id
        )
        await interaction.response.send_message(
            f"🔀 Moved **{existing['item']}** from {existing['store']} to {store}.", ephemeral=True
        )

    @shop_group.command(name="got", description="Check an item off the list")
    @app_commands.describe(item="Which item", store="Only needed if the same item is active on more than one list")
    @app_commands.choices(store=STORE_CHOICES)
    @in_shopping_channel()
    async def got(self, interaction: discord.Interaction, item: str, store: Optional[str] = None):
        item = item.strip()
        matches = await self._find_active(interaction.guild_id, item, store)

        if not matches:
            await interaction.response.send_message(f"No active item named '{item}' found.", ephemeral=True)
            return
        if len(matches) > 1:
            stores = ", ".join(sorted({m["store"] for m in matches}))
            await interaction.response.send_message(
                f"'{item}' is active on more than one list: {stores}. Specify store to pick one.",
                ephemeral=True,
            )
            return

        await self._check_off(matches[0], interaction.user.id)
        await interaction.response.send_message(f"✅ Checked off **{matches[0]['item']}**.", ephemeral=True)

    @shop_group.command(name="remove", description="Delete an item entirely (not checked off, just removed)")
    @app_commands.describe(item="Which item", store="Only needed if the same item is active on more than one list")
    @app_commands.choices(store=STORE_CHOICES)
    @in_shopping_channel()
    async def remove(self, interaction: discord.Interaction, item: str, store: Optional[str] = None):
        item = item.strip()
        matches = await self._find_active(interaction.guild_id, item, store)

        if not matches:
            await interaction.response.send_message(f"No active item named '{item}' found.", ephemeral=True)
            return
        if len(matches) > 1:
            stores = ", ".join(sorted({m["store"] for m in matches}))
            await interaction.response.send_message(
                f"'{item}' is active on more than one list: {stores}. Specify store to pick one.",
                ephemeral=True,
            )
            return

        existing = matches[0]
        if existing["message_id"]:
            try:
                message = await interaction.channel.fetch_message(existing["message_id"])
                await message.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
        await self.bot.db.execute("DELETE FROM shopping_items WHERE id=?", (existing["id"],))
        await self.bot.db.commit()
        await interaction.response.send_message(f"🗑️ Removed **{existing['item']}**.", ephemeral=True)

    @shop_group.command(name="clear-checked", description="Remove all checked-off items")
    @app_commands.describe(store="Only clear this store's checked items (default: all stores)")
    @app_commands.choices(store=STORE_CHOICES)
    @in_shopping_channel()
    async def clear_checked(self, interaction: discord.Interaction, store: Optional[str] = None):
        query = "SELECT * FROM shopping_items WHERE guild_id=? AND status='checked'"
        params: list = [interaction.guild_id]
        if store:
            query += " AND store=?"
            params.append(store)
        async with self.bot.db.execute(query, params) as cur:
            rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("Nothing checked off to clear.", ephemeral=True)
            return

        for row in rows:
            if row["message_id"]:
                try:
                    message = await interaction.channel.fetch_message(row["message_id"])
                    await message.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
            await self.bot.db.execute("DELETE FROM shopping_items WHERE id=?", (row["id"],))
        await self.bot.db.commit()
        await interaction.response.send_message(f"🧹 Cleared {len(rows)} checked-off item(s).", ephemeral=True)

    @shop_group.command(name="list", description="Show the current shopping list")
    @app_commands.describe(store=f"Which store's list (default: {DEFAULT_STORE}); choose '{ALL_STORES}' to see everything")
    @app_commands.choices(store=STORE_CHOICES_WITH_ALL)
    @in_shopping_channel()
    async def list_items(self, interaction: discord.Interaction, store: Optional[str] = None):
        store = store or DEFAULT_STORE
        embed, view = await self._build_list_view(interaction.guild_id, store)
        await interaction.response.send_message(embed=embed, view=view)

    # ------------------------------------------------------------ reactions

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != CHECK_EMOJI:
            return

        async with self.bot.db.execute(
            "SELECT * FROM shopping_items WHERE message_id=? AND status='active'", (payload.message_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return

        await self._check_off(row, payload.user_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shopping(bot))
