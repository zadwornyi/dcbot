"""Конфигурация бота, загружается из .env"""
import os

from dotenv import load_dotenv

load_dotenv()


def _int_or_none(name: str):
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Переменная {name} должна быть числом (ID), сейчас: {raw!r}")


def _channel_list(name: str):
    """Парсит 'Rules:123, Intro:456' -> [('Rules', 123), ('Intro', 456)].

    Метку можно опустить ('123' -> ('', 123)).
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            label, _, cid = part.rpartition(":")
            label = label.strip()
        else:
            label, cid = "", part
        cid = cid.strip()
        try:
            result.append((label, int(cid)))
        except ValueError:
            raise ValueError(f"В {name} ожидался ID канала (число), получено: {cid!r}")
    return result


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ECONOMY_CHANNEL_ID = _int_or_none("ECONOMY_CHANNEL_ID")
ADMIN_ROLE_ID = _int_or_none("ADMIN_ROLE_ID")
MEMBER_ROLE_ID = _int_or_none("MEMBER_ROLE_ID")  # роль "участник" — кого зовёт !reminder
GUILD_ID = _int_or_none("GUILD_ID")

# Приветствие новичков
WELCOME_CHANNELS = _channel_list("WELCOME_CHANNELS")   # каналы, на которые указывает бот
WELCOME_CHANNEL_ID = _int_or_none("WELCOME_CHANNEL_ID")  # резервный канал, если ЛС закрыты

# Игровые роли
ADMIN_CHANNEL_ID = _int_or_none("ADMIN_CHANNEL_ID")  # канал, куда падают заявки на роли
MAX_GAME_ROLES = 10                                  # сколько ролей максимум может выбрать игрок

PREFIX = os.getenv("PREFIX", "!").strip() or "!"
DB_PATH = os.getenv("DB_PATH", "economy.db").strip() or "economy.db"

# Оформление валюты
CURRENCY_NAME = "серебро"
CURRENCY_EMOJI = "🪙"
EMBED_COLOR = 0xC0C0C0  # серебристый
