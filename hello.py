import os
import re
import asyncio
import sqlite3
import shutil
import difflib
import tempfile
import subprocess
from pathlib import Path
from collections import defaultdict

import yt_dlp
from youtubesearchpython import VideosSearch

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.enums import ChatType
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.fsm.storage.memory import MemoryStorage


TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


DB = "media.db"
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "media_bot"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

db_lock = asyncio.Lock()
slots_lock = asyncio.Lock()

states = {}
rotations = defaultdict(int)
slots = {}


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


def _get_cache(key):
    con = db()

    try:
        row = con.execute(
            "SELECT file_id FROM media WHERE cache_key=?",
            (key,)
        ).fetchone()

        return row[0] if row else None
    finally:
        con.close()


async def get_cache(key):
    async with db_lock:
        return await asyncio.to_thread(_get_cache, key)


def _save_cache(key, file_id):
    con = db()

    try:
        con.execute(
            """
            INSERT OR REPLACE INTO media(cache_key, file_id)
            VALUES(?, ?)
            """,
            (key, file_id)
        )

        con.commit()
    finally:
        con.close()


async def save_cache(key, file_id):
    async with db_lock:
        await asyncio.to_thread(_save_cache, key, file_id)


def _get_mode(scope):
    con = db()

    try:
        row = con.execute(
            "SELECT mode FROM settings WHERE scope=?",
            (scope,)
        ).fetchone()

        return row[0] if row else "default"
    finally:
        con.close()


async def get_mode(scope):
    async with db_lock:
        return await asyncio.to_thread(_get_mode, scope)


def _save_mode(scope, mode):
    con = db()

    try:
        con.execute(
            """
            INSERT OR REPLACE INTO settings(scope, mode)
            VALUES(?, ?)
            """,
            (scope, mode)
        )

        con.commit()
    finally:
        con.close()


async def save_mode(scope, mode):
    async with db_lock:
        await asyncio.to_thread(_save_mode, scope, mode)


def scope_key(message):
    topic = message.message_thread_id or 0
    return f"{message.chat.id}:{topic}"


def user_key(message):
    topic = message.message_thread_id or 0
    return f"{message.chat.id}:{topic}:{message.from_user.id}"


def format_letters(text):
    special = set("ATFNMJULG")
    result = []

    for char in text:
        if char.isascii() and char.isalpha():
            upper = char.upper()
            result.append(
                upper if upper in special else upper.lower()
            )
        else:
            result.append(char)

    return "".join(result)


def clean_name(text):
    text = format_letters(text or "")
    text = re.sub(
        r"[^\w\s.]",
        "",
        text,
        flags=re.UNICODE
    )
    text = re.sub(r"_+", "_", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text[:180]


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
    return bool(
        re.search(
            r"https?://(?:www\.)?(?:t\.me|telegram\.me)(?:/|$)",
            text or "",
            re.IGNORECASE
        )
    )


def extract_url(text):
    match = re.search(
        r"https?://\S+",
        text or ""
    )

    if not match:
        return None

    return match.group(0).rstrip(
        ".,!?)]}>\"'"
    )


def is_youtube(url):
    return bool(
        re.search(
            r"(youtube\.com|youtu\.be)",
            url or "",
            re.IGNORECASE
        )
    )


def parse_time(value):
    value = value.strip()

    if "." in value:
        hour_part, rest = value.split(".", 1)

        if ":" not in rest:
            raise ValueError

        minute_part, second_part = rest.split(":", 1)

        if not (
            hour_part.isdigit()
            and minute_part.isdigit()
            and second_part.isdigit()
        ):
            raise ValueError

        hours = int(hour_part)
        minutes = int(minute_part)
        seconds = int(second_part)

        if minutes >= 60 or seconds >= 60:
            raise ValueError

        return hours * 3600 + minutes * 60 + seconds

    if ":" not in value:
        raise ValueError

    minute_part, second_part = value.split(":", 1)

    if not (
        minute_part.isdigit()
        and second_part.isdigit()
    ):
        raise ValueError

    minutes = int(minute_part)
    seconds = int(second_part)

    if seconds >= 60:
        raise ValueError

    return minutes * 60 + seconds


def parse_range(text):
    parts = re.split(
        r"\s*/\s*",
        text.strip()
    )

    if len(parts) != 2:
        raise ValueError

    start = parse_time(parts[0])
    end = parse_time(parts[1])

    if end <= start:
        raise ValueError

    return start, end


def duration_of(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError

    output = result.stdout.strip()

    if not output:
        raise RuntimeError

    return float(output)


async def to_voice(source, output):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libopus",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    if await process.wait() != 0:
        raise RuntimeError


async def cut_voice(source, output, start, end):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-to",
        str(end),
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libopus",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    if await process.wait() != 0:
        raise RuntimeError


async def download_audio(url, folder):
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    def run():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )
            return info, list(folder.iterdir())

    return await asyncio.to_thread(run)


async def download_default(url, folder):
    options = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(folder / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    def run():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )
            return info, list(folder.iterdir())

    return await asyncio.to_thread(run)


def find_media_file(folder):
    ignored = {
        ".part",
        ".ytdl",
        ".tmp"
    }

    files = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() not in ignored
    ]

    if not files:
        return None

    return max(
        files,
        key=lambda path: path.stat().st_size
    )


async def acquire_slot(key):
    async with slots_lock:
        state = slots.setdefault(
            key,
            {
                "running": 0,
                "waiting": 0,
                "event": asyncio.Event(),
            }
        )

        if state["running"] < 3:
            state["running"] += 1
            return True

        if state["waiting"] >= 3:
            return False

        state["waiting"] += 1
        event = state["event"]

    try:
        while True:
            await event.wait()

            async with slots_lock:
                state = slots.get(key)

                if not state:
                    return False

                if state["running"] < 3:
                    state["running"] += 1

                    if state["waiting"] > 0:
                        state["waiting"] -= 1

                    if state["waiting"] == 0:
                        event.clear()

                    return True

    except asyncio.CancelledError:
        async with slots_lock:
            state = slots.get(key)

            if state and state["waiting"] > 0:
                state["waiting"] -= 1

                if state["waiting"] == 0:
                    state["event"].clear()

        raise


async def release_slot(key):
    async with slots_lock:
        state = slots.get(key)

        if not state:
            return

        if state["running"] > 0:
            state["running"] -= 1

        if state["waiting"] > 0:
            state["event"].set()

        if state["running"] == 0 and state["waiting"] == 0:
            slots.pop(key, None)


async def is_admin(message):
    if message.chat.type == ChatType.PRIVATE:
        return True

    member = await message.bot.get_chat_member(
        message.chat.id,
        message.from_user.id
    )

    return member.status in {
        "administrator",
        "creator"
    }


async def is_callback_admin(callback):
    if callback.message.chat.type == ChatType.PRIVATE:
        return True

    member = await callback.bot.get_chat_member(
        callback.message.chat.id,
        callback.from_user.id
    )

    return member.status in {
        "administrator",
        "creator"
    }


def settings_keyboard(mode):
    voice_style = (
        "primary"
        if mode == "voice"
        else "danger"
    )

    default_style = (
        "primary"
        if mode == "default"
        else "danger"
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="فويس",
                    callback_data="mode_voice",
                    style=voice_style
                ),
                InlineKeyboardButton(
                    text="افتراضي",
                    callback_data="mode_default",
                    style=default_style
                )
            ]
        ]
    )


@router.message(Command("ادت"))
async def settings_command(message: Message):
    if not await is_admin(message):
        return

    mode = await get_mode(
        scope_key(message)
    )

    await message.answer(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=settings_keyboard(mode)
    )


@router.callback_query(
    F.data.in_({
        "mode_voice",
        "mode_default"
    })
)
async def settings_callback(
    callback: CallbackQuery
):
    if not await is_callback_admin(callback):
        await callback.answer(
            "عزيزي\nليس مصرح لك بذلك",
            show_alert=True
        )
        return

    scope = (
        f"{callback.message.chat.id}:"
        f"{callback.message.message_thread_id or 0}"
    )

    current = await get_mode(scope)

    requested = (
        "voice"
        if callback.data == "mode_voice"
        else "default"
    )

    if (
        requested == "default"
        and current == "default"
    ):
        await callback.answer(
            "زر افتراضي مُفعل\nبالفعل",
            show_alert=True
        )
        return

    await save_mode(
        scope,
        requested
    )

    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(requested)
    )

    await callback.answer()


async def send_voice_cached(
    message,
    path,
    cache_key
):
    cached = await get_cache(cache_key)

    if cached:
        sent = await message.answer_voice(
            cached,
            reply_to_message_id=message.message_id
        )

        if sent.voice:
            await save_cache(
                f"voice:{sent.voice.file_unique_id}",
                cached
            )

        return cached

    sent = await message.answer_voice(
        FSInputFile(path),
        reply_to_message_id=message.message_id
    )

    if not sent.voice:
        raise RuntimeError

    file_id = sent.voice.file_id

    await save_cache(
        cache_key,
        file_id
    )

    await save_cache(
        f"voice:{sent.voice.file_unique_id}",
        file_id
    )

    return file_id


async def send_file_cached(
    message,
    path,
    cache_key,
    filename
):
    cached = await get_cache(cache_key)

    if cached:
        await message.answer_document(
            cached,
            reply_to_message_id=message.message_id
        )
        return cached

    sent = await message.answer_document(
        FSInputFile(
            path,
            filename=filename
        ),
        reply_to_message_id=message.message_id
    )

    if not sent.document:
        raise RuntimeError

    file_id = sent.document.file_id

    await save_cache(
        cache_key,
        file_id
    )

    return file_id


async def process_youtube(message, url):
    key = f"youtube:{url.lower()}"

    cached = await get_cache(key)

    if cached:
        await message.answer_voice(
            cached,
            reply_to_message_id=message.message_id
        )
        return

    slot_key = user_key(message)

    if not await acquire_slot(slot_key):
        return

    folder = Path(
        tempfile.mkdtemp(
            dir=DOWNLOAD_ROOT
        )
    )

    try:
        _, files = await download_audio(
            url,
            folder
        )

        media = find_media_file(folder)

        if not media:
            raise RuntimeError

        output = folder / "voice.ogg"

        await to_voice(
            media,
            output
        )

        await send_voice_cached(
            message,
            output,
            key
        )

    except Exception:
        await message.answer(
            "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\n"
            "شم طيزي يلا",
            reply_to_message_id=message.message_id
        )

    finally:
        shutil.rmtree(
            folder,
            ignore_errors=True
        )

        await release_slot(slot_key)


async def process_url(message, url):
    if is_telegram_url(url):
        return

    mode = await get_mode(
        scope_key(message)
    )

    key = f"url:{mode}:{url}"

    cached = await get_cache(key)

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

    slot_key = user_key(message)

    if not await acquire_slot(slot_key):
        return

    start_message = await message.answer(
        "ههع شم كسي\nيلا",
        reply_to_message_id=message.message_id
    )

    folder = Path(
        tempfile.mkdtemp(
            dir=DOWNLOAD_ROOT
        )
    )

    try:
        if mode == "voice":
            info, files = await download_audio(
                url,
                folder
            )

            source = find_media_file(folder)

            if not source:
                raise RuntimeError

            output = folder / "voice.ogg"

            await to_voice(
                source,
                output
            )

            await send_voice_cached(
                message,
                output,
                key
            )

        else:
            info, files = await download_default(
                url,
                folder
            )

            source = find_media_file(folder)

            if not source:
                raise RuntimeError

            publisher = (
                info.get("channel")
                or info.get("uploader")
                or info.get("creator")
                or ""
            )

            title = info.get("title") or ""

            extension = (
                source.suffix
                .lstrip(".")
                .lower()
            )

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

        try:
            await start_message.delete()
        except Exception:
            pass

    except Exception:
        try:
            await start_message.delete()
        except Exception:
            pass

        await message.answer(
            "الرابط غير مدعوم او الموقع مو راضي يتعاون\n"
            "شم طيزي يلا",
            reply_to_message_id=message.message_id
        )

    finally:
        shutil.rmtree(
            folder,
            ignore_errors=True
        )

        await release_slot(slot_key)


@router.message(
    F.reply_to_message,
    F.text
)
async def voice_edit(message: Message):
    if message.text.strip() != "تعديل":
        return

    replied = message.reply_to_message

    if not replied.voice:
        return

    key = f"voice:{replied.voice.file_unique_id}"

    file_id = await get_cache(key)

    if not file_id:
        return

    state_key = user_key(message)

    states[state_key] = {
        "voice_key": key,
        "file_id": file_id
    }

    await message.answer(
        "تستطيع تعديل مدة الصوتيات هكذا\n\n"
        "12:45 / 18:36 وللساعات 12.30:48",
        reply_to_message_id=message.message_id
    )


@router.message(F.video | F.audio)
async def direct_media(message: Message):
    media = message.video or message.audio

    if not media:
        return

    key = (
        f"direct_voice:"
        f"{media.file_unique_id}"
    )

    cached = await get_cache(key)

    if cached:
        sent = await message.answer_voice(
            cached,
            reply_to_message_id=message.message_id
        )

        if sent.voice:
            await save_cache(
                f"voice:{sent.voice.file_unique_id}",
                cached
            )

        return

    slot_key = user_key(message)

    if not await acquire_slot(slot_key):
        return

    folder = Path(
        tempfile.mkdtemp(
            dir=DOWNLOAD_ROOT
        )
    )

    try:
        source = folder / "source"

        await message.bot.download(
            media,
            destination=source
        )

        output = folder / "voice.ogg"

        await to_voice(
            source,
            output
        )

        await send_voice_cached(
            message,
            output,
            key
        )

    except Exception:
        await message.answer(
            "الرابط غير مدعوم او الموقع مو راضي يتعاون",
            reply_to_message_id=message.message_id
        )

    finally:
        shutil.rmtree(
            folder,
            ignore_errors=True
        )

        await release_slot(slot_key)


@router.message(F.voice)
async def voice_received(message: Message):
    key = (
        f"direct_voice:"
        f"{message.voice.file_unique_id}"
    )

    cached = await get_cache(key)

    if cached:
        sent = await message.answer_voice(
            cached,
            reply_to_message_id=message.message_id
        )

        if sent.voice:
            await save_cache(
                f"voice:{sent.voice.file_unique_id}",
                cached
            )

        return

    slot_key = user_key(message)

    if not await acquire_slot(slot_key):
        return

    folder = Path(
        tempfile.mkdtemp(
            dir=DOWNLOAD_ROOT
        )
    )

    try:
        source = folder / "source.ogg"

        await message.bot.download(
            message.voice,
            destination=source
        )

        await send_voice_cached(
            message,
            source,
            key
        )

    except Exception:
        await message.answer(
            "الرابط غير مدعوم او الموقع مو راضي يتعاون",
            reply_to_message_id=message.message_id
        )

    finally:
        shutil.rmtree(
            folder,
            ignore_errors=True
        )

        await release_slot(slot_key)


@router.message(F.text)
async def text_handler(message: Message):
    state_key = user_key(message)
    text = message.text.strip()

    if state_key in states:
        state = states[state_key]

        try:
            start, end = parse_range(text)
        except Exception:
            states.pop(state_key, None)

            await message.answer(
                "تم انهاء وضع تعديل مدة الفويس\n"
                "تنسيق غير صالح",
                reply_to_message_id=message.message_id
            )
            return

        if not await acquire_slot(state_key):
            return

        folder = Path(
            tempfile.mkdtemp(
                dir=DOWNLOAD_ROOT
            )
        )

        try:
            source = folder / "source.ogg"
            output = folder / "edited.ogg"

            await message.bot.download(
                state["file_id"],
                destination=source
            )

            duration = duration_of(source)

            if start >= duration or end > duration:
                states.pop(state_key, None)

                await message.answer(
                    "مدة هذه الصوتيه اصغر من المدة اللتي\n"
                    "ارسلتها",
                    reply_to_message_id=message.message_id
                )
                return

            await cut_voice(
                source,
                output,
                start,
                end
            )

            sent = await message.answer_voice(
                FSInputFile(output),
                reply_to_message_id=message.message_id
            )

            if sent.voice:
                await save_cache(
                    f"voice:{sent.voice.file_unique_id}",
                    sent.voice.file_id
                )

            states.pop(state_key, None)

        except Exception:
            states.pop(state_key, None)

        finally:
            shutil.rmtree(
                folder,
                ignore_errors=True
            )

            await release_slot(state_key)

        return

    url = extract_url(text)

    if url:
        if is_telegram_url(url):
            return

        if is_youtube(url):
            await process_youtube(
                message,
                url
            )
        else:
            await process_url(
                message,
                url
            )

        return

    if (
        message.chat.type != ChatType.PRIVATE
        and text != "بوت"
    ):
        return

    replies = [
        "اهلين وسهلين\nاستاذ/ة",
        "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
        "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
        "راح انزع وتنيكني بدال هذا\nالنيج شو داضوج"
    ]

    index = (
        rotations[state_key]
        % len(replies)
    )

    rotations[state_key] += 1

    await message.answer(
        replies[index],
        reply_to_message_id=message.message_id
    )


@router.message(F.text.startswith("يوت "))
async def youtube_search(message: Message):
    query = message.text[4:].strip()

    if not query:
        return

    formatted = format_letters(query)

    start_message = await message.answer(
        f"يوت هوف\n"
        f"ها تريد {formatted}\n"
        f"تمام عبي",
        reply_to_message_id=message.message_id
    )

    try:
        search = await asyncio.to_thread(
            lambda: VideosSearch(
                query.lower(),
                limit=3
            ).result()
        )

        results = search.get(
            "result",
            []
        )

        if not results:
            raise RuntimeError

        best = max(
            results,
            key=lambda item: difflib.SequenceMatcher(
                None,
                query.lower(),
                item.get(
                    "title",
                    ""
                ).lower()
            ).ratio()
        )

        url = best.get("link")

        if not url:
            raise RuntimeError

        await process_youtube(
            message,
            url
        )

        try:
            await start_message.delete()
        except Exception:
            pass

    except Exception:
        try:
            await start_message.delete()
        except Exception:
            pass

        await message.answer(
            "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\n"
            "شم طيزي يلا",
            reply_to_message_id=message.message_id
        )


async def main():
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())