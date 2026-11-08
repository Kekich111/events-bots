"""Общая БД (SQLite) для Telegram- и VK-ботов + очередь исходящих сообщений."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

def _load_env():
    """Читает .env из папки проекта (без внешних библиотек)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env()

DB_PATH = os.getenv("DB_PATH", "data/bot.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def now_s() -> str:
    return now().strftime("%Y-%m-%d %H:%M")


def _ids(name: str) -> set:
    raw = os.getenv(name, "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x}


# Организаторы (админы) задаются в .env
ADMIN_IDS = {"tg": _ids("ADMIN_TG_IDS"), "vk": _ids("ADMIN_VK_IDS")}
COL = {"tg": "tg_id", "vk": "vk_id"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE,
    vk_id INTEGER UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    tg_username TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    subscribed INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM' (локальное время)
    seeking INTEGER NOT NULL DEFAULT 0,   -- сколько людей нужно набрать (0 = не набираем)
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    captain_id INTEGER NOT NULL REFERENCES users(id),
    max_size INTEGER NOT NULL DEFAULT 5
);
CREATE TABLE IF NOT EXISTS team_members (
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (team_id, user_id)
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    from_staff INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Очередь исходящих: ядро кладёт сюда, каждый бот забирает свою платформу
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,               -- 'tg' | 'vk'
    peer_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(platform, sent);
CREATE TABLE IF NOT EXISTS link_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
-- Состояние диалога (ждём название команды, текст обращения и т.п.)
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")  # два бота пишут в одну БД
        c.executescript(SCHEMA)
        # миграция старых баз: добавляем столбец с Telegram-юзернеймом
        if "tg_username" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:
            c.execute("ALTER TABLE users ADD COLUMN tg_username TEXT")
        # заранее заводим админов, чтобы им приходили обращения из поддержки
        for platform, ids in ADMIN_IDS.items():
            col = COL[platform]
            for pid in ids:
                if not c.execute(f"SELECT 1 FROM users WHERE {col}=?", (pid,)).fetchone():
                    c.execute(
                        f"INSERT INTO users({col}, name, is_admin, created_at) VALUES(?,?,1,?)",
                        (pid, "Организатор", now_s()),
                    )


def get_or_create_user(platform: str, pid: int, name: str, username: str = None) -> dict:
    col = COL[platform]
    with db() as c:
        row = c.execute(f"SELECT * FROM users WHERE {col}=?", (pid,)).fetchone()
        if row is None:
            admin = 1 if pid in ADMIN_IDS[platform] else 0
            cur = c.execute(
                f"INSERT INTO users({col}, name, is_admin, created_at) VALUES(?,?,?,?)",
                (pid, name, admin, now_s()),
            )
            row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        else:
            if pid in ADMIN_IDS[platform] and not row["is_admin"]:
                c.execute("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
            if name and (not row["name"] or row["name"] == "Организатор"):
                c.execute("UPDATE users SET name=? WHERE id=?", (name, row["id"]))
            row = c.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        if platform == "tg" and (row["tg_username"] or None) != (username or None):
            c.execute("UPDATE users SET tg_username=? WHERE id=?", (username or None, row["id"]))
            row = c.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        return dict(row)


# ---------- очередь исходящих ----------

def notify(c, user, text: str):
    """Положить сообщение пользователю в очередь (в рамках открытой транзакции c).
    Если аккаунт привязан к обеим соцсетям, шлём в Telegram, иначе в VK (без дублей)."""
    if user["tg_id"]:
        platform, peer = "tg", user["tg_id"]
    elif user["vk_id"]:
        platform, peer = "vk", user["vk_id"]
    else:
        return
    c.execute(
        "INSERT INTO outbox(platform, peer_id, text, created_at) VALUES(?,?,?,?)",
        (platform, peer, text, now_s()),
    )


def pending(platform: str, limit: int = 20):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM outbox WHERE platform=? AND sent=0 AND attempts<3 ORDER BY id LIMIT ?",
            (platform, limit),
        )]


def mark_sent(msg_id: int):
    with db() as c:
        c.execute("UPDATE outbox SET sent=1 WHERE id=?", (msg_id,))


def mark_failed(msg_id: int):
    with db() as c:
        c.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (msg_id,))
