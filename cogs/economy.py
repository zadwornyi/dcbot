"""Ког экономики: баланс, переводы, лидерборд, админ-команды.

Все команды гибридные (@commands.hybrid_command) — работают и через префикс (!cmd),
и через слэш (/cmd).
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db

CURRENCY = config.CURRENCY_NAME
EMOJI = config.CURRENCY_EMOJI
COLOR = config.EMBED_COLOR


def fmt(amount: int) -> str:
    """Форматирует сумму: 12 345 🪙 серебра."""
    return f"**{amount:,}** {EMOJI} {CURRENCY}".replace(",", " ")


# --- Кастомные проверки -----------------------------------------------------

class WrongChannel(commands.CheckFailure):
    pass


def member_is_admin(member: discord.Member) -> bool:
    """Админ бота = право 'Администратор' в Discord ИЛИ роль из ADMIN_ROLE_ID."""
    if member.guild_permissions.administrator:
        return True
    if config.ADMIN_ROLE_ID and any(r.id == config.ADMIN_ROLE_ID for r in member.roles):
        return True
    return False


def admin_only():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if member_is_admin(ctx.author):
            return True
        raise commands.MissingPermissions(["administrator"])

    return commands.check(predicate)


def economy_channel_only():
    """Не-админы могут вызывать команду только в назначенном канале. Админы — везде."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if member_is_admin(ctx.author):
            return True
        if config.ECONOMY_CHANNEL_ID and ctx.channel.id == config.ECONOMY_CHANNEL_ID:
            return True
        raise WrongChannel()

    return commands.check(predicate)


# --- Пагинация лидерборда ---------------------------------------------------

class LeaderboardView(discord.ui.View):
    def __init__(self, entries: list[tuple[int, int]], author_id: int, per_page: int = 10):
        super().__init__(timeout=120)
        self.entries = entries
        self.author_id = author_id
        self.per_page = per_page
        self.page = 0
        self.max_page = max(0, (len(entries) - 1) // per_page)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_page

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.entries[start:start + self.per_page]

        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = []
        for i, (user_id, balance) in enumerate(chunk):
            rank = start + i
            badge = medals.get(rank, f"`#{rank + 1}`")
            bal = f"{balance:,}".replace(",", " ")
            lines.append(f"{badge} <@{user_id}> — **{bal}** {EMOJI}")

        embed = discord.Embed(
            title=f"{EMOJI} Таблица лидеров — {CURRENCY}",
            description="\n".join(lines) if lines else "Пока ни у кого нет серебра.",
            color=COLOR,
        )
        embed.set_footer(
            text=f"Страница {self.page + 1}/{self.max_page + 1} • всего участников: {len(self.entries)}"
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Листать может только тот, кто вызвал команду.", ephemeral=True
            )
            return False
        return True

    async def _update(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="◀ Назад", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="Вперёд ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.max_page:
            self.page += 1
        await self._update(interaction)


# --- Сам ког ----------------------------------------------------------------

class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- Пользовательские команды ----

    @commands.hybrid_command(name="bal", description="Показать баланс серебра")
    @app_commands.describe(member="Чей баланс посмотреть (по умолчанию — свой)")
    @commands.guild_only()
    @economy_channel_only()
    async def bal(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author
        balance = await db.get_balance(target.id)

        num = f"{balance:,}".replace(",", " ")
        embed = discord.Embed(color=COLOR)
        if target.id != ctx.author.id:
            embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name=f"{EMOJI} Баланс серебра:", value=f"**{num}**", inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="transfer", description="Передать серебро другому участнику")
    @app_commands.describe(member="Кому передать", amount="Сколько серебра")
    @commands.guild_only()
    @economy_channel_only()
    async def transfer(self, ctx: commands.Context, member: discord.Member, amount: int):
        if amount <= 0:
            return await self._error(ctx, "Сумма должна быть больше нуля.")
        if member.id == ctx.author.id:
            return await self._error(ctx, "Нельзя перевести серебро самому себе.")
        if member.bot:
            return await self._error(ctx, "Ботам серебро не передаётся.")

        ok, sender_left = await db.transfer(ctx.author.id, member.id, amount)
        if not ok:
            return await self._error(
                ctx,
                f"Недостаточно серебра. На балансе только {fmt(sender_left)}.",
            )

        embed = discord.Embed(
            title="✅ Перевод выполнен",
            description=(
                f"{ctx.author.mention} → {member.mention}\n"
                f"Сумма: {fmt(amount)}\n"
                f"Ваш остаток: {fmt(sender_left)}"
            ),
            color=0x57F287,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="lb", description="Таблица лидеров по серебру")
    @commands.guild_only()
    @economy_channel_only()
    async def lb(self, ctx: commands.Context):
        entries = await db.get_leaderboard()
        view = LeaderboardView(entries, author_id=ctx.author.id)
        # Если страница одна — кнопки не нужны
        message_view = view if view.max_page > 0 else None
        await ctx.reply(embed=view.build_embed(), view=message_view, mention_author=False)

    # ---- Админ-команды ----

    @commands.hybrid_command(
        name="give-balance", aliases=["gb"], description="[Админ] Начислить серебро участнику"
    )
    @app_commands.describe(member="Кому начислить", amount="Сколько серебра")
    @commands.guild_only()
    @admin_only()
    async def give_balance(self, ctx: commands.Context, member: discord.Member, amount: int):
        if amount <= 0:
            return await self._error(ctx, "Сумма должна быть больше нуля.")

        new_balance = await db.change_balance(member.id, amount)
        embed = discord.Embed(
            title="➕ Начисление",
            description=(
                f"{member.mention} получает {fmt(amount)}\n"
                f"Новый баланс: {fmt(new_balance)}"
            ),
            color=0x57F287,
        )
        embed.set_footer(text=f"Администратор: {ctx.author.display_name}")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        name="remove-balance", aliases=["rb"], description="[Админ] Снять серебро с участника"
    )
    @app_commands.describe(member="У кого снять", amount="Сколько серебра")
    @commands.guild_only()
    @admin_only()
    async def remove_balance(self, ctx: commands.Context, member: discord.Member, amount: int):
        if amount <= 0:
            return await self._error(ctx, "Сумма должна быть больше нуля.")

        # Админ может уводить баланс в минус (allow_negative=True).
        new_balance = await db.change_balance(member.id, -amount, allow_negative=True)
        embed = discord.Embed(
            title="➖ Списание",
            description=(
                f"У {member.mention} снято {fmt(amount)}\n"
                f"Новый баланс: {fmt(new_balance)}"
            ),
            color=0xED4245,
        )
        embed.set_footer(text=f"Администратор: {ctx.author.display_name}")
        await ctx.reply(embed=embed, mention_author=False)

    # ---- Вспомогательное ----

    async def _error(self, ctx: commands.Context, text: str):
        embed = discord.Embed(description=f"⚠️ {text}", color=0xED4245)
        await ctx.reply(embed=embed, mention_author=False, ephemeral=True)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        if isinstance(error, WrongChannel):
            ch = f"<#{config.ECONOMY_CHANNEL_ID}>" if config.ECONOMY_CHANNEL_ID else "специальном канале"
            await self._error(ctx, f"Команды экономики доступны только в канале {ch}.")
        elif isinstance(error, commands.MissingPermissions):
            await self._error(ctx, "Эта команда только для администраторов сервера.")
        elif isinstance(error, commands.NoPrivateMessage):
            await self._error(ctx, "Команда работает только на сервере, не в личных сообщениях.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                                commands.MemberNotFound, commands.UserInputError)):
            await self._error(ctx, "Неверные аргументы. Пример: `!transfer @ник 100`")
        else:
            await self._error(ctx, "Произошла ошибка при выполнении команды.")
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
