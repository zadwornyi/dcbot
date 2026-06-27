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
    "A muster is underway on **{guild}**, and you're not under the post yet.\n"
    "Check it out and mark yourself with a reaction — every warrior counts! 👇"
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
                "Only the person who ran the command can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="📣 Send", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
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
                ctx, "`MEMBER_ROLE_ID` is not set in .env — no one to notify."
            )
        role = guild.get_role(config.MEMBER_ROLE_ID)
        if role is None:
            return await self._error(
                ctx, f"Member role (ID `{config.MEMBER_ROLE_ID}`) not found on this server."
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
                    f"Everyone with the {role.mention} role has already reacted "
                    "to the post — no one left to call 🎉"
                ),
                color=COLOR,
            )
            return await ctx.reply(embed=embed, mention_author=False)

        # 3) Подтверждение перед массовой рассылкой.
        confirm_embed = discord.Embed(
            title="📣 Confirm the broadcast",
            description=(
                f"**{len(targets)}** member(s) with the {role.mention} role haven't "
                f"reacted to [this post]({message.jump_url}).\n\n"
                "Send them an invite via direct message?"
            ),
            color=COLOR,
        )
        confirm_embed.set_footer(text="Sent DMs cannot be recalled.")
        view = ConfirmView(ctx.author.id)
        prompt = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        await view.wait()

        if view.value is None:
            return await prompt.edit(
                embed=discord.Embed(description="⏳ Timed out — broadcast cancelled.", color=COLOR),
                view=None,
            )
        if view.value is False:
            return await prompt.edit(
                embed=discord.Embed(description="❌ Broadcast cancelled.", color=COLOR),
                view=None,
            )

        # 4) Рассылаем.
        await prompt.edit(
            embed=discord.Embed(
                description=f"📨 Sending invites to {len(targets)} member(s)…",
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
                    + f"\n\n🔗 [Go to the post]({message.jump_url})"
                ),
                color=COLOR,
            )
            if guild.icon:
                dm_embed.set_thumbnail(url=guild.icon.url)

            link_view = discord.ui.View(timeout=None)
            link_view.add_item(
                discord.ui.Button(label="Go to the post", url=message.jump_url, emoji="⚔️")
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
            title="✅ Broadcast complete",
            color=0x57F287,
        )
        result.add_field(name="Invited", value=f"**{sent}**", inline=True)
        if failed:
            result.add_field(name="DMs closed", value=f"**{failed}**", inline=True)
        result.add_field(name="Already reacted", value=f"**{len(reacted)}**", inline=True)
        result.set_footer(text=f"Triggered by {ctx.author.display_name}")
        await prompt.edit(embed=result)

    # ---- Вспомогательное ----

    async def _error(self, ctx: commands.Context, text: str):
        embed = discord.Embed(description=f"⚠️ {text}", color=0xED4245)
        await ctx.reply(embed=embed, mention_author=False)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        if isinstance(error, commands.MissingPermissions):
            await self._error(ctx, "The `!reminder` command is for administrators only.")
        elif isinstance(error, commands.NoPrivateMessage):
            await self._error(ctx, "This command only works on a server, not in DMs.")
        elif isinstance(error, (commands.MessageNotFound, commands.ChannelNotReadable)):
            await self._error(
                ctx,
                "Couldn't find that message. Provide the post link "
                "(right-click the message → \"Copy Message Link\") or its ID.",
            )
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                                commands.UserInputError)):
            await self._error(ctx, "Specify a post. Example: `!reminder <message-link>`")
        else:
            await self._error(ctx, "An error occurred while running the command.")
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminder(bot))
