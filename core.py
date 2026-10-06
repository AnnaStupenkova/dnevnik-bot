# -*- coding: utf-8 -*-
"""Логика «Дневника Состояния». Не зависит от Telegram: общается через объект tg."""
import csv
import html
import io
import json
import os
import re
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone

import content as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, tz INTEGER, wake TEXT, bed TEXT,
  start_date TEXT, state TEXT, ctx TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS answers(
  user_id INTEGER, day INTEGER, block TEXT, key TEXT, value TEXT, ts TEXT,
  PRIMARY KEY(user_id, day, block, key));
CREATE TABLE IF NOT EXISTS events(
  user_id INTEGER, day INTEGER, key TEXT, ts TEXT,
  PRIMARY KEY(user_id, day, key));
CREATE TABLE IF NOT EXISTS media(slot TEXT PRIMARY KEY, file_id TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS thoughts(user_id INTEGER, ts TEXT, thought TEXT, instead TEXT);
CREATE TABLE IF NOT EXISTS voices(
  user_id INTEGER, day INTEGER, file_id TEXT, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS plays(user_id INTEGER, lid TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
"""

MENU_BUTTONS = (C.BTN_CANCEL, C.BTN_SUPPORT, C.BTN_PATH)
ONBOARDING = ("consent", "name", "tz", "wake", "bed")


def parse_hhmm(text):
    """'21:40', '21.40', '2140', '21 40', '9' -> минуты от полуночи или None."""
    t = text.strip().replace(".", ":").replace(" ", ":").replace("-", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t) or re.fullmatch(r"(\d{2})(\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    elif re.fullmatch(r"\d{1,2}", t):
        h, mi = int(t), 0
    else:
        return None
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h * 60 + mi
    return None


def fmt(minutes):
    minutes %= 1440
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def evening_minute(bed_min):
    """Вечерняя рефлексия приходит за час до сна, но не позже 23:30."""
    b = bed_min if bed_min >= 12 * 60 else bed_min + 1440
    return min(b - 60, 23 * 60 + 30)


class Core:
    def __init__(self, db_path, tg, admin_ids=(), program_start=None, clock=None, admin_code=""):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.tg = tg
        self.admins = set(admin_ids) | {r["id"] for r in self.db.execute("SELECT id FROM admins")}
        self.admin_code = (admin_code or "").strip()
        self.program_start = program_start  # date или None
        self.clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self.unpack_images()

    # ---------- картинки ----------
    HERE = os.path.dirname(os.path.abspath(__file__))
    IMG_DIR = os.path.join(HERE, "images")

    def unpack_images(self):
        """Картинки лежат в images.zip рядом с ботом. Распаковываем, если архив новее папки."""
        z = os.path.join(self.HERE, "images.zip")
        mark = os.path.join(self.IMG_DIR, ".stamp")
        try:
            if os.path.exists(z) and (not os.path.exists(mark) or os.path.getmtime(z) > os.path.getmtime(mark)):
                os.makedirs(self.IMG_DIR, exist_ok=True)
                with zipfile.ZipFile(z) as f:
                    for n in f.namelist():
                        if n.endswith(".jpg") and "/" not in n and ".." not in n:
                            open(os.path.join(self.IMG_DIR, n), "wb").write(f.read(n))
                open(mark, "w").write("ok")
        except Exception as e:
            print("images error", repr(e))

    async def pic(self, uid, name, caption=None):
        """Отправляет картинку, если она есть. Возвращает True, если отправила."""
        p = os.path.join(self.IMG_DIR, name + ".jpg")
        if not os.path.exists(p):
            return False
        try:
            await self.tg.photo(uid, open(p, "rb").read(), caption)
            return True
        except Exception as e:
            print("pic error", name, repr(e))
            return False

    # ---------- база ----------
    def q(self, sql, *args):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    def user(self, uid):
        r = self.q("SELECT * FROM users WHERE id=?", uid).fetchone()
        if not r:
            return None
        u = dict(r)
        u["ctx"] = json.loads(u["ctx"] or "{}")
        return u

    def set_state(self, uid, state, ctx=None):
        self.q("UPDATE users SET state=?, ctx=? WHERE id=?", state,
               json.dumps(ctx or {}, ensure_ascii=False), uid)

    def save(self, uid, day, block, key, value):
        self.q("INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?,?)",
               uid, day, block, key, str(value), self.clock().isoformat(timespec="seconds"))

    def answers(self, uid, block, day=None):
        if day is None:
            rows = self.q("SELECT day,key,value FROM answers WHERE user_id=? AND block=?", uid, block)
            return [(r["day"], r["key"], r["value"]) for r in rows]
        rows = self.q("SELECT key,value FROM answers WHERE user_id=? AND block=? AND day=?", uid, block, day)
        return {r["key"]: r["value"] for r in rows}

    def has(self, uid, day, key):
        return self.q("SELECT 1 FROM events WHERE user_id=? AND day=? AND key=?", uid, day, key).fetchone() is not None

    def mark(self, uid, day, key):
        self.q("INSERT OR IGNORE INTO events VALUES(?,?,?,?)", uid, day, key,
               self.clock().isoformat(timespec="seconds"))

    def event_ts(self, uid, day, key):
        r = self.q("SELECT ts FROM events WHERE user_id=? AND day=? AND key=?", uid, day, key).fetchone()
        return datetime.fromisoformat(r["ts"]) if r else None

    def media(self, slot):
        r = self.q("SELECT file_id, kind FROM media WHERE slot=?", slot).fetchone()
        return (r["file_id"], r["kind"]) if r else None

    # ---------- время ----------
    def local_now(self, u):
        return self.clock() + timedelta(minutes=u["tz"] or 0)

    def day_of(self, u):
        if not u.get("start_date"):
            return 0
        return (self.local_now(u).date() - date.fromisoformat(u["start_date"])).days + 1

    # ---------- знакомство ----------
    async def on_start(self, uid, first_name=""):
        u = self.user(uid)
        if u and u["start_date"]:
            await self.tg.send(uid, C.IDLE_HINT, menu=True)
            return
        if not u:
            self.q("INSERT INTO users(id,name,created) VALUES(?,?,?)", uid, first_name or "",
                   self.clock().isoformat(timespec="seconds"))
        self.set_state(uid, "consent")
        await self.pic(uid, "cover")
        await self.tg.send(uid, C.WELCOME, buttons=[[("Да", "consent:yes"), ("Нет", "consent:no")]])

    async def ask_time(self, uid, text, options, prefix):
        rows = [[(o, "%s:%s" % (prefix, o)) for o in options[i:i + 4]] for i in range(0, len(options), 4)]
        await self.tg.send(uid, text, buttons=rows)

    async def set_wake(self, uid, minutes):
        self.q("UPDATE users SET wake=? WHERE id=?", fmt(minutes), uid)
        self.set_state(uid, "bed")
        await self.ask_time(uid, C.ASK_BED, C.BED_OPTIONS, "bed")

    async def set_bed(self, uid, minutes):
        self.q("UPDATE users SET bed=? WHERE id=?", fmt(minutes), uid)
        await self.pic(uid, "zamer_start")
        await self.tg.send(uid, C.BEFORE_MEASURE)
        await self.start_flow(uid, "measure", 0, "start")

    async def finish_onboarding(self, uid):
        u = self.user(uid)
        start = self.local_now(u).date() + timedelta(days=1)
        if self.program_start and self.program_start > start:
            start = self.program_start
        self.q("UPDATE users SET start_date=? WHERE id=?", start.isoformat(), uid)
        self.set_state(uid, "idle")
        eve = fmt(evening_minute(parse_hhmm(u["bed"])))
        await self.tg.send(uid, C.ONBOARD_DONE.format(
            date=start.strftime("%d.%m"), wake=u["wake"], eve=eve), menu=True)

    # ---------- опросы ----------
    def step(self, ctx):
        return C.FLOWS[ctx["flow"]][ctx["i"]]

    def options(self, step):
        if step.get("opts") == "DAYS":
            return ["День %d. %s" % (i + 1, d["title"]) for i, d in enumerate(C.DAYS)]
        return step.get("opts", [])

    def multi_buttons(self, step, sel):
        opts = self.options(step)
        rows = []
        for i in range(0, len(opts), 2):
            rows.append([(("✓ " if j in sel else "") + opts[j], "m:%d" % j)
                         for j in range(i, min(i + 2, len(opts)))])
        rows.append([("Готово", "m:done")])
        return rows

    async def start_flow(self, uid, flow, day, block):
        self.set_state(uid, "flow", {"flow": flow, "i": 0, "day": day, "block": block, "sel": []})
        await self.ask(uid)

    async def ask(self, uid):
        u = self.user(uid)
        ctx = u["ctx"]
        s = self.step(ctx)
        text = s["q"]
        if "{step}" in text:
            d = ctx["day"]
            text = text.format(step=C.DAYS[d - 1]["step"] if 1 <= d <= 7 else "")
        text = "<b>%s</b>" % text if "\n" not in text else text
        kind = s["kind"]
        buttons = None
        if kind == "scale":
            buttons = [[(str(n), "a:%d" % n) for n in range(0, 6)],
                       [(str(n), "a:%d" % n) for n in range(6, 11)]]
        elif kind == "choice":
            buttons = [[(o, "a:%d" % i)] for i, o in enumerate(self.options(s))]
        elif kind == "multi":
            buttons = self.multi_buttons(s, ctx.get("sel", []))
        elif kind == "text_opt":
            buttons = [[("Пропустить", "a:skip")]]
        await self.tg.send(uid, text, buttons=buttons)

    async def answer(self, uid, value):
        u = self.user(uid)
        ctx = u["ctx"]
        s = self.step(ctx)
        self.save(uid, ctx["day"], ctx["block"], s["key"], value)
        ctx["i"] += 1
        ctx["sel"] = []
        if ctx["i"] < len(C.FLOWS[ctx["flow"]]):
            self.set_state(uid, "flow", ctx)
            await self.ask(uid)
        else:
            self.set_state(uid, "idle")
            await self.finish_flow(uid, ctx)

    async def finish_flow(self, uid, ctx):
        flow, day, block = ctx["flow"], ctx["day"], ctx["block"]
        if flow == "measure" and block == "start":
            await self.tg.send(uid, C.BEFORE_INTENT)
            await self.start_flow(uid, "intent", 0, "intent")
        elif flow == "intent":
            await self.finish_onboarding(uid)
        elif flow == "morning":
            self.mark(uid, day, "morning_done")
            await self.tg.send(uid, C.MORNING_DONE, menu=True)
        elif flow == "evening":
            self.mark(uid, day, "eve_done")
            if day == 7 and not self.has(uid, 7, "final_done"):
                await self.pic(uid, "zamer_final")
                await self.tg.send(uid, C.BEFORE_FINAL_MEASURE)
                await self.start_flow(uid, "measure", 7, "final_measure")
            else:
                await self.bed_prompt(uid, day)
        elif flow == "measure" and block == "final_measure":
            await self.tg.send(uid, C.BEFORE_FINAL_Q)
            await self.start_flow(uid, "final", 7, "final")
        elif flow == "final":
            self.mark(uid, 7, "final_done")
            await self.send_summary(uid)
            await self.bed_prompt(uid, day)

    # ---------- утро ----------
    async def send_nastroika(self, uid, slot, fallback):
        m = self.media(slot)
        if m:
            await self.tg.audio(uid, m[0], m[1])
        else:
            await self.tg.send(uid, fallback)

    async def send_morning(self, uid, day):
        self.set_state(uid, "await_said", {"day": day})
        await self.tg.send(uid, C.MORNING_HELLO.format(day=day))
        await self.send_nastroika(uid, "nastroika_am", C.NASTROIKA_AM_TEXT)
        await self.tg.send(uid, C.MORNING_SAID_HINT, buttons=[[("Произнесла", "said:%d" % day)]])

    async def said(self, uid, day):
        self.mark(uid, day, "said")
        self.mark(uid, day, "offered")
        self.set_state(uid, "idle")
        await self.tg.send(uid, C.MESSAGE_WAITS, buttons=[[("Слушать послание", "open:%d" % day)]])

    async def open_message(self, uid, day):
        if not 1 <= day <= 7:
            return
        d = C.DAYS[day - 1]
        first = not self.has(uid, day, "opened")
        self.mark(uid, day, "opened")
        if not await self.pic(uid, "day%d" % day):
            await self.tg.send(uid, "День %d · %s\n\n<b>%s</b>\n\n%s\n\n<i>%s</i>" % (
                day, d["law"], d["title"], d["text"], C.MOTTO))
        m = self.media("msg%d" % day)
        if m:
            await self.tg.audio(uid, m[0], m[1])
        await self.tg.send(uid, "<b>%s.</b> %s" % (C.STEP_LABEL, d["step"]))
        if first or not self.has(uid, day, "morning_done"):
            await self.start_flow(uid, "morning", day, "morning")

    # ---------- вечер ----------
    async def send_evening(self, uid, day):
        await self.tg.send(uid, C.EVENING_HELLO.format(day=day), buttons=[[("Начать", "eve:%d" % day)]])

    async def bed_prompt(self, uid, day):
        await self.tg.send(uid, C.EVENING_DONE, buttons=[[("Я в постели", "inbed:%d" % day)]])

    async def in_bed(self, uid, day):
        await self.tg.send(uid, C.IN_BED)
        await self.send_nastroika(uid, "nastroika_pm", C.NASTROIKA_PM_TEXT)
        await self.tg.send(uid, "Когда произнесёшь, нажми кнопку.", buttons=[[("Отпустила", "rel:%d" % day)]])

    async def released(self, uid, day):
        self.mark(uid, day, "released")
        await self.tg.send(uid, C.RELEASED)
        lid = C.DAYS[day - 1]["lullaby"] if 1 <= day <= 7 else "noch"
        await self.send_lullaby(uid, lid, buttons=[[("Выбрать другую", "lulpick")]])

    async def send_lullaby(self, uid, lid, buttons=None):
        l = C.LULLABIES[lid]
        self.q("INSERT INTO plays VALUES(?,?,?)", uid, lid, self.clock().isoformat(timespec="seconds"))
        m = self.media("lul:" + lid)
        text = "<b>%s</b>\n%s" % (l["title"], l["line"])
        if not m:
            text += "\n" + C.NO_AUDIO
        await self.tg.send(uid, text, buttons=None if m else buttons)
        if m:
            await self.tg.audio(uid, m[0], m[1], buttons=buttons)

    async def lullaby_from_group(self, uid, gid):
        ids = next(g[2] for g in C.LULLABY_GROUPS if g[0] == gid)
        marks = ",".join("?" * len(ids))
        n = self.q("SELECT COUNT(*) c FROM plays WHERE user_id=? AND lid IN (%s)" % marks, uid, *ids).fetchone()["c"]
        await self.send_lullaby(uid, ids[n % len(ids)],
                                buttons=[[("Другую", "lulg:" + gid)], [("Выбрать по состоянию", "lulpick")]])

    # ---------- кнопки дня ----------
    async def menu(self, uid, text):
        u = self.user(uid)
        if text == C.BTN_CANCEL:
            self.set_state(uid, "thought1", {"resume": [u["state"], u["ctx"]]})
            await self.tg.send(uid, C.THOUGHT_ASK)
        elif text == C.BTN_SUPPORT:
            await self.tg.send(uid, C.SUPPORT_INTRO)
            await self.lullaby_from_group(uid, "support")
        elif text == C.BTN_PATH:
            await self.tg.send(uid, self.path_text(u))

    def path_text(self, u):
        uid, day = u["id"], self.day_of(u)
        if day < 1:
            return "Первый день: %s." % date.fromisoformat(u["start_date"]).strftime("%d.%m")
        lines = ["<b>День %d из 7</b>" % min(day, 7)] if day <= 7 else ["<b>Семь дней пройдены</b>"]
        for d in range(1, min(day, 7) + 1):
            am = "✓" if self.has(uid, d, "morning_done") else "·"
            pm = "✓" if self.has(uid, d, "eve_done") else "·"
            lines.append("День %d: утро %s  вечер %s" % (d, am, pm))
        n = self.q("SELECT COUNT(*) c FROM thoughts WHERE user_id=?", uid).fetchone()["c"]
        lines.append("Отменённых мыслей: %d" % n)
        return "\n".join(lines)

    async def resume(self, uid, saved):
        state, ctx = saved
        self.set_state(uid, state or "idle", ctx)
        if state == "flow":
            await self.ask(uid)

    # ---------- входящий текст ----------
    async def on_text(self, uid, text):
        u = self.user(uid)
        if not u:
            await self.tg.send(uid, "Напиши /start, чтобы начать.")
            return
        text = text.strip()
        st, ctx = u["state"], u["ctx"]
        if text in MENU_BUTTONS and u["start_date"] and st not in ("thought1", "thought2", "adm_slot"):
            await self.menu(uid, text)
            return
        if st == "name":
            self.q("UPDATE users SET name=? WHERE id=?", text[:40], uid)
            self.set_state(uid, "tz")
            await self.tg.send(uid, C.ASK_TZ.format(name=html.escape(text[:40])))
        elif st == "tz":
            m = parse_hhmm(text)
            if m is None:
                await self.tg.send(uid, C.BAD_TIME)
                return
            now = self.clock()
            diff = m - (now.hour * 60 + now.minute)
            diff = round(diff / 30) * 30
            while diff > 14 * 60:
                diff -= 1440
            while diff < -12 * 60:
                diff += 1440
            self.q("UPDATE users SET tz=? WHERE id=?", diff, uid)
            self.set_state(uid, "wake")
            await self.ask_time(uid, C.ASK_WAKE, C.WAKE_OPTIONS, "wake")
        elif st in ("wake", "bed"):
            m = parse_hhmm(text)
            if m is None:
                await self.tg.send(uid, C.BAD_TIME)
            elif st == "wake":
                await self.set_wake(uid, m)
            else:
                await self.set_bed(uid, m)
        elif st == "flow":
            if self.step(ctx)["kind"] in ("text", "text_opt"):
                await self.answer(uid, text)
            else:
                await self.tg.send(uid, "Здесь нужно нажать кнопку.")
                await self.ask(uid)
        elif st == "thought1":
            ctx["thought"] = text
            self.set_state(uid, "thought2", ctx)
            await self.tg.send(uid, C.THOUGHT_INSTEAD)
        elif st == "thought2":
            self.q("INSERT INTO thoughts VALUES(?,?,?,?)", uid,
                   self.clock().isoformat(timespec="seconds"), ctx.get("thought", ""), text)
            await self.tg.send(uid, C.THOUGHT_DONE.format(instead=html.escape(text)), menu=True)
            await self.resume(uid, ctx.get("resume", ["idle", {}]))
        elif st == "await_said":
            await self.tg.send(uid, C.MORNING_SAID_HINT,
                               buttons=[[("Произнесла", "said:%d" % ctx.get("day", 1))]])
        elif st == "consent":
            await self.tg.send(uid, "Нажми «Да» или «Нет» выше.")
        else:
            await self.tg.send(uid, C.IDLE_HINT, menu=bool(u["start_date"]))

    # ---------- кнопки ----------
    async def on_callback(self, uid, data, msg_id=None):
        u = self.user(uid)
        if not u:
            return "Напиши /start"
        st, ctx = u["state"], u["ctx"]
        head, _, arg = data.partition(":")

        if head == "consent":
            if st != "consent":
                return
            if arg == "yes":
                self.set_state(uid, "name")
                await self.tg.send(uid, C.ASK_NAME)
            else:
                self.set_state(uid, None)
                await self.tg.send(uid, C.CONSENT_NO)
        elif head == "wake" and st == "wake":
            await self.set_wake(uid, parse_hhmm(arg))
        elif head == "bed" and st == "bed":
            await self.set_bed(uid, parse_hhmm(arg))
        elif head == "a":
            if st != "flow":
                return "Этот вопрос уже закрыт"
            s = self.step(ctx)
            if s["kind"] == "scale" and arg.isdigit():
                await self.answer(uid, int(arg))
            elif s["kind"] == "choice" and arg.isdigit() and int(arg) < len(self.options(s)):
                await self.answer(uid, self.options(s)[int(arg)])
            elif s["kind"] == "text_opt" and arg == "skip":
                await self.answer(uid, "")
            else:
                return "Этот вопрос уже закрыт"
        elif head == "m":
            if st != "flow" or self.step(ctx)["kind"] != "multi":
                return "Этот вопрос уже закрыт"
            s = self.step(ctx)
            sel = ctx.get("sel", [])
            if arg == "done":
                if not sel:
                    return "Отметь хотя бы одну"
                await self.answer(uid, ", ".join(self.options(s)[i] for i in sorted(sel)))
            else:
                i = int(arg)
                sel.remove(i) if i in sel else sel.append(i)
                ctx["sel"] = sel
                self.set_state(uid, "flow", ctx)
                if msg_id is not None:
                    await self.tg.edit_buttons(uid, msg_id, self.multi_buttons(s, sel))
        elif head == "said":
            day = int(arg)
            if self.has(uid, day, "said"):
                return "Уже отмечено"
            await self.said(uid, day)
        elif head == "open":
            await self.open_message(uid, int(arg))
        elif head == "eve":
            day = int(arg)
            if self.has(uid, day, "eve_done"):
                return "Этот вечер уже записан"
            await self.pic(uid, "eve%d" % day)
            await self.start_flow(uid, "evening", day, "evening")
        elif head == "inbed":
            await self.in_bed(uid, int(arg))
        elif head == "rel":
            await self.released(uid, int(arg))
        elif head == "lulpick":
            await self.tg.send(uid, C.LULLABY_PICK,
                               buttons=[[(g[1], "lulg:" + g[0])] for g in C.LULLABY_GROUPS])
        elif head == "lulg":
            await self.lullaby_from_group(uid, arg)
        elif head == "adm" and uid in self.admins and st == "adm_slot":
            if arg != "x":
                self.q("INSERT OR REPLACE INTO media VALUES(?,?,?)", arg, ctx["file_id"], ctx["kind"])
                await self.tg.send(uid, "Сохранено: %s" % self.slot_name(arg))
            else:
                await self.tg.send(uid, "Отменено.")
            await self.resume(uid, ctx.get("resume", ["idle", {}]))

    # ---------- голос и аудио ----------
    async def on_voice(self, uid, file_id, kind):
        u = self.user(uid)
        if not u:
            return
        if u["state"] == "await_said" and kind == "voice":
            day = u["ctx"].get("day", 1)
            self.q("INSERT OR REPLACE INTO voices VALUES(?,?,?)", uid, day, file_id)
            if not self.has(uid, day, "said"):
                await self.said(uid, day)
            return
        if uid in self.admins:
            self.set_state(uid, "adm_slot", {"file_id": file_id, "kind": kind,
                                              "resume": [u["state"], u["ctx"]]})
            await self.tg.send(uid, "Куда поставить эту запись?", buttons=self.slot_buttons())
            return
        await self.tg.send(uid, "Голосовое я жду утром, после настройки.")

    def slots(self):
        s = [("nastroika_am", "Настройка, утро"), ("nastroika_pm", "Настройка, вечер")]
        s += [("msg%d" % (i + 1), "Послание, день %d" % (i + 1)) for i in range(7)]
        s += [("lul:" + k, "Колыбельная: " + v["title"]) for k, v in C.LULLABIES.items()]
        return s

    def slot_name(self, slot):
        return dict(self.slots()).get(slot, slot)

    def slot_buttons(self):
        s = self.slots()
        rows = [[(s[0][1], "adm:" + s[0][0])], [(s[1][1], "adm:" + s[1][0])]]
        msgs = s[2:9]
        rows.append([("День %d" % (i + 1), "adm:" + m[0]) for i, m in enumerate(msgs[:4])])
        rows.append([("День %d" % (i + 5), "adm:" + m[0]) for i, m in enumerate(msgs[4:])])
        rows += [[(name, "adm:" + slot)] for slot, name in s[9:]]
        rows.append([("Отмена", "adm:x")])
        return rows

    # ---------- расписание ----------
    async def tick(self):
        for r in self.q("SELECT id FROM users WHERE start_date IS NOT NULL").fetchall():
            try:
                await self.tick_user(self.user(r["id"]))
            except Exception as e:  # один сбой не должен останавливать остальных
                print("tick error", r["id"], repr(e))

    async def tick_user(self, u):
        uid, day = u["id"], self.day_of(u)
        loc = self.local_now(u)
        minute = loc.hour * 60 + loc.minute
        wake, bed = parse_hhmm(u["wake"]), parse_hhmm(u["bed"])
        if 1 <= day <= 7:
            if not self.has(uid, day, "morning") and wake <= minute < wake + 240:
                self.mark(uid, day, "morning")
                await self.send_morning(uid, day)
            offered = self.event_ts(uid, day, "offered")
            if (offered and not self.has(uid, day, "opened") and not self.has(uid, day, "reminded")
                    and self.clock() - offered >= timedelta(minutes=40)):
                self.mark(uid, day, "reminded")
                await self.tg.send(uid, C.MESSAGE_REMINDER,
                                   buttons=[[("Слушать послание", "open:%d" % day)]])
            if not self.has(uid, day, "eve") and minute >= evening_minute(bed):
                self.mark(uid, day, "eve")
                await self.send_evening(uid, day)
        elif day == 8 and not self.has(uid, 8, "after") and minute >= wake:
            self.mark(uid, 8, "after")
            await self.tg.send(uid, C.AFTER_PROGRAM, menu=True)
            m = self.media("nastroika_am")
            if m:
                await self.tg.audio(uid, m[0], m[1])

    # ---------- итог недели ----------
    def summary_data(self, uid):
        start = self.answers(uid, "start", 0)
        final = self.answers(uid, "final_measure", 7)
        scales = []
        for key, _, short in C.SCALES:
            b, a = start.get(key), final.get(key)
            scales.append((short, int(b) if b not in (None, "") else None,
                           int(a) if a not in (None, "") else None))
        belief, emotions, steps = {}, {}, 0
        for d, key, value in self.answers(uid, "evening"):
            if key == "belief" and value.isdigit():
                belief[d] = int(value)
            elif key == "emotions":
                for e in value.split(", "):
                    if e:
                        emotions[e] = emotions.get(e, 0) + 1
            elif key == "step_done" and value in ("Да", "Частично"):
                steps += 1
        return {"scales": scales, "belief": belief, "emotions": emotions, "steps": steps,
                "words_start": start.get("words", ""), "words_final": final.get("words", "")}

    async def send_summary(self, uid):
        import stats
        await self.pic(uid, "itog")
        d = self.summary_data(uid)
        await self.tg.photo(uid, stats.make_summary(d["scales"], d["belief"], d["emotions"]), C.FINAL_CAPTION)
        lines = ["<b>Твои семь дней</b>"]
        if d["words_start"] or d["words_final"]:
            lines.append("В начале: %s" % html.escape(d["words_start"] or "—"))
            lines.append("Сейчас: %s" % html.escape(d["words_final"] or "—"))
        lines.append("Шагов сделано: %d из 7" % d["steps"])
        th = self.q("SELECT thought, instead FROM thoughts WHERE user_id=? ORDER BY ts", uid).fetchall()
        if th:
            lines.append("\n<b>Мысли, которые ты отменила</b>")
            for t in th[:15]:
                lines.append("— %s → %s" % (html.escape(t["thought"]), html.escape(t["instead"])))
        fav = self.q("SELECT lid, COUNT(*) c FROM plays WHERE user_id=? GROUP BY lid ORDER BY c DESC LIMIT 1",
                     uid).fetchone()
        if fav:
            lines.append("\nТвоя колыбельная недели: «%s»" % C.LULLABIES[fav["lid"]]["title"])
        lines.append("\n" + C.MOTTO)
        await self.tg.send(uid, "\n".join(lines))
        v = self.q("SELECT file_id FROM voices WHERE user_id=? ORDER BY day LIMIT 1", uid).fetchone()
        if v:
            await self.tg.send(uid, C.VOICE_BACK)
            await self.tg.audio(uid, v["file_id"], "voice")

    # ---------- команды ведущей ----------
    async def on_command(self, uid, cmd, args):
        # Стать ведущей без доступа к серверу: /admin КОДОВОЕ_СЛОВО
        if cmd == "admin" and uid not in self.admins and self.admin_code and args[:1] == [self.admin_code]:
            self.q("INSERT OR IGNORE INTO admins VALUES(?)", uid)
            self.admins.add(uid)
        if uid not in self.admins:
            return False
        if cmd == "admin":
            await self.tg.send(uid, (
                "<b>Команды ведущей</b>\n"
                "/media — какие записи загружены\n"
                "/stats — сколько людей и как идут\n"
                "/export — выгрузка всех ответов\n"
                "/demo am 1 — прислать себе утро дня 1\n"
                "/demo pm 1 — прислать себе вечер дня 1\n"
                "/demo final — прислать себе итог недели\n"
                "/reset — стереть мои ответы и пройти заново\n"
                "/update — забрать новые тексты с GitHub и перезапуститься\n\n"
                "Чтобы загрузить запись, просто отправь мне аудио или голосовое."))
        elif cmd == "media":
            lines = ["%s %s" % ("✓" if self.media(s) else "—", n) for s, n in self.slots()]
            await self.tg.send(uid, "\n".join(lines))
        elif cmd == "stats":
            await self.tg.send(uid, self.stats_text())
        elif cmd == "export":
            a, t = self.export_csv()
            await self.tg.document(uid, a, "otvety.csv", "Все ответы")
            await self.tg.document(uid, t, "mysli.csv", "Отменённые мысли")
        elif cmd == "demo":
            u = self.user(uid)
            if not u or not u["start_date"]:
                await self.tg.send(uid, "Сначала пройди знакомство: /start")
            elif args[:1] == ["final"]:
                await self.send_summary(uid)
            elif len(args) == 2 and args[0] in ("am", "pm") and args[1].isdigit() and 1 <= int(args[1]) <= 7:
                if args[0] == "am":
                    await self.send_morning(uid, int(args[1]))
                else:
                    await self.send_evening(uid, int(args[1]))
            else:
                await self.tg.send(uid, "Пример: /demo am 1")
        elif cmd == "reset":
            for t in ("answers", "events", "thoughts", "voices", "plays"):
                self.q("DELETE FROM %s WHERE user_id=?" % t, uid)
            self.q("DELETE FROM users WHERE id=?", uid)
            await self.tg.send(uid, "Стёрто. Напиши /start.")
        else:
            return False
        return True

    def stats_text(self):
        total = self.q("SELECT COUNT(*) c FROM users").fetchone()["c"]
        ready = self.q("SELECT COUNT(*) c FROM users WHERE start_date IS NOT NULL").fetchone()["c"]
        lines = ["Нажали /start: %d" % total, "Прошли знакомство: %d" % ready, ""]
        for d in range(1, 8):
            c = {k: self.q("SELECT COUNT(*) c FROM events WHERE day=? AND key=?", d, k).fetchone()["c"]
                 for k in ("said", "opened", "eve_done", "released")}
            lines.append("День %d: настройка %d · послание %d · вечер %d · отпустили %d" % (
                d, c["said"], c["opened"], c["eve_done"], c["released"]))
        fin = self.q("SELECT COUNT(*) c FROM events WHERE day=7 AND key='final_done'").fetchone()["c"]
        lines.append("\nИтоговый замер: %d" % fin)
        return "\n".join(lines)

    def export_csv(self):
        def dump(header, rows):
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(header)
            w.writerows(rows)
            return buf.getvalue().encode("utf-8-sig")
        a = self.q("SELECT a.user_id, u.name, a.day, a.block, a.key, a.value, a.ts FROM answers a "
                   "LEFT JOIN users u ON u.id=a.user_id ORDER BY a.user_id, a.day, a.ts").fetchall()
        t = self.q("SELECT t.user_id, u.name, t.ts, t.thought, t.instead FROM thoughts t "
                   "LEFT JOIN users u ON u.id=t.user_id ORDER BY t.user_id, t.ts").fetchall()
        return (dump(["id", "имя", "день", "блок", "вопрос", "ответ", "время"], [tuple(r) for r in a]),
                dump(["id", "имя", "время", "мысль", "вместо неё"], [tuple(r) for r in t]))
