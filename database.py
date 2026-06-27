"""Слой работы с базой данных (SQLite через aiosqlite).

Таблица balances:
    user_id  INTEGER PRIMARY KEY  -- Discord ID пользователя
    balance  INTEGER NOT NULL     -- баланс в "серебре"

Таблицы игровых ролей:
    applications       -- заявка игрока (status: pending | active)
    application_roles  -- роли в заявке: игрок ↔ роль ↔ приоритет ↔ кто добавил
"""
from datetime import datetime, timezone

import aiosqlite

import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                player_id  INTEGER NOT NULL,
                guild_id   INTEGER NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'pending',
                updated_at TEXT    NOT NULL,
                PRIMARY KEY (player_id, guild_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS application_roles (
                player_id INTEGER NOT NULL,
                guild_id  INTEGER NOT NULL,
                role_key  TEXT    NOT NULL,
                priority  INTEGER,                       -- 1..3 или NULL (не задан)
                source    TEXT    NOT NULL DEFAULT 'player',  -- 'player' | 'admin'
                PRIMARY KEY (player_id, guild_id, role_key)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS compositions (
                guild_id          INTEGER NOT NULL,
                set_name          TEXT    NOT NULL,
                source_message_id INTEGER,
                created_at        TEXT    NOT NULL,
                PRIMARY KEY (guild_id, set_name)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS composition_slots (
                guild_id   INTEGER NOT NULL,
                set_name   TEXT    NOT NULL,
                slot_index INTEGER NOT NULL,
                role_key   TEXT    NOT NULL,
                player_id  INTEGER,                       -- кто назначен или NULL
                PRIMARY KEY (guild_id, set_name, slot_index)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS composition_pool (
                guild_id  INTEGER NOT NULL,
                set_name  TEXT    NOT NULL,
                player_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, set_name, player_id)
            )
            """
        )
        await db.commit()


async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM balances WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def change_balance(user_id: int, delta: int, allow_negative: bool = False) -> int:
    """Меняет баланс на delta (может быть отрицательным). Возвращает новый баланс.

    Если allow_negative=False, баланс не опускается ниже нуля.
    """
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM balances WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        current = row[0] if row else 0
        new_balance = current + delta
        if not allow_negative and new_balance < 0:
            new_balance = 0
        await db.execute(
            """
            INSERT INTO balances (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance
            """,
            (user_id, new_balance),
        )
        await db.commit()
        return new_balance


async def transfer(from_id: int, to_id: int, amount: int) -> tuple[bool, int]:
    """Атомарно переводит amount от from_id к to_id.

    Возвращает (успех, баланс_отправителя_после). При нехватке средств — (False, текущий_баланс).
    """
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("BEGIN")
        async with db.execute(
            "SELECT balance FROM balances WHERE user_id = ?", (from_id,)
        ) as cur:
            row = await cur.fetchone()
        sender = row[0] if row else 0

        if sender < amount:
            await db.rollback()
            return False, sender

        await db.execute(
            "UPDATE balances SET balance = balance - ? WHERE user_id = ?",
            (amount, from_id),
        )
        await db.execute(
            """
            INSERT INTO balances (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
            """,
            (to_id, amount),
        )
        await db.commit()
        return True, sender - amount


async def get_leaderboard() -> list[tuple[int, int]]:
    """Все участники с балансом > 0, отсортированы от большего к меньшему."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, balance FROM balances WHERE balance > 0 "
            "ORDER BY balance DESC, user_id ASC"
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]


# --- Игровые роли / заявки --------------------------------------------------

async def submit_application(player_id: int, guild_id: int, role_keys: list[str]) -> None:
    """Создаёт/обновляет заявку игрока: статус 'pending', роли заменяются на новые.

    Приоритеты сбрасываются (NULL), источник всех ролей — 'player'.
    """
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("BEGIN")
        await db.execute(
            """
            INSERT INTO applications (player_id, guild_id, status, updated_at)
            VALUES (?, ?, 'pending', ?)
            ON CONFLICT(player_id, guild_id)
            DO UPDATE SET status = 'pending', updated_at = excluded.updated_at
            """,
            (player_id, guild_id, _now()),
        )
        await db.execute(
            "DELETE FROM application_roles WHERE player_id = ? AND guild_id = ?",
            (player_id, guild_id),
        )
        for key in role_keys:
            await db.execute(
                "INSERT INTO application_roles (player_id, guild_id, role_key, priority, source) "
                "VALUES (?, ?, ?, NULL, 'player')",
                (player_id, guild_id, key),
            )
        await db.commit()


async def get_application(player_id: int, guild_id: int) -> dict | None:
    """Возвращает {'status', 'updated_at', 'roles': [(role_key, priority, source)]} или None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT status, updated_at FROM applications WHERE player_id = ? AND guild_id = ?",
            (player_id, guild_id),
        ) as cur:
            head = await cur.fetchone()
        if head is None:
            return None
        async with db.execute(
            "SELECT role_key, priority, source FROM application_roles "
            "WHERE player_id = ? AND guild_id = ? ORDER BY role_key",
            (player_id, guild_id),
        ) as cur:
            roles = [(r[0], r[1], r[2]) for r in await cur.fetchall()]
        return {"status": head[0], "updated_at": head[1], "roles": roles}


async def get_all_applications(guild_id: int) -> list[dict]:
    """Все заявки сервера: [{'player_id','status','updated_at','roles':[(key,priority,source)]}].

    Игроки без ролей исключаются. Отсортировано по времени обновления (свежие сверху).
    """
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT player_id, status, updated_at FROM applications "
            "WHERE guild_id = ? ORDER BY updated_at DESC",
            (guild_id,),
        ) as cur:
            apps = await cur.fetchall()
        async with db.execute(
            "SELECT player_id, role_key, priority, source FROM application_roles "
            "WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            role_rows = await cur.fetchall()

    by_player: dict[int, list[tuple]] = {}
    for pid, key, priority, source in role_rows:
        by_player.setdefault(pid, []).append((key, priority, source))

    result = []
    for pid, status, updated_at in apps:
        roles = by_player.get(pid)
        if not roles:
            continue
        result.append({"player_id": pid, "status": status,
                       "updated_at": updated_at, "roles": roles})
    return result


async def set_role_priority(player_id: int, guild_id: int, role_key: str, priority: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE application_roles SET priority = ? "
            "WHERE player_id = ? AND guild_id = ? AND role_key = ?",
            (priority, player_id, guild_id, role_key),
        )
        await db.commit()


async def ensure_application(player_id: int, guild_id: int) -> None:
    """Создаёт пустую заявку (status 'pending'), если её ещё нет. Существующую НЕ трогает.

    Нужна, чтобы админ мог открыть панель и назначить роли игроку, который сам
    заявку не подавал.
    """
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO applications (player_id, guild_id, status, updated_at) "
            "VALUES (?, ?, 'pending', ?)",
            (player_id, guild_id, _now()),
        )
        await db.commit()


async def add_application_role(
    player_id: int, guild_id: int, role_key: str, source: str = "admin"
) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO application_roles (player_id, guild_id, role_key, priority, source) "
            "VALUES (?, ?, ?, NULL, ?)",
            (player_id, guild_id, role_key, source),
        )
        await db.commit()


async def remove_application_role(player_id: int, guild_id: int, role_key: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM application_roles WHERE player_id = ? AND guild_id = ? AND role_key = ?",
            (player_id, guild_id, role_key),
        )
        await db.commit()


async def set_application_status(player_id: int, guild_id: int, status: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE applications SET status = ?, updated_at = ? WHERE player_id = ? AND guild_id = ?",
            (status, _now(), player_id, guild_id),
        )
        await db.commit()


# --- Составы (compositions) -------------------------------------------------

async def save_composition(
    guild_id: int, set_name: str, source_message_id: int | None,
    slots: list[tuple[int, str, int | None]], pool: list[int],
) -> None:
    """Сохраняет результат сборки состава, ПОЛНОСТЬЮ заменяя предыдущий.

    slots: [(slot_index, role_key, player_id|None)]; pool: все отметившиеся игроки.
    """
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("BEGIN")
        await db.execute(
            "DELETE FROM composition_slots WHERE guild_id = ? AND set_name = ?",
            (guild_id, set_name))
        await db.execute(
            "DELETE FROM composition_pool WHERE guild_id = ? AND set_name = ?",
            (guild_id, set_name))
        await db.execute(
            "INSERT INTO compositions (guild_id, set_name, source_message_id, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, set_name) DO UPDATE SET "
            "source_message_id = excluded.source_message_id, created_at = excluded.created_at",
            (guild_id, set_name, source_message_id, _now()))
        for slot_index, role_key, player_id in slots:
            await db.execute(
                "INSERT INTO composition_slots (guild_id, set_name, slot_index, role_key, player_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, set_name, slot_index, role_key, player_id))
        for player_id in pool:
            await db.execute(
                "INSERT OR IGNORE INTO composition_pool (guild_id, set_name, player_id) "
                "VALUES (?, ?, ?)",
                (guild_id, set_name, player_id))
        await db.commit()


async def get_composition(guild_id: int, set_name: str) -> dict | None:
    """Возвращает {'set_name','source_message_id','created_at','slots':[(idx,role,player)],'pool':[ids]} или None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT source_message_id, created_at FROM compositions "
            "WHERE guild_id = ? AND set_name = ?", (guild_id, set_name)) as cur:
            head = await cur.fetchone()
        if head is None:
            return None
        async with db.execute(
            "SELECT slot_index, role_key, player_id FROM composition_slots "
            "WHERE guild_id = ? AND set_name = ? ORDER BY slot_index", (guild_id, set_name)) as cur:
            slots = [(r[0], r[1], r[2]) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT player_id FROM composition_pool WHERE guild_id = ? AND set_name = ?",
            (guild_id, set_name)) as cur:
            pool = [r[0] for r in await cur.fetchall()]
        return {"set_name": set_name, "source_message_id": head[0],
                "created_at": head[1], "slots": slots, "pool": pool}


async def set_composition_slot(
    guild_id: int, set_name: str, slot_index: int, player_id: int | None) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE composition_slots SET player_id = ? "
            "WHERE guild_id = ? AND set_name = ? AND slot_index = ?",
            (player_id, guild_id, set_name, slot_index))
        await db.commit()


async def list_compositions(guild_id: int) -> list[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT set_name FROM compositions WHERE guild_id = ? ORDER BY set_name",
            (guild_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]
