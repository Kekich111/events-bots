"""VK-бот (vkbottle 4). Вся логика — в core.commands."""
import json
import os
import random
import threading
import time
import urllib.parse
import urllib.request

from vkbottle import Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Bot, Message

from core import commands, db

TOKEN = os.environ["VK_TOKEN"]
API_VERSION = "5.199"

bot = Bot(token=TOKEN)


def build_keyboard(r: commands.Reply) -> str:
    """Кнопки под сообщением (inline), а если их нет — постоянное меню внизу."""
    if r.buttons:
        kb = Keyboard(inline=True)  # у VK лимит: до 10 кнопок и 6 строк в inline-клавиатуре
        left = 10
        for i, row in enumerate(r.buttons[:6]):
            row = row[:left]
            if not row:
                break
            if i:
                kb.row()
            for label, cmd in row:
                kb.add(Text(label[:40], payload={"c": cmd}))
            left -= len(row)
        return kb.get_json()
    menu = commands.MENU_ADMIN if r.admin else commands.MENU_USER
    kb = Keyboard(one_time=False, inline=False)
    for i, row in enumerate(menu):
        if i:
            kb.row()
        for j, label in enumerate(row):
            kb.add(Text(label), color=KeyboardButtonColor.PRIMARY if (i, j) == (0, 0) else KeyboardButtonColor.SECONDARY)
    return kb.get_json()


@bot.on.private_message()
async def on_message(message: Message):
    text = message.text or ""
    if message.payload:  # нажатие inline-кнопки: команда лежит в payload
        try:
            text = json.loads(message.payload).get("c") or text
        except Exception:
            pass
    try:
        user = await message.get_user()
        name = f"{user.first_name} {user.last_name}"
    except Exception:
        name = "Пользователь VK"
    r = commands.handle("vk", message.from_id, name, text)
    await message.answer(r.text, keyboard=build_keyboard(r))


# ---------- отправка из общей очереди (рассылки, ответы поддержки, уведомления) ----------
# Работает в отдельном потоке и ходит в VK API напрямую, чтобы не зависеть от версии vkbottle.

def vk_call(method: str, **params):
    params.update(access_token=TOKEN, v=API_VERSION)
    req = urllib.request.Request(
        f"https://api.vk.com/method/{method}",
        data=urllib.parse.urlencode(params).encode(),
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)
    if "error" in res:
        err = res["error"]
        raise RuntimeError(f"VK error {err.get('error_code')}: {err.get('error_msg')}")
    return res["response"]


def sender_loop():
    while True:
        try:
            for it in db.pending("vk"):
                try:
                    vk_call(
                        "messages.send",
                        peer_id=it["peer_id"],
                        message=it["text"],
                        random_id=random.randint(1, 2**31 - 1),
                    )
                    db.mark_sent(it["id"])
                except Exception as e:
                    print(f"VK: не удалось отправить сообщение #{it['id']}: {e}", flush=True)
                    db.mark_failed(it["id"])
                time.sleep(0.06)  # лимит VK — ~20 запросов/сек
        except Exception as e:
            print("VK sender error:", e, flush=True)
        time.sleep(3)


if __name__ == "__main__":
    db.init()
    threading.Thread(target=sender_loop, daemon=True).start()
    bot.run_forever()
