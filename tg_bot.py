"""Telegram-бот (aiogram 3). Вся логика — в core.commands."""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from core import commands, db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg")

bot = Bot(os.environ["TG_TOKEN"])
dp = Dispatcher()


def markup(r: commands.Reply):
    """Кнопки под сообщением (inline), а если их нет — постоянное меню внизу."""
    if r.buttons:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label[:60], callback_data=cmd) for label, cmd in row]
            for row in r.buttons
        ])
    menu = commands.MENU_ADMIN if r.admin else commands.MENU_USER
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in menu], resize_keyboard=True
    )


async def send_reply(chat_id: int, r: commands.Reply):
    await bot.send_message(chat_id, r.text, reply_markup=markup(r))


@dp.message(F.text, F.chat.type == "private")
async def on_text(m: Message):
    r = commands.handle("tg", m.from_user.id, m.from_user.full_name, m.text, m.from_user.username)
    await send_reply(m.chat.id, r)


@dp.callback_query()
async def on_button(cb: CallbackQuery):
    await cb.answer()  # убрать «часики» на кнопке
    r = commands.handle("tg", cb.from_user.id, cb.from_user.full_name, cb.data or "", cb.from_user.username)
    await send_reply(cb.from_user.id, r)


async def sender():
    """Забирает из общей очереди сообщения для Telegram (рассылки, ответы поддержки, уведомления)."""
    while True:
        for it in db.pending("tg"):
            try:
                await bot.send_message(it["peer_id"], it["text"])
                db.mark_sent(it["id"])
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                db.mark_sent(it["id"])  # пользователь заблокировал бота
            except Exception:
                log.exception("send failed")
                db.mark_failed(it["id"])
            await asyncio.sleep(0.05)  # ~20 сообщений/сек, лимит TG — 30
        await asyncio.sleep(3)


async def main():
    db.init()
    task = asyncio.create_task(sender())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
