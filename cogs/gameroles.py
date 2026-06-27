"""Ког игровых ролей.

Игрок в канале нажимает «Выбрать роли» → отмечает от 1 до 10 ролей (страницами
кнопок, каталог может быть любого размера) → отправляет заявку.
Администраторы в админ-канале получают заявку и интерактивной панелью
проставляют каждой роли приоритет (1–3), могут добавить/убрать роли и утвердить.
Игрок через «Мои роли» видит свои роли, приоритеты и статус.

Каталог ролей — roles.json (рядом с bot.py), перечитывается на лету.
"""
import json
import logging
import math
import os
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
from cogs.economy import member_is_admin, admin_only

log = logging.getLogger("dcbot.gameroles")

COLOR = config.EMBED_COLOR
MAX_ROLES = config.MAX_GAME_ROLES
PICK_PER_PAGE = 20      # ролей на странице выбора у игрока (4 ряда по 5)
REVIEW_PER_PAGE = 4     # ролей на странице ревью-панели админа (по ряду на роль)
ROSTER_PER_PAGE = 6     # игроков на странице общего списка ролей (/roster)

ROLES_JSON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "roles.json")


# --- Каталог ----------------------------------------------------------------

def load_catalog() -> list[dict]:
    """Читает roles.json. Возвращает список валидных ролей [{key,name,emoji,description}]."""
    try:
        with open(ROLES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.warning("roles.json не найден — каталог ролей пуст.")
        return []
    except (json.JSONDecodeError, OSError) as e:
        log.warning("roles.json не прочитан (%s) — каталог ролей пуст.", e)
        return []

    catalog = []
    for item in data if isinstance(data, list) else []:
        key = str(item.get("key", "")).strip()
        name = str(item.get("name", "")).strip()
        if not key or not name:
            continue
        catalog.append({
            "key": key,
            "name": name,
            "emoji": (item.get("emoji") or None),
            "description": (item.get("description") or None),
        })
    return catalog


def catalog_index() -> dict[str, dict]:
    return {r["key"]: r for r in load_catalog()}


def role_label(key: str, index: dict[str, dict] | None = None) -> str:
    """'🎯 Sniper' для известной роли, иначе сам ключ."""
    index = index if index is not None else catalog_index()
    role = index.get(key)
    if not role:
        return key
    emoji = f"{role['emoji']} " if role.get("emoji") else ""
    return f"{emoji}{role['name']}"


def fmt_time(iso: str) -> str:
    """ISO-строку → Discord-таймстамп '<t:...:R>' (относительное время)."""
    try:
        dt = datetime.fromisoformat(iso)
        return f"<t:{int(dt.timestamp())}:R>"
    except (ValueError, TypeError):
        return iso


# --- Постоянная панель игрока ----------------------------------------------

class PlayerPanelView(discord.ui.View):
    """Висит в канале выбора ролей. Постоянная (custom_id), переживает перезапуск."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Choose Roles", emoji="📝",
                       style=discord.ButtonStyle.primary, custom_id="gr:choose")
    async def choose(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "GameRoles" = interaction.client.get_cog("GameRoles")
        catalog = load_catalog()
        if not catalog:
            return await interaction.response.send_message(
                "The role catalog is empty right now. Check back later.", ephemeral=True)

        app = await db.get_application(interaction.user.id, interaction.guild_id)
        preselected = {k for k, _, _ in app["roles"]} if app else set()

        async def on_submit(inter: discord.Interaction, selected: list[str]):
            await db.submit_application(inter.user.id, inter.guild_id, selected)
            await cog.post_review(inter.guild, inter.user)
            idx = catalog_index()
            chosen = "\n".join(f"• {role_label(k, idx)}" for k in selected)
            embed = discord.Embed(
                title="✅ Request sent",
                description=("The admins will review it and set priorities.\n"
                             "Check the status with the “My Roles” button.\n\n"
                             f"**Selected roles:**\n{chosen}"),
                color=0x57F287,
            )
            await inter.response.edit_message(content=None, embed=embed, view=None)

        view = RolePickerView(
            catalog, preselected, MAX_ROLES, on_submit,
            title="📝 Choose game roles",
            intro=f"Select from 1 to {MAX_ROLES} roles and press “Done”.",
        )
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="My Roles", emoji="📋",
                       style=discord.ButtonStyle.secondary, custom_id="gr:mine")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        app = await db.get_application(interaction.user.id, interaction.guild_id)
        embed = build_my_roles_embed(interaction.user, app)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def build_my_roles_embed(user: discord.abc.User, app: dict | None) -> discord.Embed:
    if app is None or not app["roles"]:
        return discord.Embed(
            title="📋 My game roles",
            description="You don’t have a request yet. Press “Choose Roles” to pick some.",
            color=COLOR,
        )

    idx = catalog_index()
    if app["status"] == "pending":
        status_line = "⏳ Your request is under review by the admins."
    else:
        status_line = "✅ Your roles are approved."

    roles = sorted(app["roles"], key=lambda r: (r[1] is None, r[1] or 0, r[0]))
    lines = []
    for key, priority, _src in roles:
        prio = f"priority **{priority}**" if priority else "priority not set"
        lines.append(f"• {role_label(key, idx)} — {prio}")

    embed = discord.Embed(
        title="📋 My game roles",
        description=f"{status_line}\n\n" + "\n".join(lines),
        color=COLOR,
    )
    embed.set_footer(text="To change them, press “Choose Roles” again.")
    return embed


# --- Выбор ролей игроком (страницы кнопок-переключателей) -------------------

class RoleToggleButton(discord.ui.Button):
    def __init__(self, role: dict, selected: bool, row: int):
        super().__init__(
            label=role["name"][:80],
            emoji=role.get("emoji") or None,
            style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
            row=row,
        )
        self.role_key = role["key"]

    async def callback(self, interaction: discord.Interaction):
        view: "RolePickerView" = self.view
        if self.role_key in view.selected:
            view.selected.discard(self.role_key)
        elif len(view.selected) >= view.max_roles:
            return await interaction.response.send_message(
                f"You can select at most {view.max_roles} roles. "
                "Unselect another one to pick this.", ephemeral=True)
        else:
            view.selected.add(self.role_key)
        view.render()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class _PickerNav(discord.ui.Button):
    def __init__(self, label: str, delta: int, emoji: str):
        super().__init__(label=label, emoji=emoji,
                         style=discord.ButtonStyle.primary, row=4)
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        view: "RolePickerView" = self.view
        view.page = max(0, min(view.pages - 1, view.page + self.delta))
        view.render()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class _PickerDone(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction):
        view: "RolePickerView" = self.view
        if not view.selected:
            return await interaction.response.send_message(
                "Select at least one role.", ephemeral=True)
        await view.on_submit(interaction, list(view.selected))
        view.stop()


class _PickerCancel(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger, row=4)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Cancelled.", embed=None, view=None)
        self.view.stop()


class RolePickerView(discord.ui.View):
    """Постраничный выбор ролей кнопками. Используется и игроком, и админом (для добавления)."""

    def __init__(self, roles: list[dict], preselected: set[str], max_roles: int,
                 on_submit, title: str, intro: str):
        super().__init__(timeout=300)
        self.roles = roles
        self.selected = set(preselected)
        self.max_roles = max_roles
        self.on_submit = on_submit
        self.title = title
        self.intro = intro
        self.page = 0
        self.pages = max(1, math.ceil(len(roles) / PICK_PER_PAGE))
        self.render()

    def render(self) -> None:
        self.clear_items()
        start = self.page * PICK_PER_PAGE
        for i, role in enumerate(self.roles[start:start + PICK_PER_PAGE]):
            self.add_item(RoleToggleButton(role, role["key"] in self.selected, row=i // 5))
        if self.page > 0:
            self.add_item(_PickerNav("Back", -1, emoji="⬅️"))
        if self.page < self.pages - 1:
            self.add_item(_PickerNav("More roles", +1, emoji="➡️"))
        self.add_item(_PickerDone())
        self.add_item(_PickerCancel())

    def embed(self) -> discord.Embed:
        idx = catalog_index()
        desc = f"{self.intro}\n\nSelected: **{len(self.selected)}/{self.max_roles}**"
        if self.pages > 1:
            desc += f"  ·  page {self.page + 1}/{self.pages}"
        embed = discord.Embed(title=self.title, description=desc, color=COLOR)
        if self.selected:
            names = ", ".join(role_label(k, idx) for k in self.selected)
            embed.add_field(name="Selected roles", value=names[:1024], inline=False)
        return embed


# --- Ревью-панель администраторов ------------------------------------------

def build_review_embed(player: discord.abc.User | int, app: dict) -> discord.Embed:
    player_id = player.id if isinstance(player, discord.abc.User) else player
    idx = catalog_index()

    if app["status"] == "pending":
        status = "⏳ awaiting review"
        color = COLOR
    else:
        status = "✅ approved"
        color = 0x57F287

    roles = sorted(app["roles"], key=lambda r: (r[1] is None, r[1] or 0, r[0]))
    if roles:
        lines = []
        for key, priority, source in roles:
            prio = f"**{priority}**" if priority else "—"
            who = "player" if source == "player" else "admin"
            lines.append(f"{role_label(key, idx)} — priority: {prio}  ·  _{who}_")
        roles_block = "\n".join(lines)
    else:
        roles_block = "_no roles_"

    embed = discord.Embed(
        title="🎮 Game roles request",
        description=(f"Player: <@{player_id}>\n"
                     f"Status: {status}\n"
                     f"Updated: {fmt_time(app['updated_at'])}\n\n"
                     f"**Roles:**\n{roles_block}"),
        color=color,
    )
    embed.set_footer(text="Set a priority (1–3) for each role, then press “Approve”.")
    return embed


class _NameButton(discord.ui.Button):
    def __init__(self, label: str, row: int):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary,
                         disabled=True, row=row)


class _PriorityButton(discord.ui.Button):
    def __init__(self, role_key: str, value: int, current: int | None, row: int):
        super().__init__(
            label=str(value),
            style=discord.ButtonStyle.success if current == value else discord.ButtonStyle.secondary,
            row=row,
        )
        self.role_key = role_key
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        view: "ReviewView" = self.view
        await db.set_role_priority(view.player_id, view.guild_id, self.role_key, self.value)
        await view.refresh(interaction)


class _RoleRemoveButton(discord.ui.Button):
    def __init__(self, role_key: str, row: int):
        super().__init__(label="✖", style=discord.ButtonStyle.danger, row=row)
        self.role_key = role_key

    async def callback(self, interaction: discord.Interaction):
        view: "ReviewView" = self.view
        await db.remove_application_role(view.player_id, view.guild_id, self.role_key)
        await view.refresh(interaction)


class _ReviewNav(discord.ui.Button):
    def __init__(self, label: str, delta: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        view: "ReviewView" = self.view
        view.page += self.delta
        await view.refresh(interaction)


class _AddRoleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Add Role", emoji="➕",
                         style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction: discord.Interaction):
        view: "ReviewView" = self.view
        app = await db.get_application(view.player_id, view.guild_id)
        if app is None:
            return await interaction.response.send_message("Request not found.", ephemeral=True)

        chosen = {k for k, _, _ in app["roles"]}
        remaining = [r for r in load_catalog() if r["key"] not in chosen]
        slots = MAX_ROLES - len(chosen)
        if slots <= 0:
            return await interaction.response.send_message(
                f"The player already has the maximum ({MAX_ROLES}) roles.", ephemeral=True)
        if not remaining:
            return await interaction.response.send_message(
                "All available roles are already added.", ephemeral=True)

        review_message = interaction.message
        page = view.page

        async def on_submit(inter: discord.Interaction, selected: list[str]):
            for key in selected:
                await db.add_application_role(view.player_id, view.guild_id, key)
            fresh = await db.get_application(view.player_id, view.guild_id)
            new_view = ReviewView(view.player_id, view.guild_id, fresh, page=page)
            await review_message.edit(embed=build_review_embed(view.player_id, fresh), view=new_view)
            await inter.response.edit_message(content="✅ Roles added.", embed=None, view=None)

        picker = RolePickerView(
            remaining, set(), slots, on_submit,
            title="➕ Add roles for player",
            intro=f"Select roles to add (free slots: {slots}).",
        )
        await interaction.response.send_message(embed=picker.embed(), view=picker, ephemeral=True)


class _ApproveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Approve", emoji="✅",
                         style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction):
        view: "ReviewView" = self.view
        app = await db.get_application(view.player_id, view.guild_id)
        if app is None or not app["roles"]:
            return await interaction.response.send_message(
                "Nothing to approve — the player has no roles.", ephemeral=True)

        await db.set_application_status(view.player_id, view.guild_id, "active")
        fresh = await db.get_application(view.player_id, view.guild_id)
        embed = build_review_embed(view.player_id, fresh)
        embed.set_footer(text=f"Approved by: {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=None)

        # Уведомим игрока в ЛС (не критично, если закрыты).
        member = interaction.guild.get_member(view.player_id)
        if member:
            try:
                await member.send(embed=build_my_roles_embed(member, fresh))
            except discord.HTTPException:
                pass


class ReviewView(discord.ui.View):
    """Интерактивная панель ревью: приоритеты, добавить/убрать роли, утвердить."""

    def __init__(self, player_id: int, guild_id: int, app: dict, page: int = 0):
        super().__init__(timeout=None)
        self.player_id = player_id
        self.guild_id = guild_id
        roles = sorted(app["roles"], key=lambda r: (r[1] is None, r[1] or 0, r[0]))
        self.pages = max(1, math.ceil(len(roles) / REVIEW_PER_PAGE))
        self.page = max(0, min(page, self.pages - 1))
        self._build(roles)

    def _build(self, roles: list[tuple]) -> None:
        idx = catalog_index()
        start = self.page * REVIEW_PER_PAGE
        for i, (key, priority, _src) in enumerate(roles[start:start + REVIEW_PER_PAGE]):
            self.add_item(_NameButton(role_label(key, idx), row=i))
            for value in (1, 2, 3):
                self.add_item(_PriorityButton(key, value, priority, row=i))
            self.add_item(_RoleRemoveButton(key, row=i))
        if self.pages > 1:
            self.add_item(_ReviewNav("◀", -1))
            self.add_item(_ReviewNav("▶", +1))
        self.add_item(_AddRoleButton())
        self.add_item(_ApproveButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if member_is_admin(interaction.user):
            return True
        await interaction.response.send_message(
            "This panel is for administrators only.", ephemeral=True)
        return False

    async def refresh(self, interaction: discord.Interaction) -> None:
        app = await db.get_application(self.player_id, self.guild_id)
        if app is None:
            return await interaction.response.edit_message(
                content="This request no longer exists.", embed=None, view=None)
        new_view = ReviewView(self.player_id, self.guild_id, app, page=self.page)
        await interaction.response.edit_message(
            embed=build_review_embed(self.player_id, app), view=new_view)


# --- Общий список игроков и их ролей (/roster) ------------------------------

def build_roster_embed(entries: list[dict], page: int, pages: int) -> discord.Embed:
    idx = catalog_index()
    start = page * ROSTER_PER_PAGE
    chunk = entries[start:start + ROSTER_PER_PAGE]

    blocks = []
    for entry in chunk:
        mark = "✅" if entry["status"] == "active" else "⏳"
        roles = sorted(entry["roles"], key=lambda r: (r[1] is None, r[1] or 0, r[0]))
        parts = []
        for key, priority, _src in roles:
            prio = f"`P{priority}`" if priority else "`—`"
            parts.append(f"{role_label(key, idx)} {prio}")
        blocks.append(f"{mark} <@{entry['player_id']}>\n{' · '.join(parts)}")

    embed = discord.Embed(
        title="🎮 Players & roles",
        description="\n\n".join(blocks) if blocks else "_empty_",
        color=COLOR,
    )
    embed.set_footer(
        text=f"Page {page + 1}/{pages} · players total: {len(entries)} · "
             "✅ approved  ⏳ pending  ·  P# = priority"
    )
    return embed


class RosterView(discord.ui.View):
    def __init__(self, entries: list[dict], author_id: int):
        super().__init__(timeout=180)
        self.entries = entries
        self.author_id = author_id
        self.page = 0
        self.max_page = max(0, (len(entries) - 1) // ROSTER_PER_PAGE)
        self._sync()

    def _sync(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_page

    def embed(self) -> discord.Embed:
        return build_roster_embed(self.entries, self.page, self.max_page + 1)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Only the command author can flip pages.", ephemeral=True)
        return False

    async def _update(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.max_page:
            self.page += 1
        await self._update(interaction)


# --- Сам ког ----------------------------------------------------------------

class GameRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def post_review(self, guild: discord.Guild, player: discord.abc.User) -> None:
        """Отправляет заявку игрока в админ-канал с интерактивной панелью."""
        if not config.ADMIN_CHANNEL_ID:
            log.warning("ADMIN_CHANNEL_ID не задан — заявка %s не отправлена админам.", player.id)
            return
        channel = guild.get_channel(config.ADMIN_CHANNEL_ID)
        if channel is None:
            log.warning("ADMIN_CHANNEL_ID=%s не найден на сервере.", config.ADMIN_CHANNEL_ID)
            return
        app = await db.get_application(player.id, guild.id)
        if app is None:
            return
        view = ReviewView(player.id, guild.id, app)
        await channel.send(
            content=f"📥 New request from {player.mention}",
            embed=build_review_embed(player.id, app),
            view=view,
        )

    @commands.hybrid_command(
        name="setup-game-panel", description="[Admin] Place the role-selection panel in this channel")
    @commands.guild_only()
    @admin_only()
    async def setup_game_panel(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎮 Game Roles",
            description=("Pick your game roles (up to "
                         f"{MAX_ROLES}) — press “Choose Roles”.\n"
                         "View your roles and priorities with “My Roles”."),
            color=COLOR,
        )
        await ctx.channel.send(embed=embed, view=PlayerPanelView())
        await ctx.reply("Panel installed.", ephemeral=True, mention_author=False)

    @commands.hybrid_command(
        name="review", description="[Admin] Open a player's role panel (edit / assign roles)")
    @app_commands.describe(member="Which player to edit")
    @commands.guild_only()
    @admin_only()
    async def review(self, ctx: commands.Context, member: discord.Member):
        if member.bot:
            return await ctx.reply("Bots can't have game roles.",
                                   ephemeral=True, mention_author=False)
        # Создаём пустую заявку, если игрок сам ничего не подавал — чтобы можно
        # было назначить роли с нуля кнопкой «Add Role».
        await db.ensure_application(member.id, ctx.guild.id)
        app = await db.get_application(member.id, ctx.guild.id)
        view = ReviewView(member.id, ctx.guild.id, app)
        note = "" if app["roles"] else "  ·  empty, use ➕ Add Role to assign"
        await ctx.send(
            content=f"Editing roles for **{member.display_name}**{note}",
            embed=build_review_embed(member.id, app), view=view)

    @commands.hybrid_command(
        name="roster", description="[Admin] List all players and their assigned roles")
    @commands.guild_only()
    @admin_only()
    async def roster(self, ctx: commands.Context):
        entries = await db.get_all_applications(ctx.guild.id)
        if not entries:
            return await ctx.reply(
                "No players have selected roles yet.",
                ephemeral=True, mention_author=False)
        view = RosterView(entries, ctx.author.id)
        msg_view = view if view.max_page > 0 else None
        await ctx.reply(embed=view.embed(), view=msg_view,
                        ephemeral=True, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(GameRoles(bot))
