"""Ког напоминаний: команда !reminder <пост>.

Смотрит, кто поставил реакции под указанным сообщением, и пишет в ЛС всем
участникам с ролью MEMBER_ROLE_ID, кто реакцию НЕ поставил, — зовёт их под пост.

Команда только для админов и просит подтверждение перед массовой рассылкой,
потому что отправленные ЛС нельзя отозвать.
"""
import asyncio
import logging

import discord
from discord.ext import commands

import config
from cogs.economy import admin_only

log = logging.getLogger("dcbot.reminder")

COLOR = config.EMBED_COLOR

# Текст приглашения, уходящего в ЛС. {guild} — название сервера.
REMINDER_TITLE = "⚔️ Warrior, we need you here!"
REMINDER_BODY = (
    "На сервере **{guild}** идёт сбор, а тебя ещё нет под постом.\n"
    "Загляни и отметься реакцией — каждый воин на счету! 👇"
)

# Пауза между сообщениями, чтобы не упереться в лимиты Discord на рассылку ЛС.
DM_DELAY_SECONDS = 1.0


class ConfirmView(discord.ui.View):
    """Кнопки «Разослать / Отмена». value: True — подтвердил, False — отменил, None — таймаут."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Подтвердить может только тот, кто вызвал команду.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="📣 Разослать", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()


class Reminder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="reminder")
    @commands.guild_only()
    @admin_only()
    async def reminder(self, ctx: commands.Context, message: discord.Message):
        """Зовёт в ЛС участников с ролью, не отметившихся реакцией под постом.

        <пост> — ссылка на сообщение (или его ID, или channel_id-message_id).
        """
        guild = ctx.guild

        # Роль участника должна быть настроена и существовать на сервере.
        if not config.MEMBER_ROLE_ID:
            return await self._error(
                ctx, "Не задан `MEMBER_ROLE_ID` в .env — некого оповещать."
            )
        role = guild.get_role(config.MEMBER_ROLE_ID)
        if role is None:
            return await self._error(
                ctx, f"Роль участника (ID `{config.MEMBER_ROLE_ID}`) не найдена на сервере."
            )

        # 1) Собираем всех, кто поставил ЛЮБУЮ реакцию под сообщением.
        reacted: set[int] = set()
        for reaction in message.reactions:
            async for user in reaction.users():
                reacted.add(user.id)

        # 2) Участники с ролью, кто реакцию НЕ поставил (ботов пропускаем).
        targets = [m for m in role.members if not m.bot and m.id not in reacted]

        if not targets:
            embed = discord.Embed(
                description=(
                    f"Все участники роли {role.mention} уже отметились "
                    "под постом — звать некого 🎉"
                ),
                color=COLOR,
            )
            return await ctx.reply(embed=embed, mention_author=False)

        # 3) Подтверждение перед массовой рассылкой.
        confirm_embed = discord.Embed(
            title="📣 Подтверди рассылку",
            description=(
                f"Под [этим постом]({message.jump_url}) нет реакции у "
                f"**{len(targets)}** участник(ов) с ролью {role.mention}.\n\n"
                "Разослать им приглашение в личные сообщения?"
            ),
            color=COLOR,
        )
        confirm_embed.set_footer(text="Отправленные ЛС нельзя отозвать.")
        view = ConfirmView(ctx.author.id)
        prompt = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        await view.wait()

        if view.value is None:
            return await prompt.edit(
                embed=discord.Embed(description="⏳ Время вышло — рассылка отменена.", color=COLOR),
                view=None,
            )
        if view.value is False:
            return await prompt.edit(
                embed=discord.Embed(description="❌ Рассылка отменена.", color=COLOR),
                view=None,
            )

        # 4) Рассылаем.
        await prompt.edit(
            embed=discord.Embed(
                description=f"📨 Рассылаю приглашения {len(targets)} участникам…",
                color=COLOR,
            ),
            view=None,
        )

        sent, failed = 0, 0
        for member in targets:
            dm_embed = discord.Embed(
                title=REMINDER_TITLE,
                description=(
                    REMINDER_BODY.format(guild=guild.name)
                    + f"\n\n🔗 [Перейти к посту]({message.jump_url})"
                ),
                color=COLOR,
            )
            if guild.icon:
                dm_embed.set_thumbnail(url=guild.icon.url)

            link_view = discord.ui.View(timeout=None)
            link_view.add_item(
                discord.ui.Button(label="Перейти к посту", url=message.jump_url, emoji="⚔️")
            )

            try:
                await member.send(embed=dm_embed, view=link_view)
                sent += 1
            except discord.Forbidden:
                failed += 1  # закрытые ЛС
                log.info("У %s закрыты ЛС — приглашение не доставлено.", member)
            except discord.HTTPException as e:
                failed += 1
                log.warning("Не удалось написать %s: %s", member, e)

            await asyncio.sleep(DM_DELAY_SECONDS)

        # 5) Итоговый отчёт.
        result = discord.Embed(
            title="✅ Рассылка завершена",
            color=0x57F287,
        )
        result.add_field(name="Приглашено", value=f"**{sent}**", inline=True)
        if failed:
            result.add_field(name="Закрытые ЛС", value=f"**{failed}**", inline=True)
        result.add_field(name="Отметились ранее", value=f"**{len(reacted)}**", inline=True)
        result.set_footer(text=f"Команду вызвал {ctx.author.display_name}")
        await prompt.edit(embed=result)

    # ---- Вспомогательное ----

    async def _error(self, ctx: commands.Context, text: str):
        embed = discord.Embed(description=f"⚠️ {text}", color=0xED4245)
        await ctx.reply(embed=embed, mention_author=False)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        if isinstance(error, commands.MissingPermissions):
            await self._error(ctx, "Команда `!reminder` только для администраторов.")
        elif isinstance(error, commands.NoPrivateMessage):
            await self._error(ctx, "Команда работает только на сервере, не в ЛС.")
        elif isinstance(error, (commands.MessageNotFound, commands.ChannelNotReadable)):
            await self._error(
                ctx,
                "Не нашёл такое сообщение. Укажи ссылку на пост "
                "(ПКМ по сообщению → «Копировать ссылку») или его ID.",
            )
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                                commands.UserInputError)):
            await self._error(ctx, "Укажи пост. Пример: `!reminder <ссылка-на-сообщение>`")
        else:
            await self._error(ctx, "Произошла ошибка при выполнении команды.")
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminder(bot))
