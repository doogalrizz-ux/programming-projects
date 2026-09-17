"""
@HG — natural-language bridge into whatever HomeGhost skills have opted
into it via nlp_registry. Strictly a translator: it never plays a game,
places a bet, or acts as a participant in anything — it only ever runs
actions that some other skill explicitly registered as safe to expose,
then honestly reports what actually happened.

Flow for one @-mention:
1. Gate: only real Discord @-mentions of the bot trigger anything at
   all (never plain text, never every message in a channel).
2. Extract: one Gemini call turns the message into zero or more
   {action, params} pairs, using the registry's current catalog as the
   only things it's allowed to name. Zero actions means either pure
   chit-chat or a clarifying question — both come back as direct_reply.
3. Execute: each action really runs, via the registry's handler, and
   returns a real ActionResult — success or failure, never assumed.
4. Reply: a second, small Gemini call turns the *real* outcomes into
   one short, in-character reply. This step never runs before step 3,
   so it can't cheerfully confirm something that actually failed.

Not scoped to any particular channel — unlike the slash commands, @HG
works anywhere it can see a message, since the whole point is not
needing to remember which channel a given skill lives in.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Optional

import discord
from discord.ext import commands
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

import config
import nlp_registry
from nlp_registry import ActionResult, NLPContext

log = logging.getLogger("homeghost.assistant")


class ProposedAction(BaseModel):
    action: str = Field(description="Exact action name to run, from the provided list of available actions.")
    # A JSON-encoded STRING, not a nested object: the Gemini Developer API's
    # structured-output mode rejects any open-ended object (it needs
    # "additionalProperties", which only Enterprise mode supports), and each
    # action's parameter shape is different anyway. Keeping the outer schema
    # to plain strings/arrays sidesteps that entirely; the actual shape gets
    # validated on the way back in via the action's own Pydantic model.
    params: str = Field(
        default="{}",
        description='A JSON-encoded object string with this action\'s parameters, e.g. \'{"item": "milk", "quantity": 2}\'. Use "{}" if it takes none.',
    )


class AssistantTurn(BaseModel):
    actions: list[ProposedAction] = Field(
        default_factory=list,
        description="Zero or more actions to run. Leave empty for pure conversation, or when you need to "
        "ask a clarifying question instead of guessing.",
    )
    direct_reply: Optional[str] = Field(
        default=None,
        description="Used only when actions is empty: either a warm conversational reply, or a short "
        "clarifying question if something required is missing and can't be reasonably guessed.",
    )


def _build_system_instruction(actions: list) -> str:
    lines = [
        "You are HomeGhost, nicknamed HG, a household assistant bot in a family's Discord server.",
        "You are ONLY a translator between natural language and the household's actual systems.",
        "You never act as a participant: you don't play games, place bets, or make decisions for someone.",
        "You only ever set things up, check on things, or make brief friendly small talk.",
        "",
        f"Family roster: {config.FAMILY_MEMBERS}."
        + (f" Of these, the kids are: {config.KIDS}." if config.KIDS else "")
        + " When told to do something for 'the kids', 'everyone', or a named group, "
        "expand it into one separate action per person in that group -- the same way "
        "a message naming several items becomes several separate actions.",
        "",
        "Each action you choose to run needs a `params` value: a JSON-encoded STRING "
        'containing that action\'s parameters as an object, e.g. \'{"item": "milk", "quantity": 2}\'. '
        'Not a nested object -- an actual string containing JSON text. Use "{}" for no parameters.',
        "",
        "Available actions:",
    ]
    for action in actions:
        schema = action.params_model.model_json_schema()
        props = schema.get("properties", {})
        required = schema.get("required", [])
        param_bits = []
        for pname, pinfo in props.items():
            desc = pinfo.get("description", "")
            req = "required" if pname in required else "optional"
            param_bits.append(f"{pname} ({req}): {desc}")
        lines.append(f"- {action.name}: {action.description}")
        if param_bits:
            lines.append(f"  parameters: {'; '.join(param_bits)}")
    lines += [
        "",
        "Given the user's message, decide which action(s) apply. A message naming several "
        "items or events means several separate actions, one per item.",
        "If the message is just conversation (thanks, a greeting, banter) with no action, "
        "leave actions empty and put a short, warm, in-character reply in direct_reply.",
        "If something required is genuinely missing and you can't reasonably guess it, leave "
        "actions empty and ask a short clarifying question in direct_reply instead of guessing.",
        "Keep replies brief and warm, like a helpful housemate, not a formal assistant.",
    ]
    return "\n".join(lines)


class Assistant(commands.Cog):
    """Natural-language bridge; see module docstring for the full flow."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Without an explicit timeout, a stuck network path (firewall
        # silently dropping the connection, etc.) hangs this call
        # forever instead of raising -- which means on_message never
        # replies AND never logs anything, since nothing ever actually
        # fails. 30s is generous for a small classification request.
        self.client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=30_000),
        )
        self._daily_count = 0
        self._daily_date = date.today()

    # --------------------------------------------------------------- helpers

    def _check_and_increment_daily_cap(self) -> bool:
        today = date.today()
        if today != self._daily_date:
            self._daily_date = today
            self._daily_count = 0
        if self._daily_count >= config.ASSISTANT_MAX_DAILY_REQUESTS:
            return False
        self._daily_count += 1
        return True

    def _resolve_speaker(self, member: discord.Member) -> Optional[str]:
        display = member.display_name.strip().lower()
        for name in config.FAMILY_MEMBERS:
            if name.strip().lower() == display:
                return name
        return None

    def _strip_mention(self, message: discord.Message) -> str:
        content = message.content
        for pattern in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            content = content.replace(pattern, "")
        return content.strip()

    async def _extract(self, content: str, speaker_name: Optional[str], actions: list) -> AssistantTurn:
        prompt = f"Speaker: {speaker_name or 'unknown family member'}\nMessage: {content}"
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_build_system_instruction(actions),
                response_mime_type="application/json",
                response_schema=AssistantTurn,
            ),
        )
        return AssistantTurn.model_validate_json(response.text)

    async def _draft_reply(self, original_message: str, speaker_name: Optional[str], results: list[ActionResult]) -> str:
        outcome_lines = []
        for r in results:
            line = ("Succeeded: " if r.success else "Failed: ") + r.summary
            if r.warning:
                line += f" (note: {r.warning})"
            outcome_lines.append(line)
        prompt = (
            f"Speaker: {speaker_name or 'unknown family member'}\n"
            f"Their message: {original_message}\n"
            "What actually happened:\n" + "\n".join(outcome_lines) + "\n\n"
            "Write ONE short, warm, in-character reply describing the real outcome above. "
            "If something failed, say so honestly and briefly -- don't pretend it succeeded. "
            "Address them by name if you know it. No more than two sentences."
        )
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="You are HomeGhost (HG), a friendly household assistant. Reply in plain text, not JSON.",
            ),
        )
        return response.text.strip()

    # ---------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if self.bot.user not in message.mentions:
            return

        content = self._strip_mention(message)
        if not content:
            return

        if not self._check_and_increment_daily_cap():
            await message.reply("I've hit my request limit for today — try the slash commands directly.")
            return

        speaker_name = self._resolve_speaker(message.author)
        log.info("@HG mention from %s: %r", speaker_name or message.author, content)

        # Restrict to this channel's actions when it's a skill's own
        # dedicated channel (e.g. the chore channel only ever offers chore
        # actions) -- this is what actually prevents cross-skill mix-ups
        # like a chore-shaped request getting misread as a shopping-list
        # one, rather than hoping wording alone disambiguates correctly.
        available_actions = nlp_registry.actions_for_channel(message.channel.id)

        try:
            turn = await self._extract(content, speaker_name, available_actions)
        except Exception:
            log.exception("Gemini extraction call failed")
            await message.reply("Sorry, I'm having trouble understanding right now — try the slash command directly.")
            return

        log.info("@HG extracted %d action(s): %s", len(turn.actions), [a.action for a in turn.actions])

        if not turn.actions:
            await message.reply(turn.direct_reply or "I'm not sure what you'd like me to do there.")
            return

        ctx = NLPContext(
            bot=self.bot,
            guild_id=message.guild.id,
            channel=message.channel,
            speaker_name=speaker_name,
            speaker_user_id=message.author.id,
        )

        available_names = {a.name for a in available_actions}
        results: list[ActionResult] = []
        for proposed in turn.actions:
            registered = nlp_registry.get_action(proposed.action)
            if registered is None or proposed.action not in available_names:
                # The second case shouldn't happen -- the model was only
                # offered available_actions -- but a hallucinated name is
                # cheap to guard against rather than trust blindly.
                results.append(ActionResult(success=False, summary=f"'{proposed.action}' isn't something I know how to do."))
                continue
            try:
                params_dict = json.loads(proposed.params)
                params = registered.params_model.model_validate(params_dict)
            except (json.JSONDecodeError, ValidationError):
                results.append(ActionResult(success=False, summary=f"Couldn't understand the details for {proposed.action}."))
                continue
            try:
                result = await registered.handler(ctx, params)
            except Exception:
                log.exception("NLP action '%s' failed", proposed.action)
                result = ActionResult(success=False, summary=f"'{proposed.action}' failed unexpectedly.")
            results.append(result)

        try:
            reply = await self._draft_reply(content, speaker_name, results)
        except Exception:
            log.exception("Gemini reply-drafting call failed")
            reply = "Done: " + "; ".join(r.summary for r in results)

        await message.reply(reply)


async def setup(bot: commands.Bot):
    await bot.add_cog(Assistant(bot))
