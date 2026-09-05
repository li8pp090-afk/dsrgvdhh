import os
import re
import asyncio
import sqlite3
import shutil
import difflib
import tempfile
from pathlib import Path
from collections import defaultdict

import yt_dlp
from youtubesearchpython import VideosSearch

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.enums import ChatType
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile
)
from aiogram.fsm.storage.memory import MemoryStorage

TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB = DATA_DIR / "media.db"
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "media_bot"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

rotations = defaultdict(int)
states = {}
slots = {}
slots_lock = asyncio.Lock()


def db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS media (
            cache_key TEXT PRIMARY KEY,
            file_id TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            scope TEXT PRIMARY KEY,
            mode TEXT NOT NULL
        )
    """)
    con.commit()
    return con


def get_cache(key):
    con = db()
    row = con.execute(
        "SELECT file_id FROM media WHERE cache_key=?",
        (key,)
    ).fetchone()
    con.close()
    return row[0] if row else None


def save_cache(key, file_id):
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO media(cache_key,file_id) VALUES(?,?)",
        (key, file_id)
    )
    con.commit()
    con.close()


def get_mode(scope):
    con = db()
    row = con.execute(
        "SELECT mode FROM settings WHERE scope=?",
        (scope,)
    ).fetchone()
    con.close()
    return row[0] if row else "default"


def save_mode(scope, mode):
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO settings(scope,mode) VALUES(?,?)",
        (scope, mode)
    )
    con.commit()
    con.close()


def scope_key(message):
    return f"{message.chat.id}:{message.message_thread_id or 0}"


def user_key(message):
    return f"{message.chat.id}:{message.message_thread_id or 0}:{message.from_user.id}"


def format_letters(text):
    special = set("ATFNMJULG")
    result = []

    for char in text:
        if char.isascii() and char.isalpha():
            upper = char.upper()
            result.append(upper if upper in special else upper.lower())
        else:
            result.append(char)

    return "".join(result)


def clean_name(text):
    text = format_letters(text or "")
    text = re.sub(r"[^\w\s.]", "", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_filename(publisher, title, extension):
    publisher = clean_name(publisher)
    title = clean_name(title)
    extension = extension.lstrip(".").lower()

    if publisher and title:
        name = f"{publisher} - {title}"
    else:
        name = publisher or title or "media"

    return f"{name}.{extension}"


def is_telegram_url(text):
    return bool(re.search(
        r"https?://(?:www\.)?(?:t\.me|telegram\.me)(?:/|$)",
        text or "",
        re.IGNORECASE
    ))


def extract_url(text):
    match = re.search(r"https?://\S+", text or "")

    if not match:
        return None

    return match.group(0).rstrip(".,!?)]}>\"'")


def is_youtube(url):
    return bool(re.search(
        r"(youtube\.com|youtu\.be)",
        url or "",
        re.IGNORECASE
    ))


def parse_time(value):
    value = value.strip()

    if "." in value:
        hours, rest = value.split(".", 1)

        if ":" not in rest:
            raise ValueError

        minutes, seconds = rest.split(":", 1)

        if not hours.isdigit() or not minutes.isdigit() or not seconds.isdigit():
            raise ValueError

        hours = int(hours)
        minutes = int(minutes)
        seconds = int(seconds)

        if minutes >= 60 or seconds >= 60:
            raise ValueError

        return hours * 3600 + minutes * 60 + seconds

    if ":" not in value:
        raise ValueError

    minutes, seconds = value.split(":", 1)

    if not minutes.isdigit() or not seconds.isdigit():
        raise ValueError

    minutes = int(minutes)
    seconds = int(seconds)

    if seconds >= 60:
        raise ValueError

    return minutes * 60 + seconds


def parse_range(text):
    parts = re.split(r"\s*/\s*", text.strip())

    if len(parts) != 2:
        raise ValueError

    start = parse_time(parts[0])
    end = parse_time(parts[1])

    if end <= start:
        raise ValueError

    return start, end


async def duration_of(path):
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )

    stdout, _ = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError

    return float(stdout.decode().strip())


async def ffmpeg_voice(source, output):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-vn",
        "-c:a", "libopus",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    if await process.wait() != 0:
        raise RuntimeError


async def ffmpeg_cut(source, output, start, end):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-to", str(end),
        "-i", str(source),
        "-vn",
        "-c:a", "libopus",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    if await process.wait() != 0:
        raise RuntimeError


async def download_audio(url, folder):
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True
    }

    def run():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            return info, list(folder.iterdir())

    return await asyncio.to_thread(run)


async def download_default(url, folder):
    options = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(folder / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True
    }

    def run():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            return info, list(folder.iterdir())

    return await asyncio.to_thread(run)


async def acquire_slot(key):
    async with slots_lock:
        state = slots.setdefault(
            key,
            {
                "running": 0,
                "waiting": 0,
                "queue": asyncio.Queue()
            }
        )

        if state["running"] < 3:
            state["running"] += 1
            return True

        if state["waiting"] >= 3:
            return False

        state["waiting"] += 1
        event = asyncio.Event()
        await state["queue"].put(event)

    await event.wait()

    async with slots_lock:
        state["waiting"] -= 1
        state["running"] += 1

    return True


async def release_slot(key):
    async with slots_lock:
        state = slots.get(key)

        if not state:
            return

        state["running"] = max(0, state["running"] - 1)

        if not state["queue"].empty():
            event = await state["queue"].get()
            event.set()


async def is_admin(message):
    if message.chat.type == ChatType.PRIVATE:
        return True

    member = await message.bot.get_chat_member(
        message.chat.id,
        message.from_user.id
    )

    return member.status in {"administrator", "creator"}


async def is_callback_admin(callback):
    if callback.message.chat.type == ChatType.PRIVATE:
        return True

    member = await callback.bot.get_chat_member(
        callback.message.chat.id,
        callback.from_user.id
    )

    return member.status in {"administrator", "creator"}


def settings_keyboard(mode):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="فويس",
                callback_data="mode_voice",
                style="primary" if mode == "voice" else "danger"
            ),
            InlineKeyboardButton(
                text="افتراضي",
                callback_data="mode_default",
                style="primary" if mode == "default" else "danger"
            )
        ]]
    )


@router.message(Command("ادت"))
async def settings_command(message: Message):
    if not await is_admin(message):
        return

    mode = get_mode(scope_key(message))

    await message.answer(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=settings_keyboard(mode)
    )


@router.callback_query(F.data.in_({"mode_voice", "mode_default"}))
async def settings_callback(callback: CallbackQuery):
    if not await is_callback_admin(callback):
        await callback.answer(
            "عزيزي\nليس مصرح لك بذلك",
            show_alert=True
        )
        return

    scope = f"{callback.message.chat.id}:{callback.message.message_thread_id or 0}"
    current = get_mode(scope)

    requested = "voice" if callback.data == "mode_voice" else "default"

    if requested == "default" and current == "default":
        await callback.answer(
            "زر افتراضي مُفعل\nبالفعل",
            show_alert=True
        )
        return

    save_mode(scope, requested)

    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(requested)
    )

    await callback.answer()


async def send_voice_cached(message, path, cache_key):
    cached = get_cache(cache_key)

    if cached:
        await message.answer_voice(
            cached,
            reply_to_message_id=message.message_id
        )
        return

    sent = await message.answer_voice(
        FSInputFile(path),
        reply_to_message_id=message.message_id
    )

    save_cache(cache_key, sent.voice.file_id)


async def send_file_cached(message, path, cache_key, filename):
    cached = get_cache(cache_key)

    if cached:
        await message.answer_document(
            cached,
            reply_to_message_id=message.message_id
        )
        return

    sent = await message.answer_document(
        FSInputFile(path),
        filename=filename,
        reply_to_message_id=message.message_id
    )

    save_cache(cache_key, sent.document.file_id)


async def process_youtube(message, url):
    key = f"youtube:{url.lower()}"

    cached = get_cache(key)

    if cached:
        await message.answer_voice(
            cached,
            reply_to_message_id=message.message_id
        )
        return

    slot = await acquire_slot(user_key(message))

    if not slot:
        return

    folder = Path(tempfile.mkdtemp(dir=DOWNLOAD_ROOT))

    try:
        _, files = await download_audio(url, folder)

        source = next(
            (
                x for x in files
                if x.is_file() and x.suffix.lower() != ".part"
            ),
            None
        )

        if not source:
            raise RuntimeError

        output = folder / "voice.ogg"

        await ffmpeg_voice(source, output)
        await send_voice_cached(message, output, key)

    except Exception:
        await message.answer(
            "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا",
            reply_to_message_id=message.message_id
        )

    finally:
        shutil.rmtree(folder, ignore_errors=True)
        await release_slot(user_key(message))


async def process_url(message, url):
    if is_telegram_url(url):
        return

    mode = get_mode(scope_key(message))
    key = f"url:{mode}:{url}"

    cached = get_cache(key)

    if cached:
        if mode == "voice":
            await message.answer_voice(
                cached,
                reply_to_message_id=message.message_id
            )
        else:
            await message.answer_document(
                cached,
                reply_to_message_id=message.message_id
            )
        return

    slot = await acquire_slot(user_key(message))

    if not slot:
        return

    start = await message.answer(
        "ههع شم كسي\nيلا",
        reply_to_message_id=message.message_id
    )

    folder = Path(tempfile.mkdtemp(dir=DOWNLOAD_ROOT))

    try:
        if mode == "voice":
            info, files = await download_audio(url, folder)

            source = next(
                (
                    x for x in files
                    if x.is_file() and x.suffix.lower() != ".part"
                ),
                None
            )

            if not source:
                raise RuntimeError

            output = folder / "voice.ogg"

            await ffmpeg_voice(source, output)
            await send_voice_cached(message, output, key)

        else:
            info, files = await download_default(url, folder)

            source = next(
                (
                    x for x in files
                    if x.is_file()
                    and x.suffix.lower() not in {".part", ".ytdl"}
                ),
                None
            )

            if not source:
                raise RuntimeError

            publisher = (
                info.get("channel")
                or info.get("uploader")
                or info.get("creator")
                or ""
            )

            title = info.get("title") or ""
            extension = source.suffix.lstrip(".").lower()

            filename = make_filename(
                publisher,
                title,
                extension
            )

            output = folder / filename

            if source != output:
                source.rename(output)

            await send_file_cached(
                message,
                output,
                key,
                filename
            )

        await start.delete()

    except Exception:
        try:
            await start.delete()
        except Exception:
            pass

        await message.answer(
            "الرابط غير مدعوم او الموقع مو راضي يتعاون\nشم طيزي يلا",
            reply_to_message_id=message.message_id
        )

    finally:
        shutil.rmtree(folder, ignore_errors=True)
        await release_slot(user_key(message))


@router.message(F.text.startswith("يوت "))
async def youtube_search(message: Message):
    query = message.text[4:].strip()

    if not query:
        return

    formatted = format_letters(query)

    await message.answer(
        f"يوت هوف\nها تريد {formatted}\nتمام عبي",
        reply_to_message_id=message.message_id
    )

    try:
        result = await asyncio.to_thread(
            lambda: VideosSearch(
                query.lower(),
                limit=3
            ).result()
        )

        videos = result.get("result", [])

        if not videos:
            raise RuntimeError

        best = max(
            videos,
            key=lambda video: difflib.SequenceMatcher(
                None,
                query.lower(),
                video.get("title", "").lower()
            ).ratio()
        )

        url = best.get("link")

        if not url:
            raise RuntimeError

        await process_youtube(message, url)

    except Exception:
        await message.answer(
            "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا",
            reply_to_message_id=message.message_id
        )


@router.message(F.video | F.audio)
async def direct_media(message: Message):
    media = message.video or message.audio
    key = f"direct_voice:{media.file_unique_id}"

    cached = get_cache(key)

    if cached:
        await message.answer_voice(
            cached,
            reply_to_message_id=message.message_id
        )
        return

    slot = await acquire_slot(user_key(message))

    if not slot:
        return

    folder = Path(tempfile.mkdtemp(dir=DOWNLOAD_ROOT))

    try:
        source = folder / "source"
        output = folder / "voice.ogg"

        await message.bot.download(
            media,
            destination=source
        )

        await ffmpeg_voice(source, output)
        await send_voice_cached(message, output, key)

    finally:
        shutil.rmtree(folder, ignore_errors=True)
        await release_slot(user_key(message))


@router.message(F.reply_to_message, F.text == "تعديل")
async def start_edit(message: Message):
    replied = message.reply_to_message

    if not replied.voice:
        return

    key = f"direct_voice:{replied.voice.file_unique_id}"
    cached = get_cache(key)

    if not cached:
        return

    states[user_key(message)] = {
        "file_id": cached,
        "cache_key": key
    }

    await message.answer(
        "تستطيع تعديل مدة الصوتيات هكذا\n\n"
        "12:45 / 18:36 وللساعات 12.30:48",
        reply_to_message_id=message.message_id
    )


@router.message(F.text)
async def edit_duration(message: Message):
    key = user_key(message)

    if key not in states:
        return

    state = states[key]

    try:
        start, end = parse_range(message.text)
    except Exception:
        states.pop(key, None)

        await message.answer(
            "تم انهاء وضع تعديل مدة الفويس\nتنسيق غير صالح",
            reply_to_message_id=message.message_id
        )
        return

    slot = await acquire_slot(key)

    if not slot:
        return

    folder = Path(tempfile.mkdtemp(dir=DOWNLOAD_ROOT))

    try:
        source = folder / "source.ogg"
        output = folder / "edited.ogg"

        await message.bot.download(
            state["file_id"],
            destination=source
        )

        duration = await duration_of(source)

        if end > duration:
            await message.answer(
                "مدة هذه الصوتيه اصغر من المدة اللتي\n"
                "ارسلتها",
                reply_to_message_id=message.message_id
            )
            return

        await ffmpeg_cut(
            source,
            output,
            start,
            end
        )

        await message.answer_voice(
            FSInputFile(output),
            reply_to_message_id=message.message_id
        )

        states.pop(key, None)

    except Exception:
        states.pop(key, None)

    finally:
        shutil.rmtree(folder, ignore_errors=True)
        await release_slot(key)


@router.message(F.text)
async def text_handler(message: Message):
    text = message.text.strip()

    url = extract_url(text)

    if url:
        if is_telegram_url(url):
            return

        if is_youtube(url):
            await process_youtube(message, url)
        else:
            await process_url(message, url)

        return

    if message.chat.type != ChatType.PRIVATE and text != "بوت":
        return

    key = user_key(message)

    replies = [
        "اهلين وسهلين\nاستاذ/ة",
        "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
        "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
        "راح انزع وتنيكني بدال هذا\nالنيج شو داضوج"
    ]

    index = rotations[key] % len(replies)
    rotations[key] += 1

    await message.answer(
        replies[index],
        reply_to_message_id=message.message_id
    )


async def main():
    bot = Bot(TOKEN)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())