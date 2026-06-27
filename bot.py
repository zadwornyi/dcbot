"""Точка входа Discord-бота экономики.

Запуск:  python bot.py
"""
import asyncio
import logging

import discord
from discord.ext import commands

import config
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dcbot")

intents = discord.Intents.default()
intents.message_content = True  # нужно для текстовых команд через !
intents.members = True          # нужно для разрешения участников/упоминаний


class EconomyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or(config.PREFIX),
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        await db.init_db()
        await self.load_extension("cogs.economy")
        await self.load_extension("cogs.welcome")
        await self.load_extension("cogs.gameroles")
        await self.load_extension("cogs.composition")
        await self.load_extension("cogs.reminder")
        await self.load_extension("cogs.help")

        # Постоянная панель выбора ролей — переживает перезапуск.
        from cogs.gameroles import PlayerPanelView
        self.add_view(PlayerPanelView())

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Слэш-команды синхронизированы для сервера %s (%d шт.)", config.GUILD_ID, len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Слэш-команды синхронизированы глобально (%d шт.). Появятся в течение часа.", len(synced))

    async def on_ready(self):
        log.info("Бот запущен как %s (id: %s)", self.user, self.user.id)
        if config.ECONOMY_CHANNEL_ID:
            log.info("Канал экономики для не-админов: %s", config.ECONOMY_CHANNEL_ID)
        else:
            log.warning("ECONOMY_CHANNEL_ID не задан — не-админы не смогут пользоваться командами экономики.")


async def main():
    if not config.TOKEN:
        raise SystemExit("Не задан DISCORD_TOKEN. Скопируйте .env.example в .env и впишите токен.")
    bot = EconomyBot()
    async with bot:
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
