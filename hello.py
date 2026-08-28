import os
import asyncio
import sqlite3
import tempfile
import shutil
import subprocess
import mimetypes
from pathlib import Path
from collections import defaultdict, deque

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


TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "bot.db")

MAX_ACTIVE = 3
MAX_WAITING = 3
ALBUM_SIZE = 8

queues = defaultdict(deque)
active = defaultdict(int)
locks = defaultdict(asyncio.Lock)


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()

    conn.execute("""
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            PRIMARY KEY (
                chat_id,
                thread_id
            )
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            mode TEXT NOT NULL DEFAULT 'default',
            PRIMARY KEY (
                chat_id,
                thread_id
            )
        )
    """)

    conn.commit()
    conn.close()


def save_chat(key):
    conn = db()

    conn.execute(
        """
        INSERT OR IGNORE INTO chats
        (chat_id, thread_id)
        VALUES (?, ?)
        """,
        key
    )

    conn.commit()
    conn.close()


def get_setting(key):
    conn = db()

    row = conn.execute(
        """
        SELECT mode
        FROM settings
        WHERE chat_id = ?
        AND thread_id = ?
        """,
        key
    ).fetchone()

    conn.close()

    return row[0] if row else "default"


def set_setting(key, mode):
    conn = db()

    conn.execute(
        """
        INSERT OR REPLACE INTO settings
        (
            chat_id,
            thread_id,
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

    conn.commit()
    conn.close()


def cache_get(
    content_id,
    mode,
    media_type
):
    conn = db()

    row = conn.execute(
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
    ).fetchone()

    conn.close()

    return row[0] if row else None


def cache_set(
    content_id,
    mode,
    media_type,
    file_id
):
    conn = db()

    conn.execute(
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

    conn.commit()
    conn.close()


def get_key(message):
    return (
        message.chat.id,
        message.message_thread_id or 0
    )


def valid_url(text):
    return text.startswith((
        "http://",
        "https://"
    ))


def is_youtube_command(text):
    if not text:
        return False

    parts = text.strip().split(
        maxsplit=1
    )

    if len(parts) != 2:
        return False

    command = parts[0].casefold()

    return command in {
        "يوت",
        "yt"
    }


def youtube_query(text):
    return text.strip().split(
        maxsplit=1
    )[1].strip()


def download_keyboard(mode):
    if mode == "audio":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎵 صوت",
                        callback_data="download:audio",
                        style="primary"
                    )
                ]
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 الافتراضي",
                    callback_data="download:default",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="🎵 صوت",
                    callback_data="download:audio",
                    style="danger"
                )
            ]
        ]
    )


def settings_keyboard(mode):
    default_style = (
        "primary"
        if mode == "default"
        else "danger"
    )

    audio_style = (
        "primary"
        if mode == "audio"
        else "danger"
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="الافتراضي",
                    callback_data="settings:default",
                    style=default_style
                ),
                InlineKeyboardButton(
                    text="صوت",
                    callback_data="settings:audio",
                    style=audio_style
                )
            ]
        ]
    )


def probe(filepath):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                filepath
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        streams = result.stdout.lower().splitlines()

        if "video" in streams:
            return "video"

        if "audio" in streams:
            return "audio"

    except Exception:
        pass

    mime_type, _ = mimetypes.guess_type(filepath)

    if mime_type:
        if mime_type.startswith("image/"):
            return "photo"

        if mime_type.startswith("video/"):
            return "video"

        if mime_type.startswith("audio/"):
            return "audio"

    return "document"


def collect_downloaded(info, folder):
    files = []

    def collect(entry):
        if not entry:
            return

        filepath = entry.get("filepath")

        if filepath and os.path.isfile(filepath):
            if filepath not in files:
                files.append(filepath)

        requested = entry.get(
            "requested_downloads"
        ) or []

        for item in requested:
            filepath = item.get("filepath")

            if filepath and os.path.isfile(filepath):
                if filepath not in files:
                    files.append(filepath)

    entries = info.get("entries")

    if entries:
        for entry in entries:
            collect(entry)
    else:
        collect(info)

    if not files:
        files = [
            str(path)
            for path in Path(folder).iterdir()
            if path.is_file()
        ]

    return sorted(
        files,
        key=lambda path: Path(path).stat().st_mtime
    )


def yt_info(url, folder, options):
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=True
        )


async def download_default(
    url,
    folder
):
    def run():
        options = {
            "outtmpl": str(
                Path(folder) / "%(title)s.%(ext)s"
            ),
            "format": "bestvideo*+bestaudio/best",
            "noplaylist": False,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "restrictfilenames": False,
            "windowsfilenames": False
        }

        info = yt_info(
            url,
            folder,
            options
        )

        entries = info.get("entries")

        if entries:
            result = []

            for entry in entries:
                if not entry:
                    continue

                files = collect_downloaded(
                    entry,
                    folder
                )

                content_id = entry.get("id")

                for filepath in files:
                    result.append(
                        (
                            content_id,
                            filepath
                        )
                    )

            return result

        files = collect_downloaded(
            info,
            folder
        )

        content_id = info.get("id")

        return [
            (
                content_id,
                filepath
            )
            for filepath in files
        ]

    return await asyncio.to_thread(run)


async def download_audio(
    url,
    folder
):
    def run():
        options = {
            "outtmpl": str(
                Path(folder) / "%(title)s.%(ext)s"
            ),
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
            "windowsfilenames": False
        }

        info = yt_info(
            url,
            folder,
            options
        )

        files = collect_downloaded(
            info,
            folder
        )

        if not files:
            raise FileNotFoundError()

        return (
            info.get("id"),
            files[0]
        )

    content_id, source = await asyncio.to_thread(
        run
    )

    cached = cache_get(
        content_id,
        "audio",
        "voice"
    )

    if cached:
        return content_id, None, cached

    output = str(
        Path(folder) / "audio.ogg"
    )

    def convert():
        subprocess.run(
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

    await asyncio.to_thread(
        convert
    )

    return content_id, output, None


async def download_youtube_voice(
    title,
    folder
):
    def run():
        options = {
            "outtmpl": str(
                Path(folder) / "%(title)s.%(ext)s"
            ),
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
            "windowsfilenames": False
        }

        info = yt_info(
            f"ytsearch1:{title}",
            folder,
            options
        )

        entries = info.get("entries") or []

        if not entries:
            raise FileNotFoundError()

        entry = entries[0]

        files = collect_downloaded(
            entry,
            folder
        )

        if not files:
            raise FileNotFoundError()

        return (
            entry.get("id"),
            files[0]
        )

    content_id, source = await asyncio.to_thread(
        run
    )

    cached = cache_get(
        content_id,
        "youtube_voice",
        "voice"
    )

    if cached:
        return content_id, None, cached

    output = str(
        Path(folder) / "voice.ogg"
    )

    def convert():
        subprocess.run(
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

    await asyncio.to_thread(
        convert
    )

    return content_id, output, None


async def send_file(
    bot,
    message,
    filepath,
    content_id,
    mode
):
    media_type = await asyncio.to_thread(
        probe,
        filepath
    )

    old_file_id = cache_get(
        content_id,
        mode,
        media_type
    )

    if old_file_id:
        if media_type == "photo":
            return await bot.send_photo(
                message.chat.id,
                old_file_id,
                message_thread_id=message.message_thread_id
            )

        if media_type == "video":
            return await bot.send_video(
                message.chat.id,
                old_file_id,
                message_thread_id=message.message_thread_id
            )

        return await bot.send_document(
            message.chat.id,
            old_file_id,
            message_thread_id=message.message_thread_id
        )

    file = FSInputFile(
        filepath,
        filename=Path(filepath).name
    )

    if media_type == "photo":
        sent = await bot.send_photo(
            message.chat.id,
            file,
            message_thread_id=message.message_thread_id
        )

        file_id = sent.photo[-1].file_id

    elif media_type == "video":
        sent = await bot.send_video(
            message.chat.id,
            file,
            message_thread_id=message.message_thread_id
        )

        file_id = sent.video.file_id

    else:
        sent = await bot.send_document(
            message.chat.id,
            file,
            message_thread_id=message.message_thread_id
        )

        file_id = sent.document.file_id

    cache_set(
        content_id,
        mode,
        media_type,
        file_id
    )

    return sent


async def send_voice_cached(
    bot,
    message,
    filepath,
    content_id,
    mode,
    cached_file_id=None
):
    old_file_id = cached_file_id or cache_get(
        content_id,
        mode,
        "voice"
    )

    if old_file_id:
        return await bot.send_voice(
            message.chat.id,
            old_file_id,
            message_thread_id=message.message_thread_id
        )

    file = FSInputFile(
        filepath,
        filename="voice.ogg"
    )

    sent = await bot.send_voice(
        message.chat.id,
        file,
        message_thread_id=message.message_thread_id
    )

    cache_set(
        content_id,
        mode,
        "voice",
        sent.voice.file_id
    )

    return sent


async def send_album(
    bot,
    message,
    items,
    mode
):
    for start in range(
        0,
        len(items),
        ALBUM_SIZE
    ):
        batch = items[
            start:start + ALBUM_SIZE
        ]

        media = []
        uncached = []

        for content_id, filepath in batch:
            media_type = await asyncio.to_thread(
                probe,
                filepath
            )

            if media_type not in {
                "photo",
                "video"
            }:
                continue

            file_id = cache_get(
                content_id,
                mode,
                media_type
            )

            if file_id:
                media.append(
                    (
                        content_id,
                        media_type,
                        file_id,
                        None
                    )
                )

            else:
                file = FSInputFile(
                    filepath,
                    filename=Path(
                        filepath
                    ).name
                )

                media.append(
                    (
                        content_id,
                        media_type,
                        None,
                        file
                    )
                )

            uncached.append(
                (
                    content_id,
                    filepath,
                    media_type
                )
            )

        if not media:
            continue

        if len(media) == 1:
            content_id, media_type, file_id, file = media[0]

            if file_id:
                if media_type == "photo":
                    await bot.send_photo(
                        message.chat.id,
                        file_id,
                        message_thread_id=message.message_thread_id
                    )
                else:
                    await bot.send_video(
                        message.chat.id,
                        file_id,
                        message_thread_id=message.message_thread_id
                    )

            else:
                await send_file(
                    bot,
                    message,
                    file,
                    content_id,
                    mode
                )

            continue

        telegram_media = []

        for (
            content_id,
            media_type,
            file_id,
            file
        ) in media:

            value = file_id or file

            if media_type == "photo":
                telegram_media.append(
                    InputMediaPhoto(
                        media=value
                    )
                )
            else:
                telegram_media.append(
                    InputMediaVideo(
                        media=value
                    )
                )

        sent_messages = await bot.send_media_group(
            message.chat.id,
            media=telegram_media,
            message_thread_id=message.message_thread_id
        )

        for (
            item,
            sent
        ) in zip(
            media,
            sent_messages
        ):
            (
                content_id,
                media_type,
                old_file_id,
                file
            ) = item

            if old_file_id:
                continue

            if media_type == "photo":
                file_id = sent.photo[-1].file_id
            else:
                file_id = sent.video.file_id

            cache_set(
                content_id,
                mode,
                media_type,
                file_id
            )


async def process(
    bot,
    message,
    key,
    mode
):
    folder = tempfile.mkdtemp(
        prefix="download_"
    )

    try:
        if mode == "youtube_voice":
            title = youtube_query(
                message.text
            )

            content_id, filepath, cached = (
                await download_youtube_voice(
                    title,
                    folder
                )
            )

            await send_voice_cached(
                bot,
                message,
                filepath,
                content_id,
                mode,
                cached
            )

        elif mode == "audio":
            content_id, filepath, cached = (
                await download_audio(
                    message.text.strip(),
                    folder
                )
            )

            await send_voice_cached(
                bot,
                message,
                filepath,
                content_id,
                mode,
                cached
            )

        else:
            items = await download_default(
                message.text.strip(),
                folder
            )

            valid_items = []

            for content_id, filepath in items:
                if not content_id:
                    continue

                media_type = await asyncio.to_thread(
                    probe,
                    filepath
                )

                if media_type in {
                    "photo",
                    "video"
                }:
                    valid_items.append(
                        (
                            content_id,
                            filepath
                        )
                    )

            if len(valid_items) == 1:
                content_id, filepath = valid_items[0]

                await send_file(
                    bot,
                    message,
                    filepath,
                    content_id,
                    mode
                )

            elif valid_items:
                await send_album(
                    bot,
                    message,
                    valid_items,
                    mode
                )

    except Exception:
        pass

    finally:
        await asyncio.to_thread(
            shutil.rmtree,
            folder,
            True
        )

        async with locks[key]:
            active[key] -= 1

        await start_waiting(
            bot,
            key
        )


async def start_waiting(
    bot,
    key
):
    async with locks[key]:
        if active[key] >= MAX_ACTIVE:
            return

        if not queues[key]:
            return

        message, mode = queues[key].popleft()

        active[key] += 1

    asyncio.create_task(
        process(
            bot,
            message,
            key,
            mode
        )
    )


async def add_download(
    bot,
    message,
    mode
):
    key = get_key(message)

    save_chat(key)

    async with locks[key]:
        if active[key] < MAX_ACTIVE:
            active[key] += 1

            asyncio.create_task(
                process(
                    bot,
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


async def settings_command(
    message: Message
):
    key = get_key(message)

    save_chat(key)

    mode = get_setting(key)

    await message.answer(
        "تستطيع تغيير وضع عمل البوت من هذه\n"
        "الازرار",
        reply_markup=settings_keyboard(mode)
    )


async def settings_callback(
    callback: CallbackQuery
):
    if not callback.message:
        return

    key = get_key(
        callback.message
    )

    current = get_setting(key)

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

        set_setting(
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

    set_setting(
        key,
        selected
    )

    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(
            selected
        )
    )

    await callback.answer()


async def handle(
    message: Message
):
    if not message.text:
        return

    text = message.text.strip()

    if text.casefold() == "ادت":
        await settings_command(
            message
        )
        return

    if is_youtube_command(text):
        await add_download(
            bot,
            message,
            "youtube_voice"
        )
        return

    if not valid_url(text):
        return

    key = get_key(message)

    mode = get_setting(key)

    await message.reply(
        "اختر طريقة التحميل:",
        reply_markup=download_keyboard(mode)
    )


async def callback_handler(
    callback: CallbackQuery
):
    if not callback.message:
        return

    data = callback.data or ""

    if data.startswith("settings:"):
        await settings_callback(
            callback
        )
        return

    if not data.startswith("download:"):
        await callback.answer()
        return

    original = callback.message.reply_to_message

    if not original or not original.text:
        await callback.answer()
        return

    mode = data.split(
        ":",
        1
    )[1]

    await callback.answer()

    await add_download(
        bot,
        original,
        mode
    )


async def main():
    global bot

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    init_db()

    bot = Bot(
        token=TOKEN
    )

    await bot.send_message(
        chat_id=8044375236,
        text="؟!\n"
             "اشتغل البوت مرتلخ مولاي\n"
             "ارضع عيرك"
    )

    dp = Dispatcher()

    dp.message.register(
        handle,
        F.text
    )

    dp.callback_query.register(
        callback_handler
    )

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query"
        ]
    )


if __name__ == "__main__":
    asyncio.run(main())