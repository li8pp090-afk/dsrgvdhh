import os
import asyncio
import tempfile
import shutil
import subprocess
import mimetypes
from pathlib import Path
from collections import defaultdict, deque

import aiosqlite
import yt_dlp

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo
)


BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE = os.getenv("DATABASE_PATH", "bot.db")

MAX_ACTIVE = 3
MAX_WAITING = 3
ALBUM_LIMIT = 8

bot = None

queues = defaultdict(deque)
active = defaultdict(int)
queue_locks = defaultdict(asyncio.Lock)


async def initialize():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                content_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                PRIMARY KEY (
                    content_id,
                    mode,
                    media_type
                )
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    topic_id
                )
            )
        """)
        await db.commit()


def chat_key(message):
    return (
        message.chat.id,
        message.message_thread_id or 0
    )


async def get_mode(key):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            """
            SELECT mode
            FROM settings
            WHERE chat_id = ?
            AND topic_id = ?
            """,
            key
        ) as cursor:
            row = await cursor.fetchone()

    if row:
        return row[0]

    return "default"


async def save_mode(key, mode):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO settings
            (
                chat_id,
                topic_id,
                mode
            )
            VALUES (?, ?, ?)
            """,
            (
                key[0],
                key[1],
                mode
            )
        )
        await db.commit()


async def get_cached_file(
    content_id,
    mode,
    media_type
):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            """
            SELECT file_id
            FROM cache
            WHERE content_id = ?
            AND mode = ?
            AND media_type = ?
            """,
            (
                content_id,
                mode,
                media_type
            )
        ) as cursor:
            row = await cursor.fetchone()

    return row[0] if row else None


async def save_cached_file(
    content_id,
    mode,
    media_type,
    file_id
):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO cache
            (
                content_id,
                mode,
                media_type,
                file_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                content_id,
                mode,
                media_type,
                file_id
            )
        )
        await db.commit()


def is_url(text):
    return text.startswith(
        (
            "http://",
            "https://"
        )
    )


def youtube_command(text):
    parts = text.strip().split(
        maxsplit=1
    )

    if len(parts) != 2:
        return False

    return parts[0].casefold() in {
        "yt",
        "يوت"
    }


def youtube_title(text):
    return text.strip().split(
        maxsplit=1
    )[1].strip()


def reply_parameters(message):
    return message.as_reply_parameters()


def settings_keyboard(mode):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="الافتراضي",
                    callback_data="mode:default",
                    style=(
                        "primary"
                        if mode == "default"
                        else "danger"
                    )
                ),
                InlineKeyboardButton(
                    text="صوت",
                    callback_data="mode:audio",
                    style=(
                        "primary"
                        if mode == "audio"
                        else "danger"
                    )
                )
            ]
        ]
    )


def detect_type(filepath):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                filepath
            ],
            capture_output=True,
            text=True,
            timeout=15
        )

        streams = {
            item.strip().lower()
            for item in result.stdout.splitlines()
        }

        if "video" in streams:
            return "video"

        if "audio" in streams:
            return "audio"

    except Exception:
        pass

    mime, _ = mimetypes.guess_type(
        filepath
    )

    if mime:
        if mime.startswith("image/"):
            return "photo"

        if mime.startswith("video/"):
            return "video"

        if mime.startswith("audio/"):
            return "audio"

    return "document"


def downloaded_files(info, directory):
    result = []

    def collect(entry):
        if not entry:
            return

        filepath = entry.get("filepath")

        if filepath and os.path.isfile(filepath):
            if filepath not in result:
                result.append(filepath)

        for item in (
            entry.get("requested_downloads")
            or []
        ):
            filepath = item.get("filepath")

            if filepath and os.path.isfile(filepath):
                if filepath not in result:
                    result.append(filepath)

    entries = info.get("entries")

    if entries:
        for entry in entries:
            collect(entry)
    else:
        collect(info)

    if not result:
        result = [
            str(path)
            for path in Path(directory).iterdir()
            if path.is_file()
        ]

    return sorted(
        result,
        key=lambda item: Path(item).stat().st_mtime
    )


def inspect_url(url):
    def run():
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(
                url,
                download=False
            )

    return asyncio.to_thread(run)


async def default_download(
    url,
    directory
):
    def run():
        options = {
            "outtmpl": str(
                Path(directory) / "%(title)s.%(ext)s"
            ),
            "format": "bestvideo*+bestaudio/best",
            "noplaylist": False,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )

        if not info:
            raise FileNotFoundError()

        output = []

        entries = info.get("entries")

        if entries:
            for entry in entries:
                if not entry:
                    continue

                content_id = entry.get("id")

                for filepath in downloaded_files(
                    entry,
                    directory
                ):
                    output.append(
                        (
                            content_id,
                            filepath
                        )
                    )
        else:
            content_id = info.get("id")

            for filepath in downloaded_files(
                info,
                directory
            ):
                output.append(
                    (
                        content_id,
                        filepath
                    )
                )

        return output

    return await asyncio.to_thread(run)


async def audio_download(
    url,
    directory
):
    def run():
        options = {
            "outtmpl": str(
                Path(directory) / "%(title)s.%(ext)s"
            ),
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )

        if not info:
            raise FileNotFoundError()

        files = downloaded_files(
            info,
            directory
        )

        if not files:
            raise FileNotFoundError()

        return info.get("id"), files[0]

    content_id, source = await asyncio.to_thread(
        run
    )

    cached = await get_cached_file(
        content_id,
        "audio",
        "voice"
    )

    if cached:
        return content_id, None, cached

    output = str(
        Path(directory) / "audio.ogg"
    )

    await asyncio.to_thread(
        lambda: subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                source,
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "libopus",
                output
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
    )

    return content_id, output, None


async def youtube_audio(
    title,
    directory
):
    def run():
        options = {
            "outtmpl": str(
                Path(directory) / "%(title)s.%(ext)s"
            ),
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"ytsearch1:{title}",
                download=True
            )

        if not info:
            raise FileNotFoundError()

        entries = info.get("entries") or []

        if not entries:
            raise FileNotFoundError()

        entry = entries[0]

        files = downloaded_files(
            entry,
            directory
        )

        if not files:
            raise FileNotFoundError()

        return entry.get("id"), files[0]

    content_id, source = await asyncio.to_thread(
        run
    )

    cached = await get_cached_file(
        content_id,
        "youtube_voice",
        "voice"
    )

    if cached:
        return content_id, None, cached

    output = str(
        Path(directory) / "youtube.ogg"
    )

    await asyncio.to_thread(
        lambda: subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                source,
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "libopus",
                output
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
    )

    return content_id, output, None


async def send_media(
    message,
    filepath,
    content_id,
    mode
):
    media_type = await asyncio.to_thread(
        detect_type,
        filepath
    )

    cached = await get_cached_file(
        content_id,
        mode,
        media_type
    )

    reply = reply_parameters(
        message
    )

    if cached:
        if media_type == "photo":
            return await bot.send_photo(
                message.chat.id,
                cached,
                message_thread_id=message.message_thread_id,
                reply_parameters=reply
            )

        if media_type == "video":
            return await bot.send_video(
                message.chat.id,
                cached,
                message_thread_id=message.message_thread_id,
                reply_parameters=reply
            )

        return await bot.send_document(
            message.chat.id,
            cached,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply
        )

    file = FSInputFile(
        filepath,
        filename=Path(filepath).name
    )

    if media_type == "photo":
        sent = await bot.send_photo(
            message.chat.id,
            file,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply
        )

        file_id = sent.photo[-1].file_id

    elif media_type == "video":
        sent = await bot.send_video(
            message.chat.id,
            file,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply
        )

        file_id = sent.video.file_id

    else:
        sent = await bot.send_document(
            message.chat.id,
            file,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply
        )

        file_id = sent.document.file_id

    await save_cached_file(
        content_id,
        mode,
        media_type,
        file_id
    )

    return sent


async def send_voice(
    message,
    filepath,
    content_id,
    mode,
    cached=None
):
    reply = reply_parameters(
        message
    )

    if cached:
        return await bot.send_voice(
            message.chat.id,
            cached,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply
        )

    file = FSInputFile(
        filepath,
        filename=Path(filepath).name
    )

    sent = await bot.send_voice(
        message.chat.id,
        file,
        message_thread_id=message.message_thread_id,
        reply_parameters=reply
    )

    await save_cached_file(
        content_id,
        mode,
        "voice",
        sent.voice.file_id
    )

    return sent


async def send_album(
    message,
    items,
    mode
):
    for start in range(
        0,
        len(items),
        ALBUM_LIMIT
    ):
        batch = items[
            start:start + ALBUM_LIMIT
        ]

        media = []
        cache_data = []

        for content_id, filepath in batch:
            media_type = await asyncio.to_thread(
                detect_type,
                filepath
            )

            if media_type not in {
                "photo",
                "video"
            }:
                continue

            cached = await get_cached_file(
                content_id,
                mode,
                media_type
            )

            media_file = cached or FSInputFile(
                filepath,
                filename=Path(filepath).name
            )

            if media_type == "photo":
                media.append(
                    InputMediaPhoto(
                        media=media_file
                    )
                )
            else:
                media.append(
                    InputMediaVideo(
                        media=media_file
                    )
                )

            cache_data.append(
                (
                    content_id,
                    media_type
                )
            )

        if not media:
            continue

        if len(media) == 1:
            content_id, filepath = batch[0]

            await send_media(
                message,
                filepath,
                content_id,
                mode
            )

            continue

        sent = await bot.send_media_group(
            message.chat.id,
            media=media,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply_parameters(message)
        )

        for sent_message, data in zip(
            sent,
            cache_data
        ):
            content_id, media_type = data

            if media_type == "photo":
                file_id = sent_message.photo[-1].file_id
            else:
                file_id = sent_message.video.file_id

            await save_cached_file(
                content_id,
                mode,
                media_type,
                file_id
            )


async def process(
    message,
    key,
    mode
):
    directory = tempfile.mkdtemp(
        prefix="download_"
    )

    progress_msg = None

    try:
        if mode == "youtube_voice":
            title = youtube_title(
                message.text
            )

            progress_text = (
                f"ها تريد {title}\n"
                "بلة انطيني شوي من وقتك"
            )
            error_text = (
                "لقد تعثرت المعذرة \n"
                "هذا العنوان غير متوفر"
            )
        else:
            progress_text = (
                "?!\n"
                "شو يعني من تدز رابط اشتغل هيج تريد\n"
                "ديلا يلا ماشي"
            )
            error_text = (
                "الرابط غير مدعوم او الموقع غير مدعوم \n"
                "ههع شم كسي يلا"
            )

        progress_msg = await bot.send_message(
            message.chat.id,
            progress_text,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply_parameters(message)
        )

        try:
            if mode == "audio":
                content_id, filepath, cached = (
                    await audio_download(
                        message.text,
                        directory
                    )
                )

                await send_voice(
                    message,
                    filepath,
                    content_id,
                    mode,
                    cached
                )

            elif mode == "youtube_voice":
                title = youtube_title(
                    message.text
                )

                content_id, filepath, cached = (
                    await youtube_audio(
                        title,
                        directory
                    )
                )

                await send_voice(
                    message,
                    filepath,
                    content_id,
                    mode,
                    cached
                )

            else:
                items = await default_download(
                    message.text,
                    directory
                )

                media = []

                for content_id, filepath in items:
                    if not content_id:
                        continue

                    media_type = await asyncio.to_thread(
                        detect_type,
                        filepath
                    )

                    if media_type in {
                        "photo",
                        "video"
                    }:
                        media.append(
                            (
                                content_id,
                                filepath
                            )
                        )

                if len(media) == 1:
                    content_id, filepath = media[0]

                    await send_media(
                        message,
                        filepath,
                        content_id,
                        mode
                    )

                elif media:
                    await send_album(
                        message,
                        media,
                        mode
                    )
                else:
                    raise FileNotFoundError()

            if progress_msg:
                await bot.delete_message(
                    message.chat.id,
                    progress_msg.message_id
                )

        except Exception:
            if progress_msg:
                await bot.edit_message_text(
                    error_text,
                    chat_id=message.chat.id,
                    message_id=progress_msg.message_id
                )

    finally:
        await asyncio.to_thread(
            shutil.rmtree,
            directory,
            True
        )

        async with queue_locks[key]:
            active[key] -= 1

        await start_next(key)


async def start_next(key):
    async with queue_locks[key]:
        if active[key] >= MAX_ACTIVE:
            return

        if not queues[key]:
            return

        message, mode = queues[key].popleft()

        active[key] += 1

    asyncio.create_task(
        process(
            message,
            key,
            mode
        )
    )


async def enqueue(
    message,
    mode
):
    key = chat_key(message)

    async with queue_locks[key]:
        if active[key] < MAX_ACTIVE:
            active[key] += 1

            asyncio.create_task(
                process(
                    message,
                    key,
                    mode
                )
            )

            return

        if len(queues[key]) < MAX_WAITING:
            queues[key].append(
                (
                    message,
                    mode
                )
            )


async def show_settings(
    message
):
    key = chat_key(message)
    mode = await get_mode(key)

    await message.answer(
        "تستطيع تغيير وضع عمل البوت من هذه\n"
        "الازرار",
        reply_markup=settings_keyboard(mode),
        reply_parameters=reply_parameters(message)
    )


async def settings_callback(
    callback
):
    if not callback.message:
        return

    key = chat_key(
        callback.message
    )

    current = await get_mode(key)

    selected = callback.data.split(
        ":",
        1
    )[1]

    if selected == current:
        if current == "default":
            await callback.answer(
                "لا يمكنك تعطيل الوضع الافتراضي\n"
                "تستطيع تبديل الوضع وليس تعطيل كل الاوضاع",
                show_alert=False
            )
            return

        await save_mode(
            key,
            "default"
        )

        await callback.message.edit_reply_markup(
            reply_markup=settings_keyboard(
                "default"
            )
        )

        await callback.answer()
        return

    await save_mode(
        key,
        selected
    )

    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(
            selected
        )
    )

    await callback.answer()


async def on_message(
    message: Message
):
    if not message.text:
        return

    text = message.text.strip()

    if text.casefold() == "ادت":
        await show_settings(message)
        return

    if youtube_command(text):
        await enqueue(
            message,
            "youtube_voice"
        )
        return

    if not is_url(text):
        return

    mode = await get_mode(
        chat_key(message)
    )

    await enqueue(
        message,
        mode
    )


async def on_callback(
    callback: CallbackQuery
):
    if callback.data.startswith(
        "mode:"
    ):
        await settings_callback(
            callback
        )
        return

    await callback.answer()


async def main():
    global bot

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    await initialize()

    bot = Bot(
        token=BOT_TOKEN
    )

    await bot.send_message(
        8044375236,
        "؟!\n"
        "اشتغل البوت مرتلخ مولاي\n"
        "ارضع عيرك"
    )

    dispatcher = Dispatcher()

    dispatcher.message.register(
        on_message,
        F.text
    )

    dispatcher.callback_query.register(
        on_callback
    )

    await dispatcher.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(main())
