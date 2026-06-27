"""Ког сборки состава (compositions).

Поток:
1. Админ задаёт набор ролей в sets/<имя>.json (тиры: первые 20, потом +5, +5, ...).
2. Игроки ставят реакции на сообщение-сбор; админ запускает /compose <набор> <ссылка|id>.
3. Бот парсит отметившихся, распределяет их по ролям набора (по приоритетам 1→2→3,
   максимальное паросочетание, дефицитные игроки вперёд), сохраняет результат в БД.
4. Админ интерактивной панелью правит состав, затем /publish — публикует игрокам.

Имена ролей берутся из roles.json (тот же каталог, что выбирают игроки).
"""
import json
import logging
import os
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
from cogs.economy import admin_only, fmt, member_is_admin
from cogs.gameroles import catalog_index, load_catalog, role_label

log = logging.getLogger("dcbot.composition")
COLOR = config.EMBED_COLOR
SETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sets")
SLOTS_PER_PAGE = 25      # лимит опций в Select Discord


# --- Загрузка наборов -------------------------------------------------------

def norm_set_name(name: str) -> str:
    """Имя набора без хвоста '.json'."""
    name = name.strip()
    return name[:-5] if name.lower().endswith(".json") else name


def list_sets() -> list[str]:
    if not os.path.isdir(SETS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SETS_DIR) if f.endswith(".json"))


def _name_to_key() -> dict[str, str]:
    """Карта для распознавания роли по КЛЮЧУ или ИМЕНИ (без учёта регистра) → ключ."""
    mapping: dict[str, str] = {}
    for role in load_catalog():
        mapping[role["key"].lower()] = role["key"]
        mapping[role["name"].lower()] = role["key"]
    return mapping


def load_set(name: str) -> dict | None:
    """Читает sets/<name>.json → {'name','tiers':[[role_key,...],...],'unknown':[нераспознанные]}.

    В файле можно писать как ключ ('bms'), так и имя роли ('BMS') — приводим к ключу.
    Имя набора принимается и с хвостом '.json', и без него.
    """
    name = norm_set_name(name)
    path = os.path.join(SETS_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log.warning("Набор %s не прочитан: %s", name, e)
        return None

    raw_tiers = data.get("tiers") or []
    # Допускаем и плоский "roles": режем на 20 + по 5.
    if not raw_tiers and data.get("roles"):
        flat = list(data["roles"])
        raw_tiers = [flat[:20]] + [flat[i:i + 5] for i in range(20, len(flat), 5)]

    resolver = _name_to_key()
    tiers: list[list[str]] = []
    unknown: list[str] = []
    for tier in raw_tiers:
        if not tier:
            continue
        resolved = []
        for token in tier:
            key = resolver.get(str(token).strip().lower())
            if key:
                resolved.append(key)
            else:
                unknown.append(str(token))
        tiers.append(resolved)
    return {"name": name, "tiers": tiers, "unknown": sorted(set(unknown))}


def flat_roles(tiers: list[list[str]]) -> list[str]:
    return [r for tier in tiers for r in tier]


def tier_bounds(tiers: list[list[str]]) -> list[tuple[int, int, int]]:
    """[(tier_no, start_index, end_index_exclusive)] для группировки слотов по тирам."""
    bounds = []
    start = 0
    for i, tier in enumerate(tiers):
        bounds.append((i, start, start + len(tier)))
        start += len(tier)
    return bounds


# --- Чистый алгоритм распределения ------------------------------------------

def compose_roster(
    tiers: list[list[str]],
    players: dict[int, dict[str, int | None]],
    rng: random.Random | None = None,
) -> tuple[list[int | None], list[int]]:
    """Распределяет игроков по слотам набора.

    players: player_id -> {role_key: priority(1..3 или None)}.
    Возвращает (assignment, leftovers): assignment[i] = player_id|None для слота i;
    leftovers = id игроков, которым места не нашлось.

    Логика: тир за тиром; внутри тира приоритеты 1→2→3→(не задан). На каждом уровне —
    максимальное паросочетание (чтобы занять максимум слотов), кандидаты обходятся в
    порядке возрастания числа доступных ему ролей набора (дефицитные вперёд), далее случайно.
    Один игрок занимает не более одной роли.
    """
    rng = rng or random
    flat = flat_roles(tiers)
    n = len(flat)
    set_roles = set(flat)
    scarcity = {p: len(set(roles.keys()) & set_roles) for p, roles in players.items()}

    assignment: list[int | None] = [None] * n
    assigned: set[int] = set()

    base = 0
    for tier in tiers:
        tier_idx = list(range(base, base + len(tier)))
        base += len(tier)
        for prio in (1, 2, 3, None):
            free = [s for s in tier_idx if assignment[s] is None]
            if not free:
                continue
            # adjacency: игрок -> слоты, где у него есть роль слота с этим приоритетом
            adj: dict[int, list[int]] = {}
            for p, roles in players.items():
                if p in assigned:
                    continue
                elig = [s for s in free if flat[s] in roles and roles[flat[s]] == prio]
                if elig:
                    adj[p] = elig
            if not adj:
                continue

            # Дефицитность слота: сколько кандидатов на него претендует. Игрок в первую
            # очередь занимает самый дефицитный слот → не оставляем редкие роли пустыми.
            slot_cands: dict[int, int] = {}
            for slots_of_p in adj.values():
                for s in slots_of_p:
                    slot_cands[s] = slot_cands.get(s, 0) + 1
            for p in adj:
                adj[p].sort(key=lambda s: (slot_cands[s], rng.random()))

            match_slot: dict[int, int] = {}  # slot -> player

            def augment(p: int, seen: set[int]) -> bool:
                for s in adj[p]:
                    if s in seen:
                        continue
                    seen.add(s)
                    if s not in match_slot or augment(match_slot[s], seen):
                        match_slot[s] = p
                        return True
                return False

            order = sorted(adj.keys(), key=lambda p: (scarcity[p], rng.random()))
            for p in order:
                augment(p, set())

            for s, p in match_slot.items():
                assignment[s] = p
                assigned.add(p)

    leftovers = [p for p in players if p not in assigned]
    return assignment, leftovers


# --- Парсинг реакций --------------------------------------------------------

def parse_message_ref(channel_id_default: int, ref: str) -> tuple[int, int] | None:
    """Из ссылки на сообщение или из id → (channel_id, message_id)."""
    ref = ref.strip()
    if "/" in ref:
        parts = ref.rstrip("/").split("/")
        try:
            return int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            return None
    if ref.isdigit():
        return channel_id_default, int(ref)
    return None


async def collect_reactors(message: discord.Message) -> list[int]:
    """Все НЕ-боты, поставившие любую реакцию на сообщение."""
    users: set[int] = set()
    for reaction in message.reactions:
        async for u in reaction.users():
            if not u.bot:
                users.add(u.id)
    return list(users)


async def players_from_pool(guild_id: int, pool: list[int]) -> dict[int, dict[str, int | None]]:
    """Для каждого отметившегося собирает его роли+приоритеты (пустой dict, если ролей нет)."""
    players: dict[int, dict[str, int | None]] = {}
    for pid in pool:
        app = await db.get_application(pid, guild_id)
        roles = {k: prio for k, prio, _src in app["roles"]} if app else {}
        players[pid] = roles
    return players


# --- Отрисовка состава ------------------------------------------------------

def _member_name(guild: discord.Guild, pid: int) -> str:
    m = guild.get_member(pid)
    return m.display_name if m else f"id:{pid}"


def build_result_embed(guild: discord.Guild, set_name: str, tiers, comp: dict,
                       title: str | None = None, show_unassigned: bool = True) -> discord.Embed:
    idx = catalog_index()
    slots = comp["slots"]
    by_index = {i: (role, pid) for i, role, pid in slots}
    assigned_ids = {pid for _, _, pid in slots if pid}

    lines = []
    for tno, start, end in tier_bounds(tiers):
        filled = sum(1 for i in range(start, end) if by_index.get(i, (None, None))[1])
        lines.append(f"\n__**Roles {start + 1}–{end}**__  ({filled}/{end - start})")
        for i in range(start, end):
            role, pid = by_index.get(i, (None, None))
            who = f"<@{pid}>" if pid else "*— empty —*"
            lines.append(f"`{i + 1:>2}` {role_label(role, idx)} → {who}")

    leftovers = [p for p in comp["pool"] if p not in assigned_ids]
    embed = discord.Embed(
        title=title or f"🧩 Composition “{set_name}”",
        description="\n".join(lines) if lines else "*empty set*",
        color=COLOR,
    )
    if leftovers and show_unassigned:
        more = f" (+{len(leftovers) - 20})" if len(leftovers) > 20 else ""
        embed.add_field(
            name=f"🪑 Unassigned ({len(leftovers)})",
            value=", ".join(f"<@{p}>" for p in leftovers[:20]) + more,
            inline=False)
    total = len(slots)
    filled_total = len(assigned_ids)
    embed.set_footer(text=f"Filled {filled_total}/{total} · checked in: {len(comp['pool'])}")
    return embed


async def build_leftovers_detail(guild: discord.Guild, guild_id: int,
                                 comp: dict) -> str:
    """Список нераспределённых с их ролями — для админа."""
    idx = catalog_index()
    assigned_ids = {pid for _, _, pid in comp["slots"] if pid}
    leftovers = [p for p in comp["pool"] if p not in assigned_ids]
    if not leftovers:
        return ""
    out = []
    for pid in leftovers:
        app = await db.get_application(pid, guild_id)
        roles = app["roles"] if app else []
        if roles:
            rs = ", ".join(
                f"{role_label(k, idx)}"
                + (f" `P{prio}`" if prio else " `—`")
                for k, prio, _ in sorted(roles, key=lambda r: (r[1] is None, r[1] or 0, r[0])))
        else:
            rs = "*no roles selected*"
        out.append(f"<@{pid}> — {rs}")
    return "\n".join(out)


# --- Сборка результата (pool → algo → save → render) ------------------------

async def run_compose(guild: discord.Guild, set_name: str,
                      message: discord.Message) -> tuple[discord.Embed, "CompositionView"]:
    sdef = load_set(set_name)
    pool = await collect_reactors(message)
    players = await players_from_pool(guild.id, pool)
    assignment, _leftovers = compose_roster(sdef["tiers"], players, random)
    flat = flat_roles(sdef["tiers"])
    slots = [(i, flat[i], assignment[i]) for i in range(len(flat))]
    await db.save_composition(guild.id, set_name, message.id, slots, list(players.keys()))
    return await render_panel(guild, set_name)


async def render_panel(guild: discord.Guild, set_name: str,
                       current_tier: int = 0, selected_slot: int | None = None):
    comp = await db.get_composition(guild.id, set_name)
    if comp is None:
        return None, None
    sdef = load_set(set_name)
    tiers = sdef["tiers"] if sdef and sdef["tiers"] else [[r for _, r, _ in comp["slots"]]]
    embed = build_result_embed(guild, set_name, tiers, comp, show_unassigned=False)
    detail = await build_leftovers_detail(guild, guild.id, comp)
    if detail:
        embed.add_field(name="🪑 Unassigned — their roles", value=detail[:1024], inline=False)
    view = CompositionView(guild, set_name, tiers, comp, current_tier, selected_slot)
    return embed, view


async def _refresh(interaction: discord.Interaction, set_name: str,
                   current_tier: int, selected_slot: int | None) -> None:
    embed, view = await render_panel(interaction.guild, set_name, current_tier, selected_slot)
    if embed is None:
        return await interaction.response.edit_message(
            content="This composition no longer exists.", embed=None, view=None)
    await interaction.response.edit_message(content=None, embed=embed, view=view)


# --- Интерактивная панель редактирования ------------------------------------

class _TierNav(discord.ui.Button):
    def __init__(self, label: str, delta: int, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        v: "CompositionView" = self.view
        new_tier = (v.current_tier + self.delta) % len(v.tiers)
        await _refresh(interaction, v.set_name, new_tier, None)


class _SlotSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], row: int):
        super().__init__(placeholder="Pick a slot to edit…", options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        v: "CompositionView" = self.view
        await _refresh(interaction, v.set_name, v.current_tier, int(self.values[0]))


class _PlayerSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], row: int):
        super().__init__(placeholder="Assign player (or clear)…", options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        v: "CompositionView" = self.view
        slot = v.selected_slot
        val = self.values[0]
        if val == "__clear__":
            await db.set_composition_slot(v.guild_id, v.set_name, slot, None)
        else:
            pid = int(val)
            await db.set_composition_slot(v.guild_id, v.set_name, slot, pid)
        await _refresh(interaction, v.set_name, v.current_tier, v.selected_slot)


class _PublishButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Publish", emoji="📣",
                         style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        v: "CompositionView" = self.view
        comp = await db.get_composition(v.guild_id, v.set_name)
        embed = build_result_embed(interaction.guild, v.set_name, v.tiers, comp,
                                   title=f"🧩 Roster — {v.set_name}", show_unassigned=True)
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("Published to this channel ✅", ephemeral=True)


class CompositionView(discord.ui.View):
    def __init__(self, guild: discord.Guild, set_name: str, tiers, comp: dict,
                 current_tier: int, selected_slot: int | None):
        super().__init__(timeout=600)
        self.guild_id = guild.id
        self.set_name = set_name
        self.tiers = tiers
        self.current_tier = max(0, min(current_tier, len(tiers) - 1))
        self.selected_slot = selected_slot
        self._build(guild, comp)

    def _build(self, guild: discord.Guild, comp: dict) -> None:
        _tno, start, end = tier_bounds(self.tiers)[self.current_tier]
        by_index = {i: (role, pid) for i, role, pid in comp["slots"]}
        assigned_ids = {pid for _, _, pid in comp["slots"] if pid}

        if len(self.tiers) > 1:
            self.add_item(_TierNav("◀ prev", -1, row=0))
            self.add_item(_TierNav("next ▶", +1, row=0))

        slot_opts = []
        for i in range(start, end):
            role, pid = by_index.get(i, (None, None))
            who = _member_name(guild, pid) if pid else "— empty —"
            slot_opts.append(discord.SelectOption(
                label=f"#{i + 1} {role}"[:100], value=str(i),
                description=who[:100], default=(self.selected_slot == i)))
        self.add_item(_SlotSelect(slot_opts[:SLOTS_PER_PAGE], row=1))

        if self.selected_slot is not None:
            unassigned = [p for p in comp["pool"] if p not in assigned_ids]
            popts = [discord.SelectOption(label="— Clear slot —", value="__clear__", emoji="🧹")]
            for pid in unassigned[:24]:
                popts.append(discord.SelectOption(label=_member_name(guild, pid)[:100],
                                                  value=str(pid)))
            self.add_item(_PlayerSelect(popts, row=2))

        self.add_item(_PublishButton(row=3))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if member_is_admin(interaction.user):
            return True
        await interaction.response.send_message(
            "This panel is for administrators only.", ephemeral=True)
        return False


class _ConfirmRebuild(discord.ui.View):
    def __init__(self, set_name: str, message: discord.Message):
        super().__init__(timeout=60)
        self.set_name = set_name
        self.message = message

    @discord.ui.button(label="Rebuild from scratch", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Rebuilding…", view=None)
        embed, view = await run_compose(interaction.guild, self.set_name, self.message)
        await interaction.channel.send(embed=embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


# --- Ког --------------------------------------------------------------------

class GameComposition(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="sets", description="[Admin] List available role sets")
    @commands.guild_only()
    @admin_only()
    async def sets(self, ctx: commands.Context):
        files = list_sets()
        saved = await db.list_compositions(ctx.guild.id)
        if not files:
            return await ctx.reply("No sets defined. Add a `sets/<name>.json` file.",
                                   ephemeral=True, mention_author=False)
        lines = []
        for name in files:
            sdef = load_set(name)
            size = len(flat_roles(sdef["tiers"])) if sdef else 0
            mark = " · 💾 has composition" if name in saved else ""
            warn = f" · ⚠️ unknown roles: {', '.join(sdef['unknown'])}" if sdef and sdef["unknown"] else ""
            lines.append(f"**{name}** — {size} slots{mark}{warn}")
        embed = discord.Embed(title="🧩 Role sets", description="\n".join(lines), color=COLOR)
        await ctx.reply(embed=embed, ephemeral=True, mention_author=False)

    @commands.hybrid_command(
        name="compose", description="[Admin] Build a composition from a message's reactions")
    @app_commands.describe(set_name="Set name (sets/<name>.json)",
                           message="Message link or ID with the check-in reactions")
    @commands.guild_only()
    @admin_only()
    async def compose(self, ctx: commands.Context, set_name: str, message: str):
        set_name = norm_set_name(set_name)
        sdef = load_set(set_name)
        if not sdef or not sdef["tiers"]:
            return await ctx.reply(f"Set '{set_name}' not found or empty.",
                                   ephemeral=True, mention_author=False)
        ref = parse_message_ref(ctx.channel.id, message)
        if ref is None:
            return await ctx.reply("Couldn't parse the message link/ID.",
                                   ephemeral=True, mention_author=False)
        cid, mid = ref
        channel = ctx.guild.get_channel(cid) or ctx.channel
        try:
            msg = await channel.fetch_message(mid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await ctx.reply("Message not found (check the link and my access).",
                                   ephemeral=True, mention_author=False)

        if await db.get_composition(ctx.guild.id, set_name):
            return await ctx.reply(
                f"Composition '{set_name}' already exists. Rebuild from scratch (manual edits will be lost)?",
                view=_ConfirmRebuild(set_name, msg), ephemeral=True, mention_author=False)

        embed, view = await run_compose(ctx.guild, set_name, msg)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(
        name="composition", description="[Admin] Reopen the edit panel of a saved composition")
    @app_commands.describe(set_name="Set name")
    @commands.guild_only()
    @admin_only()
    async def composition(self, ctx: commands.Context, set_name: str):
        set_name = norm_set_name(set_name)
        embed, view = await render_panel(ctx.guild, set_name)
        if embed is None:
            return await ctx.reply(f"No saved composition for '{set_name}'. Run /compose first.",
                                   ephemeral=True, mention_author=False)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(
        name="publish", description="[Admin] Publish a composition for players to see")
    @app_commands.describe(set_name="Set name", channel="Where to publish (default: here)")
    @commands.guild_only()
    @admin_only()
    async def publish(self, ctx: commands.Context, set_name: str,
                      channel: discord.TextChannel | None = None):
        set_name = norm_set_name(set_name)
        comp = await db.get_composition(ctx.guild.id, set_name)
        if comp is None:
            return await ctx.reply(f"No saved composition for '{set_name}'.",
                                   ephemeral=True, mention_author=False)
        sdef = load_set(set_name)
        tiers = sdef["tiers"] if sdef and sdef["tiers"] else [[r for _, r, _ in comp["slots"]]]
        target = channel or ctx.channel
        embed = build_result_embed(ctx.guild, set_name, tiers, comp,
                                   title=f"🧩 Roster — {set_name}", show_unassigned=True)
        await target.send(embed=embed)
        await ctx.reply(f"Published to {target.mention} ✅", ephemeral=True, mention_author=False)

    @commands.hybrid_command(
        name="vc-check",
        description="[Admin] Who (assigned + checked-in but unassigned) is missing from your voice channel")
    @app_commands.describe(set_name="Composition set name")
    @commands.guild_only()
    @admin_only()
    async def vc_check(self, ctx: commands.Context, set_name: str):
        set_name = norm_set_name(set_name)
        voice = getattr(ctx.author, "voice", None)
        if voice is None or voice.channel is None:
            return await ctx.reply("You need to be in a voice channel to use this.",
                                   ephemeral=True, mention_author=False)
        vc = voice.channel

        comp = await db.get_composition(ctx.guild.id, set_name)
        if comp is None:
            return await ctx.reply(f"No saved composition for '{set_name}'.",
                                   ephemeral=True, mention_author=False)

        in_vc = {m.id for m in vc.members}
        # назначенные в составе игроки (по слотам), без повторов, в порядке слотов
        assigned: list[int] = []
        for _i, _role, pid in comp["slots"]:
            if pid and pid not in assigned:
                assigned.append(pid)
        assigned_set = set(assigned)
        # отметившиеся реакцией, но пока НЕ назначенные ни на одну роль
        unassigned = [pid for pid in comp["pool"] if pid not in assigned_set]

        if not assigned and not unassigned:
            return await ctx.reply(f"Composition '{set_name}' has no players yet.",
                                   ephemeral=True, mention_author=False)

        assigned_missing = [pid for pid in assigned if pid not in in_vc]
        unassigned_missing = [pid for pid in unassigned if pid not in in_vc]

        total = len(assigned) + len(unassigned)
        if not assigned_missing and not unassigned_missing:
            return await ctx.reply(
                f"✅ All {total} players (assigned + unassigned) are in **{vc.name}**.",
                mention_author=False)

        parts = [f"🔇 Missing from **{vc.name}**:"]
        if assigned_missing:
            mentions = " ".join(f"<@{pid}>" for pid in assigned_missing)
            parts.append(f"\n**Assigned ({len(assigned_missing)}/{len(assigned)}):**\n{mentions}")
        if unassigned_missing:
            mentions = " ".join(f"<@{pid}>" for pid in unassigned_missing)
            parts.append(
                f"\n**Checked in, no role yet ({len(unassigned_missing)}/{len(unassigned)}):**\n{mentions}")
        await ctx.reply(content="\n".join(parts), mention_author=False)

    @commands.hybrid_command(
        name="split",
        description="[Admin] Evenly split silver among a composition's assigned players")
    @app_commands.describe(set_name="Composition set name",
                           amount="Total silver to split (floored per player)")
    @commands.guild_only()
    @admin_only()
    async def split(self, ctx: commands.Context, set_name: str, amount: int):
        set_name = norm_set_name(set_name)
        if amount <= 0:
            return await ctx.reply("Amount must be greater than zero.",
                                   ephemeral=True, mention_author=False)
        comp = await db.get_composition(ctx.guild.id, set_name)
        if comp is None:
            return await ctx.reply(f"No saved composition for '{set_name}'.",
                                   ephemeral=True, mention_author=False)

        # Получатели — только игроки, назначенные на роль (без повторов, в порядке слотов).
        recipients: list[int] = []
        for _i, _role, pid in comp["slots"]:
            if pid and pid not in recipients:
                recipients.append(pid)
        if not recipients:
            return await ctx.reply(
                f"Composition '{set_name}' has no assigned players yet.",
                ephemeral=True, mention_author=False)

        share = amount // len(recipients)
        if share <= 0:
            return await ctx.reply(
                f"Amount {amount} is too small to split among {len(recipients)} players "
                "(each would get less than 1).",
                ephemeral=True, mention_author=False)

        for pid in recipients:
            await db.change_balance(pid, share)

        distributed = share * len(recipients)
        leftover = amount - distributed
        lines = [
            f"Each of **{len(recipients)}** assigned players received {fmt(share)}.",
            f"Distributed: {fmt(distributed)} of {fmt(amount)}.",
        ]
        if leftover:
            lines.append(f"Remainder kept (not distributed): {fmt(leftover)}.")
        embed = discord.Embed(
            title=f"💰 Silver split — {set_name}",
            description="\n".join(lines),
            color=0x57F287,
        )
        embed.add_field(
            name="Recipients",
            value=", ".join(f"<@{pid}>" for pid in recipients)[:1024],
            inline=False)
        embed.set_footer(text=f"By: {ctx.author.display_name}")
        await ctx.reply(embed=embed, mention_author=False)

        # Уведомление в канал экономики с реальным пингом получателей
        # (упоминания внутри embed не пингуют, поэтому отдельным сообщением).
        if config.ECONOMY_CHANNEL_ID:
            econ_ch = ctx.guild.get_channel(config.ECONOMY_CHANNEL_ID)
            if econ_ch is not None:
                mentions = " ".join(f"<@{pid}>" for pid in recipients)
                await econ_ch.send(
                    f"## 💰 Награда за состав «{set_name}»\n"
                    f"Каждому из **{len(recipients)}** участников начислено "
                    f"{fmt(share)}. Поздравляем! 🎉\n"
                    f"-# Получатели:\n{mentions}",
                    allowed_mentions=discord.AllowedMentions(users=True))


async def setup(bot: commands.Bot):
    await bot.add_cog(GameComposition(bot))
