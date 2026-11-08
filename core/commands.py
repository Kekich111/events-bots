"""Вся логика команд. Боты (TG/VK) только передают сюда текст и показывают ответ.

handle() возвращает Reply: текст + кнопки (inline) + флаг админа (для выбора меню).
Диалоги (создание команды/мероприятия, поддержка, рассылка) хранятся в таблице user_state.
"""
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .db import db, now, now_s, notify, get_or_create_user


class UserError(Exception):
    """Понятная пользователю ошибка."""


@dataclass
class Reply:
    text: str
    buttons: list = field(default_factory=list)  # [[(подпись, команда), ...], ...]
    admin: bool = False


BTN_EVENTS = "📅 Мероприятия"
BTN_TEAMS = "👥 Мои команды"
BTN_SUB = "🔔 Подписка"
BTN_SUPPORT = "🆘 Поддержка"
BTN_HELP = "❓ Помощь"
BTN_NEWEVENT = "➕ Мероприятие"
BTN_TICKETS = "🎫 Обращения"
BTN_BROADCAST = "📢 Рассылка"

MENU_USER = [[BTN_EVENTS, BTN_TEAMS], [BTN_SUB, BTN_SUPPORT]]
MENU_ADMIN = MENU_USER + [[BTN_NEWEVENT, BTN_TICKETS, BTN_BROADCAST]]

ALIASES = {
    BTN_EVENTS: "/events", BTN_TEAMS: "/myteams", BTN_SUB: "/sub",
    BTN_SUPPORT: "/support", BTN_HELP: "/help", BTN_NEWEVENT: "/event_new",
    BTN_TICKETS: "/tickets", BTN_BROADCAST: "/broadcast",
    "начать": "/start", "start": "/start", "помощь": "/help",
}
CANCEL = [("✖ Отмена", "/cancel")]
SKIP_CANCEL = [("⏭ Пропустить", "/skip")], CANCEL
# /event_3, /join_5 и т.п. — кликабельные ссылки в Telegram
LINK_RE = re.compile(r"^/(event|join|leave|ticket|close)_(\d+)(@\w+)?$", re.I)


# ---------- вспомогательное ----------

def fmt_dt(s: str) -> str:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%d.%m %H:%M")


def parse_int(s: str, hint: str) -> int:
    try:
        return int(s.strip())
    except ValueError:
        raise UserError(hint)


def parse_dt(s: str) -> str:
    for f in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s.strip(), f).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    raise UserError("Не получилось разобрать дату. Пример: 2026-11-20 18:00 или 20.11.2026 18:00")


def taken_count(c, event_id: int) -> int:
    return c.execute(
        "SELECT COUNT(DISTINCT m.user_id) FROM team_members m "
        "JOIN teams t ON t.id=m.team_id WHERE t.event_id=?", (event_id,)
    ).fetchone()[0]


def event_line(c, e, link: bool = True) -> str:
    s = f"#{e['id']} {e['title']} — {fmt_dt(e['starts_at'])}"
    if e["location"]:
        s += f"\n   📍 {e['location']}"
    if e["seeking"]:
        s += f"\n   🔎 Набор: {taken_count(c, e['id'])}/{e['seeking']} чел."
    if link:
        s += f"\n   👉 /event_{e['id']}"
    return s


def user_team_on_event(c, user_id: int, event_id: int):
    return c.execute(
        "SELECT t.id FROM team_members m JOIN teams t ON t.id=m.team_id "
        "WHERE m.user_id=? AND t.event_id=?", (user_id, event_id)
    ).fetchone()


def upcoming_event(c, event_id: int):
    e = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not e:
        raise UserError("Мероприятие не найдено.")
    if e["starts_at"] < now_s():
        raise UserError("Это мероприятие уже прошло.")
    return e


# ---------- состояние диалога ----------

def set_state(u, state: str, **data):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO user_state(user_id, state, data) VALUES(?,?,?)",
                  (u["id"], state, json.dumps(data, ensure_ascii=False)))


def get_state(u):
    with db() as c:
        r = c.execute("SELECT state, data FROM user_state WHERE user_id=?", (u["id"],)).fetchone()
    return (r["state"], json.loads(r["data"] or "{}")) if r else None


def clear_state(u):
    with db() as c:
        c.execute("DELETE FROM user_state WHERE user_id=?", (u["id"],))


# ---------- общие команды ----------

def cmd_start(u, a):
    return Reply(f"Привет, {u['name']}! 👋\nЯ показываю мероприятия, помогаю собирать команды и отвечаю на вопросы.\n\n"
                 "Пользуйтесь кнопками внизу 👇", [[("📅 Ближайшие мероприятия", "/events")]])


def cmd_help(u, a):
    t = (
        "Всё делается кнопками внизу. Текстом тоже можно:\n"
        "/events — мероприятия\n"
        "/myteams — мои команды\n"
        "/sub — вкл/выкл рассылку\n"
        "/support — написать организаторам (любое сообщение без «/» тоже уйдёт им)\n"
        "/link — связать аккаунты Telegram и VK\n"
        "/cancel — выйти из диалога"
    )
    if u["is_admin"]:
        t += (
            "\n\nОрганизатор:\n"
            "/event_new — создать мероприятие (по шагам)\n"
            "Изменить или удалить мероприятие — кнопки на его карточке\n"
            "/broadcast — рассылка всем\n"
            "/tickets — обращения; /ticket <№> — открыть\n"
            "/reply <№> [текст], /close <№>"
        )
    return t


def cmd_events(u, a):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE starts_at>=? ORDER BY starts_at LIMIT 8", (now_s(),)
        ).fetchall()
        if not rows:
            return "Пока нет запланированных мероприятий."
        text = "📅 Ближайшие мероприятия:\n\n" + "\n\n".join(event_line(c, e, link=False) for e in rows) \
               + "\n\nВыберите мероприятие 👇"
    buttons = [[(f"{e['title'][:24]} · {fmt_dt(e['starts_at'])}", f"/event {e['id']}")] for e in rows]
    return Reply(text, buttons)


def cmd_event(u, a):
    eid = parse_int(a, "Укажите номер мероприятия.")
    with db() as c:
        e = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not e:
            raise UserError("Мероприятие не найдено.")
        out = [event_line(c, e, link=False)]
        if e["description"]:
            out.append("\n" + e["description"])
        teams = c.execute(
            "SELECT t.*, u.name AS cap, (SELECT COUNT(*) FROM team_members WHERE team_id=t.id) AS n "
            "FROM teams t JOIN users u ON u.id=t.captain_id WHERE t.event_id=?", (eid,)
        ).fetchall()
    buttons = []
    if teams:
        out.append("\nКоманды:")
        for t in teams:
            out.append(f"• «{t['name']}» — {t['n']}/{t['max_size']}, капитан {t['cap']}"
                       + ("" if t["n"] < t["max_size"] else " (заполнена)"))
            if t["n"] < t["max_size"] and len(buttons) < 5:
                buttons.append([(f"Вступить: {t['name'][:20]} ({t['n']}/{t['max_size']})", f"/join {t['id']}")])
    else:
        out.append("\nКоманд пока нет — создайте первую 👇")
    if e["starts_at"] >= now_s():
        buttons.append([("➕ Создать свою команду", f"/team_new {eid}")])
    if u["is_admin"]:
        buttons.append([("✏️ Изменить", f"/event_edit {eid}"), ("🗑 Удалить", f"/event_del {eid}")])
    buttons.append([("⬅ Все мероприятия", "/events")])
    return Reply("\n".join(out), buttons)


def set_sub(u, val: int):
    with db() as c:
        c.execute("UPDATE users SET subscribed=? WHERE id=?", (val, u["id"]))
    return "🔔 Рассылка включена: новые мероприятия и дайджест раз в неделю." if val else "🔕 Рассылка выключена. Включить: нажмите «Подписка»."


def cmd_sub(u, a):
    return set_sub(u, 0 if u["subscribed"] else 1)


# ---------- команды ----------

def _split_name_max(s: str):
    body, _, mx = s.partition("|")
    max_size = parse_int(mx, "Максимум людей — число, например: Название | 6") if mx.strip() else 5
    return body.strip(), max(2, min(max_size, 50))


def create_team(u, eid: int, name: str, max_size: int = 5) -> Reply:
    name = name.strip()[:60]
    if not name:
        raise UserError("Название не может быть пустым.")
    with db() as c:
        upcoming_event(c, eid)
        if user_team_on_event(c, u["id"], eid):
            raise UserError("Вы уже состоите в команде на это мероприятие (раздел «Мои команды»).")
        tid = c.execute("INSERT INTO teams(event_id, name, captain_id, max_size) VALUES(?,?,?,?)",
                        (eid, name, u["id"], max_size)).lastrowid
        c.execute("INSERT INTO team_members VALUES(?,?)", (tid, u["id"]))
    return Reply(f"✅ Команда «{name}» создана (до {max_size} чел.). Вы капитан.\n"
                 f"Пусть друзья найдут мероприятие в боте и нажмут «Вступить».",
                 [[("👥 Мои команды", "/myteams")], [("📅 Мероприятия", "/events")]])


def cmd_team_create(u, a):
    parts = a.partition("|")[0].strip().split(None, 1)
    if len(parts) < 2:
        raise UserError("Проще через кнопки: «Мероприятия» → мероприятие → «Создать свою команду».\n"
                        "Или: /team_create <№ мероприятия> <название> [| макс. человек]")
    eid = parse_int(parts[0], "Первым укажите номер мероприятия.")
    name, mx = _split_name_max(parts[1] + ("|" + a.partition("|")[2] if "|" in a else ""))
    return create_team(u, eid, name, mx)


def cmd_team_new(u, a):
    eid = parse_int(a, "Укажите номер мероприятия.")
    with db() as c:
        upcoming_event(c, eid)
        if user_team_on_event(c, u["id"], eid):
            raise UserError("Вы уже состоите в команде на это мероприятие.")
    set_state(u, "team_name", eid=eid)
    return Reply("Шаг 1/2. Как назвать команду? Напишите название.", [CANCEL])


SIZES = [[2, 3, 4], [5, 6, 8], [10, 15, 20]]


def size_prompt(name: str) -> Reply:
    rows = [[(f"{n} чел.", f"/team_size {n}") for n in row] for row in SIZES]
    return Reply(f"Шаг 2/2. Сколько человек максимум в команде «{name}»?\n"
                 "Выберите кнопкой или напишите число (от 2 до 50).", rows + [CANCEL])


def cmd_join(u, a):
    tid = parse_int(a, "Укажите номер команды.")
    with db() as c:
        t = c.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
        if not t:
            raise UserError("Команда не найдена.")
        upcoming_event(c, t["event_id"])
        if c.execute("SELECT 1 FROM team_members WHERE team_id=? AND user_id=?", (tid, u["id"])).fetchone():
            raise UserError("Вы уже в этой команде.")
        if user_team_on_event(c, u["id"], t["event_id"]):
            raise UserError("Вы уже в другой команде на это мероприятие. Сначала выйдите из неё («Мои команды»).")
        n = c.execute("SELECT COUNT(*) FROM team_members WHERE team_id=?", (tid,)).fetchone()[0]
        if n >= t["max_size"]:
            raise UserError("В команде нет мест.")
        c.execute("INSERT INTO team_members VALUES(?,?)", (tid, u["id"]))
        cap = c.execute("SELECT * FROM users WHERE id=?", (t["captain_id"],)).fetchone()
        notify(c, cap, f"👥 {u['name']} вступил(а) в вашу команду «{t['name']}» ({n + 1}/{t['max_size']}).")
    return Reply(f"✅ Вы в команде «{t['name']}» ({n + 1}/{t['max_size']}).", [[("👥 Мои команды", "/myteams")]])


def cmd_leave(u, a):
    tid = parse_int(a, "Укажите номер команды.")
    with db() as c:
        t = c.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
        if not t or not c.execute(
            "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?", (tid, u["id"])
        ).fetchone():
            raise UserError("Вы не состоите в этой команде.")
        c.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?", (tid, u["id"]))
        rest = c.execute("SELECT user_id FROM team_members WHERE team_id=? ORDER BY rowid", (tid,)).fetchall()
        if not rest:
            c.execute("DELETE FROM teams WHERE id=?", (tid,))
            return f"Вы вышли. Команда «{t['name']}» расформирована (никого не осталось)."
        if t["captain_id"] == u["id"]:
            new_cap = rest[0]["user_id"]
            c.execute("UPDATE teams SET captain_id=? WHERE id=?", (new_cap, tid))
            row = c.execute("SELECT * FROM users WHERE id=?", (new_cap,)).fetchone()
            notify(c, row, f"👑 Вы стали капитаном команды «{t['name']}».")
    return f"Вы вышли из команды «{t['name']}»."


def cmd_myteams(u, a):
    with db() as c:
        rows = c.execute(
            "SELECT t.id, t.name, t.captain_id, e.title, e.starts_at, "
            "(SELECT COUNT(*) FROM team_members WHERE team_id=t.id) AS n, t.max_size "
            "FROM team_members m JOIN teams t ON t.id=m.team_id JOIN events e ON e.id=t.event_id "
            "WHERE m.user_id=? ORDER BY e.starts_at", (u["id"],)
        ).fetchall()
    if not rows:
        return Reply("Вы пока не в командах.", [[("📅 Выбрать мероприятие", "/events")]])
    text = "👥 Ваши команды:\n\n" + "\n".join(
        f"«{r['name']}» {'👑' if r['captain_id'] == u['id'] else ''}\n"
        f"   {r['title']} — {fmt_dt(r['starts_at'])}, {r['n']}/{r['max_size']} чел." for r in rows)
    return Reply(text, [[(f"🚪 Выйти: {r['name'][:28]}", f"/leave {r['id']}")] for r in rows[:8]])


# ---------- поддержка ----------

def cmd_support(u, a):
    if a:
        return support_message(u, a)
    set_state(u, "support")
    return Reply("🆘 Напишите свой вопрос — организаторы ответят прямо здесь. "
                 "Можно отправить несколько сообщений подряд.", [[("✔ Выйти из поддержки", "/cancel")]])


def support_message(u, text: str) -> str:
    with db() as c:
        t = c.execute("SELECT id FROM tickets WHERE user_id=? AND status='open'", (u["id"],)).fetchone()
        tid = t["id"] if t else c.execute(
            "INSERT INTO tickets(user_id, created_at) VALUES(?,?)", (u["id"], now_s())).lastrowid
        c.execute("INSERT INTO ticket_messages(ticket_id, text, created_at) VALUES(?,?,?)", (tid, text, now_s()))
        for adm in c.execute("SELECT * FROM users WHERE is_admin=1").fetchall():
            notify(c, adm, f"🆘 Обращение #{tid} от {u['name']}:\n{text}\n\nОткрыть: /ticket_{tid}")
    return f"✅ Отправлено (обращение #{tid}). Ответим здесь, как только сможем."


def cmd_cancel(u, a):
    return "Хорошо, отменено. Выберите действие в меню 👇"


# ---------- привязка аккаунтов ----------

def cmd_link(u, a):
    code = a.strip().upper()
    if not code:
        code = secrets.token_hex(3).upper()
        with db() as c:
            c.execute("DELETE FROM link_codes WHERE user_id=?", (u["id"],))
            c.execute("INSERT INTO link_codes VALUES(?,?,?)",
                      (code, u["id"], (now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")))
        return f"🔗 Код: {code}\nВ течение 15 минут отправьте в боте другой соцсети:\n/link {code}"
    cols = ("tg_id", "vk_id")
    with db() as c:
        row = c.execute("SELECT * FROM link_codes WHERE code=? AND expires_at>=?", (code, now_s())).fetchone()
        if not row:
            raise UserError("Код не найден или истёк. Получите новый: /link")
        keep = c.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        drop = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if keep["id"] == drop["id"]:
            raise UserError("Код создан в этом же боте — введите его в другом.")
        if any(keep[col] and drop[col] for col in cols):
            raise UserError("К тому аккаунту уже привязан другой профиль этой соцсети.")
        vals = {col: drop[col] for col in cols if drop[col]}
        c.execute("INSERT OR IGNORE INTO team_members(team_id, user_id) "
                  "SELECT team_id, ? FROM team_members WHERE user_id=?", (keep["id"], drop["id"]))
        c.execute("DELETE FROM team_members WHERE user_id=?", (drop["id"],))
        for tbl, col in (("teams", "captain_id"), ("tickets", "user_id"), ("events", "created_by")):
            c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (keep["id"], drop["id"]))
        c.execute("DELETE FROM link_codes WHERE user_id IN (?,?)", (keep["id"], drop["id"]))
        c.execute("UPDATE users SET tg_id=NULL, vk_id=NULL WHERE id=?", (drop["id"],))
        sets = ", ".join(f"{col}=?" for col in vals)
        c.execute(f"UPDATE users SET {sets}, is_admin=? WHERE id=?",
                  (*vals.values(), max(keep["is_admin"], drop["is_admin"]), keep["id"]))
        c.execute("DELETE FROM users WHERE id=?", (drop["id"],))
    return "✅ Аккаунты связаны: команды и обращения теперь общие. Уведомления приходят в Telegram."


# ---------- организатор ----------

def create_event(u, title: str, starts: str, loc: str = "", desc: str = "", seeking: int = 0) -> Reply:
    with db() as c:
        eid = c.execute(
            "INSERT INTO events(title, description, location, starts_at, seeking, created_by, created_at) "
            "VALUES(?,?,?,?,?,?,?)", (title, desc, loc, starts, seeking, u["id"], now_s())
        ).lastrowid
        e = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        text = "🆕 Новое мероприятие!\n\n" + event_line(c, e) + (f"\n\n{desc}" if desc else "")
        subs = c.execute("SELECT * FROM users WHERE subscribed=1").fetchall()
        for s in subs:
            notify(c, s, text)
    return Reply(f"✅ Мероприятие #{eid} создано. Рассылка поставлена в очередь: {len(subs)} чел.",
                 [[("📅 Мероприятия", "/events")]])


def cmd_event_new(u, a):
    set_state(u, "ev_title")
    return Reply("➕ Новое мероприятие.\n\nШаг 1/5. Название?", [CANCEL])


def cmd_event_add(u, a):
    p = [x.strip() for x in a.split("|")]
    if len(p) < 2 or not p[0]:
        raise UserError("Проще пошагово: нажмите «➕ Мероприятие».\n"
                        "Или: /event_add Название | 2026-11-20 18:00 | Место | Описание | Сколько людей ищем")
    seeking = parse_int(p[4], "«Сколько людей ищем» — число.") if len(p) > 4 and p[4] else 0
    return create_event(u, p[0], parse_dt(p[1]), p[2] if len(p) > 2 else "", p[3] if len(p) > 3 else "", seeking)


def _event_or_error(c, eid: int):
    e = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not e:
        raise UserError("Мероприятие не найдено.")
    return e


def _notify_participants(c, eid: int, text: str) -> int:
    rows = c.execute(
        "SELECT DISTINCT u.* FROM team_members m JOIN teams t ON t.id=m.team_id "
        "JOIN users u ON u.id=m.user_id WHERE t.event_id=?", (eid,)
    ).fetchall()
    for r in rows:
        notify(c, r, text)
    return len(rows)


def cmd_event_del(u, a):
    eid = parse_int(a, "Укажите номер мероприятия.")
    with db() as c:
        e = _event_or_error(c, eid)
        teams = c.execute("SELECT COUNT(*) FROM teams WHERE event_id=?", (eid,)).fetchone()[0]
        people = taken_count(c, eid)
    return Reply(f"🗑 Удалить «{e['title']}» ({fmt_dt(e['starts_at'])})?\n"
                 f"Команд: {teams}, участников: {people}. Участники получат уведомление об отмене.",
                 [[("✅ Да, удалить", f"/event_del_yes {eid}")], [("⬅ Нет, назад", f"/event {eid}")]])


def cmd_event_del_yes(u, a):
    eid = parse_int(a, "Укажите номер мероприятия.")
    with db() as c:
        e = _event_or_error(c, eid)
        n = _notify_participants(c, eid, f"❌ Мероприятие «{e['title']}» ({fmt_dt(e['starts_at'])}) отменено. "
                                         "Ваша команда расформирована.")
        c.execute("DELETE FROM events WHERE id=?", (eid,))  # команды удаляются каскадом
    return Reply(f"🗑 Мероприятие «{e['title']}» удалено. Уведомлено участников: {n}.",
                 [[("📅 Мероприятия", "/events")]])


EDIT_FIELDS = {
    "title": ("Название", "title", False),
    "date": ("Дата и время", "starts_at", False),
    "place": ("Место", "location", True),
    "desc": ("Описание", "description", True),
    "seek": ("Сколько людей ищем", "seeking", True),
}


def cmd_event_edit(u, a):
    eid = parse_int(a, "Укажите номер мероприятия.")
    with db() as c:
        e = _event_or_error(c, eid)
    rows = [[(f"✏️ {label}", f"/event_field {eid} {key}")] for key, (label, _, _) in EDIT_FIELDS.items()]
    return Reply(f"Что изменить в «{e['title']}»?", rows + [[("⬅ Назад", f"/event {eid}")]])


def cmd_event_field(u, a):
    parts = a.split()
    if len(parts) != 2 or parts[1] not in EDIT_FIELDS:
        raise UserError("Откройте мероприятие и нажмите «Изменить».")
    eid, key = parse_int(parts[0], "Укажите номер мероприятия."), parts[1]
    label, col, optional = EDIT_FIELDS[key]
    with db() as c:
        e = _event_or_error(c, eid)
    cur = fmt_dt(e[col]) if key == "date" else (e[col] or "—")
    hint = {"date": "Например: 2026-11-20 18:00 или 20.11.2026 18:00.",
            "seek": "Число людей. Чтобы выключить набор — «Очистить».",
            "place": "Чтобы убрать место — «Очистить».", "desc": "Чтобы убрать описание — «Очистить»."}.get(key, "")
    set_state(u, "ev_edit", eid=eid, field=key)
    buttons = ([[("🧹 Очистить", "/skip")]] if optional else []) + [CANCEL]
    return Reply(f"{label} сейчас: {cur}\nНапишите новое значение. {hint}".strip(), buttons)


def do_broadcast(text: str) -> str:
    with db() as c:
        subs = c.execute("SELECT * FROM users WHERE subscribed=1").fetchall()
        for s in subs:
            notify(c, s, "📢 " + text)
    return f"✅ Рассылка поставлена в очередь: {len(subs)} чел."


def cmd_broadcast(u, a):
    if a:
        return do_broadcast(a)
    set_state(u, "broadcast")
    return Reply("📢 Напишите текст рассылки одним сообщением — его получат все подписчики.", [CANCEL])


def cmd_tickets(u, a):
    with db() as c:
        rows = c.execute(
            "SELECT t.id, u.name, (SELECT text FROM ticket_messages WHERE ticket_id=t.id "
            "ORDER BY id DESC LIMIT 1) AS last FROM tickets t JOIN users u ON u.id=t.user_id "
            "WHERE t.status='open' ORDER BY t.id LIMIT 8"
        ).fetchall()
    if not rows:
        return "Открытых обращений нет 🎉"
    text = "🎫 Открытые обращения:\n\n" + "\n".join(f"#{r['id']} {r['name']}: {r['last'][:80]}" for r in rows)
    return Reply(text, [[(f"#{r['id']} {r['name'][:24]}", f"/ticket {r['id']}")] for r in rows])


def cmd_ticket(u, a):
    tid = parse_int(a, "Укажите номер обращения.")
    with db() as c:
        t = c.execute("SELECT t.*, u.name FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=?", (tid,)).fetchone()
        if not t:
            raise UserError("Обращение не найдено.")
        msgs = c.execute("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 8", (tid,)).fetchall()[::-1]
    text = f"🎫 Обращение #{tid} — {t['name']} ({'открыто' if t['status'] == 'open' else 'закрыто'})\n\n" + \
           "\n".join(("🛠 " if m["from_staff"] else "👤 ") + m["text"] for m in msgs)
    buttons = [[("💬 Ответить", f"/reply {tid}")], [("✅ Закрыть", f"/close {tid}")]] if t["status"] == "open" else []
    return Reply(text, buttons + [[("🎫 Все обращения", "/tickets")]])


def do_reply(tid: int, text: str) -> Reply:
    with db() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not t:
            raise UserError("Обращение не найдено.")
        c.execute("INSERT INTO ticket_messages(ticket_id, from_staff, text, created_at) VALUES(?,1,?,?)",
                  (tid, text, now_s()))
        usr = c.execute("SELECT * FROM users WHERE id=?", (t["user_id"],)).fetchone()
        notify(c, usr, f"💬 Поддержка (обращение #{tid}):\n{text}")
    return Reply(f"✅ Ответ на #{tid} отправлен.", [[("✅ Закрыть обращение", f"/close {tid}")], [("🎫 Все обращения", "/tickets")]])


def cmd_reply(u, a):
    parts = a.split(None, 1)
    if not parts:
        raise UserError("Откройте обращение: «🎫 Обращения» и нажмите «Ответить».")
    tid = parse_int(parts[0], "Укажите номер обращения.")
    if len(parts) == 1:
        with db() as c:
            if not c.execute("SELECT 1 FROM tickets WHERE id=?", (tid,)).fetchone():
                raise UserError("Обращение не найдено.")
        set_state(u, "reply", tid=tid)
        return Reply(f"💬 Напишите ответ на обращение #{tid} одним сообщением.", [CANCEL])
    return do_reply(tid, parts[1])


def cmd_close(u, a):
    tid = parse_int(a, "Укажите номер обращения.")
    with db() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not t:
            raise UserError("Обращение не найдено.")
        c.execute("UPDATE tickets SET status='closed' WHERE id=?", (tid,))
        usr = c.execute("SELECT * FROM users WHERE id=?", (t["user_id"],)).fetchone()
        notify(c, usr, f"Обращение #{tid} закрыто. Если остались вопросы — просто напишите.")
    return Reply(f"Обращение #{tid} закрыто.", [[("🎫 Все обращения", "/tickets")]])


# ---------- диалоги (ответы на вопросы бота) ----------

def continue_dialog(u, st, text: str):
    state, d = st
    skip = text == "-"
    if state == "support":
        return Reply(support_message(u, text) + "\nМожно дописать ещё.", [[("✔ Готово", "/cancel")]])
    if state == "team_name":
        if "|" in text:  # быстрый ввод: «Название | 8»
            name, mx = _split_name_max(text)
            r = create_team(u, d["eid"], name, mx)
            clear_state(u)
            return r
        name = text.strip()[:60]
        if not name:
            raise UserError("Название не может быть пустым.")
        set_state(u, "team_size", eid=d["eid"], name=name)
        return size_prompt(name)
    if state == "team_size":
        size = parse_int(text.lower().replace("чел.", "").replace("чел", ""), "Нужно число от 2 до 50 — выберите кнопкой или напишите.")
        if not 2 <= size <= 50:
            raise UserError("Размер команды — от 2 до 50 человек.")
        r = create_team(u, d["eid"], d["name"], size)
        clear_state(u)
        return r
    if state == "ev_title":
        set_state(u, "ev_date", title=text[:100])
        ex = (now() + timedelta(days=7)).strftime("%Y-%m-%d 18:00")
        return Reply(f"Шаг 2/5. Когда? Дата и время, например: {ex}", [CANCEL])
    if state == "ev_date":
        d["starts"] = parse_dt(text)
        set_state(u, "ev_place", **d)
        return Reply("Шаг 3/5. Место проведения?", list(SKIP_CANCEL))
    if state == "ev_place":
        d["loc"] = "" if skip else text[:200]
        set_state(u, "ev_desc", **d)
        return Reply("Шаг 4/5. Короткое описание?", list(SKIP_CANCEL))
    if state == "ev_desc":
        d["desc"] = "" if skip else text[:1000]
        set_state(u, "ev_seek", **d)
        return Reply("Шаг 5/5. Сколько людей нужно набрать? Число (если набор не ведётся — пропустите).", list(SKIP_CANCEL))
    if state == "ev_seek":
        seeking = 0 if skip else parse_int(text, "Нужно число, например 10 (или нажмите «Пропустить»).")
        r = create_event(u, d["title"], d["starts"], d.get("loc", ""), d.get("desc", ""), seeking)
        clear_state(u)
        return r
    if state == "ev_edit":
        key, eid = d["field"], d["eid"]
        label, col, optional = EDIT_FIELDS[key]
        if key == "title":
            val = text.strip()[:100]
            if not val:
                raise UserError("Название не может быть пустым.")
        elif key == "date":
            val = parse_dt(text)
        elif key == "seek":
            val = 0 if skip else parse_int(text, "Нужно число, например 10 (или нажмите «Очистить»).")
            if val < 0:
                raise UserError("Число не может быть отрицательным.")
        else:
            val = "" if skip else text.strip()[:1000 if key == "desc" else 200]
        with db() as c:
            _event_or_error(c, eid)
            c.execute(f"UPDATE events SET {col}=? WHERE id=?", (val, eid))
            e = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
            n = 0
            if key in ("title", "date", "place"):
                shown = fmt_dt(val) if key == "date" else (val or "не указано")
                n = _notify_participants(c, eid, f"ℹ️ Изменение в мероприятии «{e['title']}»:\n{label}: {shown}")
        clear_state(u)
        extra = f" Участники уведомлены: {n}." if n else ""
        return Reply(f"✅ Обновлено.{extra}", [[("⬅ К мероприятию", f"/event {eid}")], [("✏️ Изменить ещё", f"/event_edit {eid}")]])
    if state == "broadcast":
        clear_state(u)
        return do_broadcast(text)
    if state == "reply":
        r = do_reply(d["tid"], text)
        clear_state(u)
        return r
    clear_state(u)
    return "Не понял. Откройте меню 👇 или /help"


COMMANDS = {
    "start": cmd_start, "help": cmd_help, "events": cmd_events, "event": cmd_event,
    "team_create": cmd_team_create, "team_new": cmd_team_new, "join": cmd_join, "leave": cmd_leave,
    "myteams": cmd_myteams, "sub": cmd_sub, "subscribe": lambda u, a: set_sub(u, 1),
    "unsubscribe": lambda u, a: set_sub(u, 0), "support": cmd_support, "cancel": cmd_cancel, "link": cmd_link,
    "event_new": cmd_event_new, "event_add": cmd_event_add, "event_del": cmd_event_del,
    "event_del_yes": cmd_event_del_yes, "event_edit": cmd_event_edit, "event_field": cmd_event_field,
    "broadcast": cmd_broadcast, "tickets": cmd_tickets, "ticket": cmd_ticket, "reply": cmd_reply,
    "close": cmd_close,
}
ADMIN_ONLY = {"event_new", "event_add", "event_del", "event_del_yes", "event_edit", "event_field", "broadcast", "tickets", "ticket", "reply", "close"}


def handle(platform: str, pid: int, name: str, text: str) -> Reply:
    """Единая точка входа для обоих ботов."""
    u = get_or_create_user(platform, pid, name)
    text = (text or "").strip()
    try:
        if not text:
            out = "Отправьте текстовое сообщение."
        else:
            text = ALIASES.get(text, ALIASES.get(text.lower(), text))
            m = LINK_RE.match(text)
            if m:
                text = f"/{m.group(1).lower()} {m.group(2)}"
            if text.startswith("/"):
                parts = text.split(None, 1)
                cmd = parts[0][1:].split("@")[0].lower()
                args = parts[1].strip() if len(parts) > 1 else ""
                st = get_state(u)
                if cmd == "skip":
                    out = continue_dialog(u, st, "-") if st else "Нечего пропускать."
                elif cmd == "team_size":  # кнопка выбора размера работает внутри диалога
                    if st and st[0] == "team_size":
                        out = continue_dialog(u, st, args)
                    else:
                        out = Reply("Выбор размера устарел. Начните заново: «Мероприятия» → мероприятие → «Создать свою команду».",
                                    [[("📅 Мероприятия", "/events")]])
                elif cmd not in COMMANDS:
                    out = "Неизвестная команда. Список: /help"
                elif cmd in ADMIN_ONLY and not u["is_admin"]:
                    out = "Эта команда только для организаторов."
                else:
                    clear_state(u)  # любая команда выводит из диалога
                    out = COMMANDS[cmd](u, args)
            else:
                st = get_state(u)
                if st:
                    out = continue_dialog(u, st, text)
                elif u["is_admin"]:
                    out = "Не понял. Откройте меню 👇 или /help. Чтобы написать в поддержку, нажмите «🆘 Поддержка»."
                else:
                    out = Reply(support_message(u, text) + "\nМожно дописать ещё.")
    except UserError as e:
        out = Reply(str(e), [CANCEL] if get_state(u) else [])  # в диалоге оставляем «Отмена»
    r = out if isinstance(out, Reply) else Reply(out)
    r.text = r.text[:3800]
    r.admin = bool(u["is_admin"])
    return r
