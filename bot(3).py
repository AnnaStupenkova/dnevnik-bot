# -*- coding: utf-8 -*-
"""Запуск бота «Дневник Состояния» в Telegram.  Команда:  python bot.py"""
import asyncio
import html
import os
import subprocess
import sys
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)

import content as C
from core import Core


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def inline(buttons):
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in buttons])


MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=C.BTN_CANCEL), KeyboardButton(text=C.BTN_SUPPORT)],
    [KeyboardButton(text=C.BTN_PATH)]])


class TG:
    """Тонкая прослойка: core говорит что отправить, TG отправляет."""
    def __init__(self, bot):
        self.bot = bot

    async def send(self, uid, text, buttons=None, menu=False):
        markup = inline(buttons) or (MENU if menu else None)
        await self.bot.send_message(uid, text, reply_markup=markup)

    async def audio(self, uid, file_id, kind, buttons=None):
        if kind == "voice":
            await self.bot.send_voice(uid, file_id, reply_markup=inline(buttons))
        elif kind == "document":
            await self.bot.send_document(uid, file_id, reply_markup=inline(buttons))
        else:
            await self.bot.send_audio(uid, file_id, reply_markup=inline(buttons))

    async def photo(self, uid, png, caption=None):
        await self.bot.send_photo(uid, BufferedInputFile(png, "itog.png"), caption=caption)

    async def document(self, uid, data, filename, caption=None):
        await self.bot.send_document(uid, BufferedInputFile(data, filename), caption=caption)

    async def edit_buttons(self, uid, msg_id, buttons):
        try:
            await self.bot.edit_message_reply_markup(chat_id=uid, message_id=msg_id, reply_markup=inline(buttons))
        except Exception:
            pass


async def main():
    load_env()
    token = os.environ["BOT_TOKEN"]
    admins = [int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x]
    start = os.environ.get("PROGRAM_START", "").strip()
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    core = Core(os.environ.get("DB_PATH", "dnevnik.db"), TG(bot), admins,
                date.fromisoformat(start) if start else None,
                admin_code=os.environ.get("ADMIN_CODE", ""))
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def _start(m: Message):
        await core.on_start(m.from_user.id, m.from_user.first_name or "")

    @dp.message(Command("id"))
    async def _id(m: Message):
        await m.answer("Твой номер: <code>%d</code>" % m.from_user.id)

    @dp.message(Command("update"))
    async def _update(m: Message):
        if m.from_user.id not in core.admins:
            return
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            r = subprocess.run(["git", "pull", "--ff-only"], cwd=here, capture_output=True, text=True, timeout=60)
            out = (r.stdout + r.stderr).strip()[-600:]
        except Exception as e:
            await m.answer("Не получилось: %s" % e)
            return
        if r.returncode != 0:
            await m.answer("Не получилось:\n<code>%s</code>" % html.escape(out))
            return
        chk = subprocess.run([sys.executable, "-c", "import content, core, stats"], cwd=here,
                             capture_output=True, text=True, timeout=60)
        if chk.returncode != 0:
            subprocess.run(["git", "reset", "--hard", "HEAD@{1}"], cwd=here)
            await m.answer("В новых файлах ошибка. Я вернула прошлую версию и работаю дальше.\n<code>%s</code>"
                           % html.escape(chk.stderr.strip()[-500:]))
            return
        await m.answer("Обновлено. Перезапускаюсь, это 5 секунд.\n<code>%s</code>" % html.escape(out))
        asyncio.get_running_loop().call_later(1, os._exit, 1)   # служба на сервере запустит бота заново

    @dp.message(F.text.startswith("/"))
    async def _cmd(m: Message):
        parts = m.text[1:].split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        if not await core.on_command(m.from_user.id, cmd, parts[1:]):
            await m.answer(C.IDLE_HINT)

    @dp.message(F.voice)
    async def _voice(m: Message):
        await core.on_voice(m.from_user.id, m.voice.file_id, "voice")

    @dp.message(F.audio)
    async def _audio(m: Message):
        await core.on_voice(m.from_user.id, m.audio.file_id, "audio")

    @dp.message(F.document)
    async def _doc(m: Message):
        if (m.document.mime_type or "").startswith("audio/"):
            await core.on_voice(m.from_user.id, m.document.file_id, "document")

    @dp.message(F.text)
    async def _text(m: Message):
        await core.on_text(m.from_user.id, m.text)

    @dp.callback_query()
    async def _cb(c: CallbackQuery):
        toast = await core.on_callback(c.from_user.id, c.data or "", c.message.message_id if c.message else None)
        await c.answer(toast or None)

    async def ticker():
        while True:
            try:
                await core.tick()
            except Exception as e:
                print("ticker error", repr(e))
            await asyncio.sleep(30)

    asyncio.create_task(ticker())
    print("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
