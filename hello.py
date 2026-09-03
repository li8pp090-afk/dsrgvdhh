import asyncio
import os
import re
import random
import aiosqlite
from pathlib import Path
from difflib import SequenceMatcher

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage

import yt_dlp
from youtube_search import YoutubeSearch

BOT_TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = "bot_data.db"

MAX_ACTIVE_DOWNLOADS = 3
MAX_QUEUED_DOWNLOADS = 3
TOTAL_MAX_TASKS = MAX_ACTIVE_DOWNLOADS + MAX_QUEUED_DOWNLOADS

PATTERN_YOUTUBE = re.compile(r"(?:youtu\.be/|youtube\.com/)")
PATTERN_URL = re.compile(r"https?://(?!t\.me/|telegram\.me/)[^\s]+")
PATTERN_TELEGRAM = re.compile(r"https?://(?:t\.me|telegram\.me)/")

file_id_cache = {"default": set(), "voice": set(), "yut": set()}
trim_sessions = {}
active_tasks = set()
semaphore = asyncio.Semaphore(MAX_ACTIVE_DOWNLOADS)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'default'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_counters (
                user_id INTEGER PRIMARY KEY,
                reply_index INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.commit()

async def get_chat_mode(chat_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT mode FROM chat_settings WHERE chat_id = ?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else "default"

async def set_chat_mode(chat_id: int, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO chat_settings VALUES (?, ?)", (chat_id, mode))
        await db.commit()

async def get_user_index(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT reply_index FROM user_counters WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def next_user_index(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        idx = await get_user_index(user_id)
        new_idx = (idx + 1) % 4
        await db.execute("INSERT OR REPLACE INTO user_counters VALUES (?, ?)", (user_id, new_idx))
        await db.commit()
        return idx

def format_case(text: str) -> str:
    text = text.lower()
    for c in "atfnmjulg":
        text = text.replace(c, c.upper())
    return text

def sanitize_filename(channel, title):
    def clean_part(s):
        s = re.sub(r'[\\/*?:"<>|]', '', s)
        s = re.sub(r'[^\w\s-]', '', s)
        return s.strip()
    return format_case(f"{clean_part(channel)} - {clean_part(title)}")

async def search_youtube(query: str):
    try:
        results = YoutubeSearch(query, max_results=3).to_dict()
        if not results:
            return None
        q_low = query.lower()
        best = max(results, key=lambda r: SequenceMatcher(None, q_low, r.get("title", "").lower()).ratio())
        return best
    except Exception:
        return None

async def get_chat_owner_id(bot: Bot, chat_id: int):
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.status == "creator":
                return admin.user.id
    except Exception:
        pass
    return None

def get_settings_keyboard(mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(
        text="فويس",
        callback_data="setmode:voice",
        color="primary" if mode == "voice" else "danger"
    )
    kb.button(
        text="افتراضي",
        callback_data="setmode:default",
        color="primary" if mode == "default" else "danger"
    )
    kb.adjust(2)
    return kb.as_markup()

async def download_default(url: str):
    opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(uploader)s - %(title)s.%(ext)s"),
        "ffmpeg_location": "ffmpeg",
        "quiet": True,
        "no_warnings": True,
    }
    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(None, ydl.extract_info, url, download=True)
            return info
    except Exception:
        return None

async def convert_to_ogg(src_file: Path, dst_name: str):
    out_file = DOWNLOAD_DIR / f"{dst_name}.ogg"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(src_file), "-vn", "-c:a", "libopus", "-y", str(out_file),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    src_file.unlink(missing_ok=True)
    return out_file if out_file.exists() else None

async def download_voice(url: str):
    temp_base = DOWNLOAD_DIR / "audio_tmp"
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(temp_base),
        "quiet": True,
        "no_warnings": True,
    }
    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(None, ydl.extract_info, url, download=True)
            if not info:
                return None
        src_file = temp_base.with_suffix("." + info.get("ext"))
        safe_name = sanitize_filename(info.get("uploader"), info.get("title"))
        return await convert_to_ogg(src_file, safe_name)
    except Exception:
        for f in DOWNLOAD_DIR.glob("audio_tmp*"):
            f.unlink(missing_ok=True)
        return None

def parse_duration_str(s: str):
    s = s.strip().replace(" ", "")
    try:
        if ":" not in s:
            return float(s)
        if "." in s:
            h_part, rest = s.split(".", 1)
            m, sec = rest.split(":")
            return int(h_part)*3600 + int(m)*60 + float(sec)
        m, sec = s.split(":")
        return int(m)*60 + float(sec)
    except Exception:
        return None

async def trim_audio_file(src: Path, dst: Path, start, end):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end-start:.3f}",
        "-c", "copy", "-y", str(dst), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    return await proc.wait() == 0

reply_list = [
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
    "راح انزع وتنيكني بدال هذا\nالنيج شو داضوج"
]
success_default = [
    "هلو باي\nاه",
    "باي هلو\nاه",
    "اه باي\nهلو",
    "اه هلو\nباي"
]

@dp.message(CommandStart())
async def cmd_start(message: Message):
    pass

@dp.message(F.text == "ادت")
async def settings_panel(message: Message, bot: Bot):
    chat_id = message.chat.id
    if chat_id > 0:
        owner_id = message.chat.id
    else:
        owner_id = await get_chat_owner_id(bot, chat_id)
    if not owner_id or message.from_user.id != owner_id:
        return
    mode = await get_chat_mode(chat_id)
    await message.answer("تستطيع تغيير وضع عمل البوت\nمن هنا", reply_markup=get_settings_keyboard(mode))

@dp.callback_query(F.data.startswith("setmode:"))
async def change_mode_cb(cq: CallbackQuery, bot: Bot):
    _, target_mode = cq.data.split(":")
    chat_id = cq.message.chat.id
    if chat_id > 0:
        owner_id = chat_id
    else:
        owner_id = await get_chat_owner_id(bot, chat_id)
    if cq.from_user.id != owner_id:
        await cq.answer("عزيزي\nليس مصرح لك بذلك", show_alert=True)
        return

    current_mode = await get_chat_mode(chat_id)

    if target_mode == "default":
        if current_mode == "default":
            await cq.answer("زر افتراضي مُفعل\nبالفعل", show_alert=True)
            return
        await set_chat_mode(chat_id, "default")
        await cq.message.edit_reply_markup(reply_markup=get_settings_keyboard("default"))
        await cq.message.answer(random.choice(success_default))
        await cq.answer()

    elif target_mode == "voice":
        new_mode = "default" if current_mode == "voice" else "voice"
        await set_chat_mode(chat_id, new_mode)
        await cq.message.edit_reply_markup(reply_markup=get_settings_keyboard(new_mode))
        if new_mode == "default":
            await cq.answer()
        else:
            await cq.answer()

@dp.message()
async def router(message: Message, bot: Bot):
    chat_id = message.chat.id
    text = message.text.strip() if message.text else ""
    uid = message.from_user.id if message.from_user else None
    is_private = chat_id > 0

    if chat_id in trim_sessions and uid == trim_sessions[chat_id].get("uid"):
        session = trim_sessions[chat_id]
        parts = text.split("/")
        if len(parts) != 2:
            trim_sessions.pop(chat_id, None)
            await message.reply("تم انهاء وضع تعديل مدة الفويس\nتنسيق غير صالح")
            return
        t1 = parse_duration_str(parts[0])
        t2 = parse_duration_str(parts[1])
        if t1 is None or t2 is None or t2 <= t1:
            trim_sessions.pop(chat_id, None)
            await message.reply("تم انهاء وضع تعديل مدة الفويس\nتنسيق غير صالح")
            return
        if t2 > session["duration"]:
            await message.reply("مدة هذه الصوتيه اصغر من المدة اللتي\nارسلتها")
            return
        out_path = DOWNLOAD_DIR / f"trimmed_{chat_id}.ogg"
        ok = await trim_audio_file(session["path"], out_path, t1, t2)
        if ok:
            await bot.send_voice(chat_id, FSInputFile(out_path))
        out_path.unlink(missing_ok=True)
        trim_sessions.pop(chat_id, None)
        return

    if text.lower().startswith("يوت "):
        query = text[4:].strip()
        if not query:
            return
        res = await search_youtube(query)
        if not res:
            await message.reply("الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا")
            return
        vid_url = "https://youtu.be/" + res["id"]
        disp_title = format_case(res["title"])
        starter = await message.reply(f"ها تريد {disp_title}\nتمام عبي")
        if len(active_tasks) >= TOTAL_MAX_TASKS:
            return
        asyncio.create_task(process_yut(message, bot, vid_url, starter))
        return

    url_match = PATTERN_URL.search(text)
    if url_match and not PATTERN_TELEGRAM.search(text):
        url = url_match.group()
        if len(active_tasks) >= TOTAL_MAX_TASKS:
            return
        mode = await get_chat_mode(chat_id)
        starter = await message.reply("ههع شم كسي\nيلا")
        asyncio.create_task(process_download(message, bot, url, starter, mode))
        return

    if text == "تعديل" and message.reply_to_message and message.reply_to_message.voice:
        vid = message.reply_to_message.voice
        fid = vid.file_id
        dur = vid.duration
        trim_sessions[chat_id] = {"uid": uid, "file_id": fid, "duration": dur, "path": DOWNLOAD_DIR}
        await message.reply("تستطيع تعديل مدة الصوتيات هكذا\n\n12:45 / 18:36 وللساعات 12.30:48")
        return

    should_respond = False
    if is_private and uid:
        should_respond = True
    elif not is_private and text == "بوت" and uid:
        should_respond = True

    if should_respond and uid:
        idx = await next_user_index(uid)
        await message.reply(reply_list[idx])

async def process_download(msg: Message, bot: Bot, url: str, starter: Message, mode: str):
    chat_id = msg.chat.id
    active_tasks.add(chat_id)
    try:
        async with semaphore:
            fid = url
            if fid in file_id_cache.get(mode, set()):
                await starter.delete()
                return
            if mode == "voice":
                out_file = await download_voice(url)
                if out_file:
                    await bot.send_voice(chat_id, FSInputFile(out_file))
                    file_id_cache[mode].add(fid)
                    out_file.unlink(missing_ok=True)
            else:
                info = await download_default(url)
                if info:
                    fn = sanitize_filename(info.get("uploader"), info.get("title")) + "." + info.get("ext")
                    fp = DOWNLOAD_DIR / fn
                    if fp.exists():
                        await bot.send_document(chat_id, FSInputFile(fp))
                        file_id_cache[mode].add(fid)
                        fp.unlink(missing_ok=True)
            success = bool(out_file) or bool(info)
            if not success:
                await starter.edit_text("الرابط غير مدعوم او الموقع مو راضي يتعاون\nشم طيزي يلا")
                return
            await starter.delete()
    finally:
        active_tasks.discard(chat_id)

async def process_yut(msg: Message, bot: Bot, url: str, starter: Message):
    chat_id = msg.chat.id
    task_key = f"yut_{chat_id}"
    active_tasks.add(task_key)
    try:
        async with semaphore:
            fid = url
            if fid in file_id_cache.get("yut", set()):
                await starter.delete()
                return
            out_file = await download_voice(url)
            if not out_file:
                await starter.edit_text("الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا")
                return
            await bot.send_voice(chat_id, FSInputFile(out_file))
            file_id_cache["yut"].add(fid)
            out_file.unlink(missing_ok=True)
            await starter.delete()
    finally:
        active_tasks.discard(task_key)

async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
