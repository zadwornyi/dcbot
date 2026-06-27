"""Ког помощи: команда !help / /help.

Показывает только те команды, что доступны вызвавшему, в зависимости от его роли:
обычный участник видит игровые команды, администратор — дополнительно админ-блок.
"""
import discord
from discord.ext import commands

import config
from cogs.economy import member_is_admin

P = config.PREFIX
COLOR = config.EMBED_COLOR


def _block(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{cmd}` — {desc}" for cmd, desc in rows)


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Показать доступные тебе команды")
    async def help(self, ctx: commands.Context):
        is_admin = isinstance(ctx.author, discord.Member) and member_is_admin(ctx.author)

        embed = discord.Embed(
            title="📖 Доступные команды",
            description=(f"Роль: **{'администратор' if is_admin else 'участник'}**.\n"
                        f"Команды работают и с префиксом `{P}`, и как слэш `/`."),
            color=COLOR,
        )

        embed.add_field(
            name="💰 Экономика",
            value=_block([
                (f"{P}bal [@кто]", "показать баланс серебра"),
                (f"{P}transfer @кому сумма", "передать серебро другому участнику"),
                (f"{P}lb", "таблица лидеров"),
            ]),
            inline=False,
        )

        embed.add_field(
            name="🎮 Игровые роли",
            value=("Выбор ролей — через панель в специальном канале:\n"
                   "**📝 Выбрать роли** — выбрать до "
                   f"{config.MAX_GAME_ROLES} игровых ролей\n"
                   "**📋 Мои роли** — посмотреть свои роли, приоритеты и статус заявки"),
            inline=False,
        )

        if is_admin:
            embed.add_field(
                name="🛠️ Администрирование",
                value=_block([
                    (f"{P}give-balance @кому сумма  ·  {P}gb", "начислить серебро"),
                    (f"{P}remove-balance @кому сумма  ·  {P}rb", "снять серебро (можно в минус)"),
                    (f"{P}setup-game-panel", "поставить панель выбора ролей в текущий канал"),
                    (f"{P}review @игрок", "открыть панель игрока: задать/убрать роли и приоритеты"),
                    (f"{P}roster", "список всех игроков и их ролей с приоритетами"),
                    (f"{P}sets", "список наборов ролей (sets/*.json)"),
                    (f"{P}compose <набор> <ссылка/id>", "собрать состав по реакциям на сообщение"),
                    (f"{P}composition <набор>", "открыть панель правки состава"),
                    (f"{P}publish <набор> [канал]", "опубликовать состав игрокам"),
                    (f"{P}vc-check <набор>", "кого из состава нет в твоём голосовом канале"),
                    (f"{P}split <набор> сумма", "поровну раздать серебро назначенным игрокам состава"),
                    (f"{P}reminder <пост>", "позвать в ЛС участников, не отметившихся реакцией под постом"),
                ]),
                inline=False,
            )
            embed.set_footer(text="Команды экономики тебе доступны в любом канале.")
        else:
            ch = f"<#{config.ECONOMY_CHANNEL_ID}>" if config.ECONOMY_CHANNEL_ID else "специальном канале"
            embed.description += f"\nКоманды экономики работают в канале {ch}."

        await ctx.reply(embed=embed, mention_author=False, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
