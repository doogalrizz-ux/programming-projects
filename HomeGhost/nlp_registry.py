"""
Shared registry letting any HomeGhost skill opt a handful of its own
functions into natural-language control via the @HG assistant cog,
without the assistant needing to know that skill exists at all.

A skill calls `register(...)` — typically from its own `cog_load` — to
expose one of its own "core" functions (the same one its slash command
already calls) under a name, a plain-language description, and a
Pydantic model describing its parameters. The assistant cog only ever
talks to this module; it has no direct imports of laundry, scheduler,
shopping, or any future skill (casino, chess, ...). Adding a new skill
never requires touching this file or the assistant cog.

Deliberately not exposed here: anything that would make @HG a
*participant* rather than a translator (playing a chess move, placing
a casino bet, deciding what's for dinner). A skill should only ever
register its own "set this up" / "check on this" actions — that's a
per-skill judgment call, enforced by convention, not by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Type

import discord
from pydantic import BaseModel


class ActionResult(BaseModel):
    """What every registered action must return, so @HG can honestly
    describe what actually happened instead of assuming success."""

    success: bool
    summary: str
    warning: Optional[str] = None


@dataclass
class NLPContext:
    """Real Discord context for one action call, supplied by the
    assistant cog from the actual message — never something the LLM
    fills in itself."""

    bot: discord.Client
    guild_id: int
    channel: discord.abc.Messageable
    speaker_name: Optional[str]
    speaker_user_id: int


@dataclass
class NLPAction:
    name: str
    description: str
    params_model: Type[BaseModel]
    handler: Callable[[NLPContext, BaseModel], Awaitable[ActionResult]]
    # None = available in any channel. Set this to a skill's own dedicated
    # channel ID (e.g. config.CHORE_CHANNEL_ID) whenever the action could
    # plausibly be confused with another skill's similarly-shaped action
    # ("add/assign this thing to that entity") -- see actions_for_channel.
    channel_id: Optional[int] = None


_REGISTRY: dict[str, NLPAction] = {}


def register(
    name: str,
    description: str,
    params_model: Type[BaseModel],
    handler: Callable[[NLPContext, BaseModel], Awaitable[ActionResult]],
    channel_id: Optional[int] = None,
) -> None:
    _REGISTRY[name] = NLPAction(
        name=name, description=description, params_model=params_model, handler=handler, channel_id=channel_id
    )


def all_actions() -> list[NLPAction]:
    return list(_REGISTRY.values())


def actions_for_channel(channel_id: int) -> list[NLPAction]:
    """The actions @HG should even consider for a message in this channel.

    If this channel is some skill's own dedicated channel, restrict to
    just that skill's actions (plus anything globally available) -- this
    is what actually prevents e.g. a chore-shaped request in the chore
    channel from getting misclassified as a shopping-list action, rather
    than just hoping the model's wording-only judgment gets it right.
    Outside any skill's dedicated channel, there's no such signal to
    narrow things down, so every action stays available -- @HG still
    works from anywhere that isn't a skill's own claimed turf.
    """
    scoped = [a for a in _REGISTRY.values() if a.channel_id == channel_id]
    if scoped:
        global_actions = [a for a in _REGISTRY.values() if a.channel_id is None]
        return scoped + global_actions
    return list(_REGISTRY.values())


def get_action(name: str) -> Optional[NLPAction]:
    return _REGISTRY.get(name)


def clear() -> None:
    """Test helper only — reset the registry between test runs."""
    _REGISTRY.clear()
