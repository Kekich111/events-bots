"""Планировщик: раз в неделю кладёт в очередь дайджест мероприятий для подписчиков.
Запускается отдельным процессом, чтобы рассылка не дублировалась."""
import os
import time
from datetime import timedelta

from core import db
from core.commands import event_line

DIGEST_DAY = int(os.getenv("DIGEST_DAY", "0"))    # 0 = понедельник
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "10"))  # по времени TZ_NAME
DAYS_AHEAD = int(os.getenv("DIGEST_DAYS", "14"))


def send_digest() -> int:
    t = db.now()
    with db.db() as c:
        events = c.execute(
            "SELECT * FROM events WHERE starts_at>=? AND starts_at<=? ORDER BY starts_at LIMIT 10",
            (db.now_s(), (t + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d %H:%M")),
        ).fetchall()
        if not events:
            return 0
        text = "📅 Мероприятия на ближайшие недели:\n\n" + "\n\n".join(event_line(c, e) for e in events) \
               + "\n\nПодробнее: /event <№>\nОтписаться: /sub"
        subs = c.execute("SELECT * FROM users WHERE subscribed=1").fetchall()
        for s in subs:
            db.notify(c, s, text)
        return len(subs)


def tick():
    t = db.now()
    week = t.strftime("%G-W%V")
    if t.weekday() != DIGEST_DAY or t.hour < DIGEST_HOUR:
        return
    with db.db() as c:
        row = c.execute("SELECT value FROM meta WHERE key='last_digest'").fetchone()
        if row and row["value"] == week:
            return
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_digest', ?)", (week,))
    n = send_digest()
    print(f"digest {week}: {n} recipients")


if __name__ == "__main__":
    db.init()
    while True:
        try:
            tick()
        except Exception as e:
            print("scheduler error:", e)
        time.sleep(60)
