"""
HomeGhost — household automation bot.

Entry point: wires up the bot instance, the shared database connection,
and loads each skill as a cog. Laundry is the first skill; future ones
(shopping list, schedule, chess, ...) get added the same way, as their
own file under cogs/, sharing this bot instance and database.
"""
import logging

import discord
from discord.ext import commands

import config
import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("homeghost")

# Add new skills here as they're built. cogs.assistant is loaded
# conditionally below, since it's the one skill that's opt-in.
INITIAL_EXTENSIONS = [
    "cogs.laundry",
    "cogs.scheduler",
    "cogs.shopping",
    "cogs.chores",
]


class HomeGhost(commands.Bot):
    def __init__(self):
        # Slash commands + reactions don't need any privileged intents
        # (message content, members, presences). The one exception is
        # @HG's natural-language assistant, which has to read the actual
        # words in a message — only request that privileged intent when
        # the assistant is actually configured, so everyone else never
        # has to touch that Developer Portal toggle at all.
        intents = discord.Intents.default()
        if config.GEMINI_API_KEY:
            intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.db = await database.connect(config.DB_PATH)

        extensions = list(INITIAL_EXTENSIONS)
        if config.GEMINI_API_KEY:
            extensions.append("cogs.assistant")
        else:
            log.info("GEMINI_API_KEY not set — skipping @HG natural-language assistant.")

        for extension in extensions:
            await self.load_extension(extension)
            log.info("Loaded extension: %s", extension)

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced slash commands to guild %s (instant, dev mode)", config.GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global slash commands (can take up to an hour to appear)")

    async def close(self):
        if hasattr(self, "db"):
            await self.db.close()
        await super().close()


bot = HomeGhost()


@bot.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)


def main():
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
