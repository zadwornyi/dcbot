"""Ког приветствия: при входе нового участника бот пишет ему в ЛС,
а если личка закрыта — в резервный welcome-канал.

Текст приветствия редактируется в welcome.json (рядом с bot.py) и перечитывается
при КАЖДОМ входе участника — перезапуск бота для смены текста не нужен.
Список каналов берётся из config.WELCOME_CHANNELS (.env).
"""
import json
import logging
import os

import discord
from discord.ext import commands

import config

log = logging.getLogger("dcbot.welcome")

WELCOME_JSON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "welcome.json")

# Значения по умолчанию — используются, если в JSON какого-то ключа нет
# или файл не найден/повреждён.
DEFAULTS = {
    "title": "Welcome to {guild}! 👋",
    "intro": "Glad to have you here! Here are the channels to check out first:",
    "channel_line": "📌 **{label}** → <#{id}>",
    "channel_line_no_label": "📌 <#{id}>",
    "no_channels": "The channel list will be added soon.",
    "footer": "See you around!",
    "color": config.EMBED_COLOR,
    "show_guild_icon": True,
}


def _load_template() -> dict:
    """Читает welcome.json и дополняет недостающие ключи дефолтами."""
    data = {}
    try:
        with open(WELCOME_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.warning("welcome.json не найден — использую текст по умолчанию.")
    except (json.JSONDecodeError, OSError) as e:
        log.warning("welcome.json не прочитан (%s) — использую текст по умолчанию.", e)
    return {**DEFAULTS, **data}


def _parse_color(val) -> int:
    if isinstance(val, int):
        return val
    s = str(val).strip().lstrip("#")
    if s.lower().startswith("0x"):
        s = s[2:]
    try:
        return int(s, 16)
    except ValueError:
        return config.EMBED_COLOR


def _render(text: str, member: discord.Member) -> str:
    """Подставляет плейсхолдеры. Доступны: {guild}, {member}, {count}."""
    return (
        str(text)
        .replace("{guild}", member.guild.name)
        .replace("{member}", member.mention)
        .replace("{count}", str(member.guild.member_count or 0))
    )


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def build_embed(self, member: discord.Member) -> discord.Embed:
        tpl = _load_template()

        lines = []
        for label, cid in config.WELCOME_CHANNELS:
            if label:
                line = tpl["channel_line"].replace("{label}", label)
            else:
                line = tpl["channel_line_no_label"]
            lines.append(line.replace("{id}", str(cid)))
        guide = "\n".join(lines) if lines else tpl["no_channels"]

        embed = discord.Embed(
            title=_render(tpl["title"], member),
            description=f"{_render(tpl['intro'], member)}\n\n{guide}",
            color=_parse_color(tpl["color"]),
        )
        if tpl.get("show_guild_icon", True) and member.guild.icon:
            embed.set_thumbnail(url=member.guild.icon.url)
        if tpl.get("footer"):
            embed.set_footer(text=_render(tpl["footer"], member))
        return embed

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        embed = self.build_embed(member)

        # 1) Пытаемся написать в личку.
        try:
            await member.send(embed=embed)
            return
        except discord.Forbidden:
            log.info("У %s закрыты ЛС — пробуем welcome-канал.", member)
        except discord.HTTPException as e:
            log.warning("Не удалось отправить ЛС %s: %s", member, e)

        # 2) Фолбэк: пишем в резервный канал с упоминанием.
        if not config.WELCOME_CHANNEL_ID:
            return
        channel = member.guild.get_channel(config.WELCOME_CHANNEL_ID)
        if channel is None:
            log.warning("WELCOME_CHANNEL_ID=%s не найден на сервере.", config.WELCOME_CHANNEL_ID)
            return
        try:
            await channel.send(content=member.mention, embed=embed)
        except discord.HTTPException as e:
            log.warning("Не удалось написать в welcome-канал: %s", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
