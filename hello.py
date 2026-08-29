import os
import asyncio
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

from File import (
    init_file_database,
    get_file,
    save_file
)


TOKEN = os.getenv("BOT_TOKEN")

MAX_ACTIVE = 3
MAX_WAITING = 3
ALBUM_SIZE = 8

queues = defaultdict(deque)
active = defaultdict(int)
locks = defaultdict(asyncio.Lock)

bot = None


def get_key(message):
    return (
        message.chat.id,
        message.message_thread_id or 0
    )


def is_url(text):
    return text.startswith((
        "http://",
        "https://"
    ))


def is_youtube_command(text):
    parts = text.strip().split(
        maxsplit=1
    )

    if len(parts) != 2:
        return False

    return parts[0].casefold() in {
        "yt",
        "يوت"
    }


def youtube_query(text):
    return text.strip().split(
        maxsplit=1
    )[1].strip()


def get_setting(key):
    return settings.get(
        key,
        "default"
    )


def set_setting(key, mode):
    settings[key] = mode


def settings_keyboard(mode):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="الافتراضي",
                    callback_data="settings:default",
                    style=(
                        "primary"
                        if mode == "default"
                        else "danger"
                    )
                ),
                InlineKeyboardButton(
                    text="صوت",
                    callback_data="settings:audio",
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

    mime_type, _ = mimetypes.guess_type(
        filepath
    )

    if mime_type:
        if mime_type.startswith("image/"):
            return "photo"

        if mime_type.startswith("video/"):
            return "video"

        if mime_type.startswith("audio/"):
            return "audio"

    return "document"


def collect_files(info, folder):
    files = []

    def collect(entry):
        if not entry:
            return

        filepath = entry.get(
            "filepath"
        )

        if filepath and os.path.isfile(filepath):
            if filepath not in files:
                files.append(filepath)

        for item in entry.get(
            "requested_downloads"
        ) or []:
            filepath = item.get(
                "filepath"
            )

            if filepath and os.path.isfile(filepath):
                if filepath not in files:
                    files.append(filepath)

    entries = info.get(
        "entries"
    )

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
        key=lambda x: Path(x).stat().st_mtime
    )


async def download_default(url, folder):
    def run():
        options = {
            "outtmpl": str(
                Path(folder) /
                "%(title)s.%(ext)s"
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

        result = []

        entries = info.get(
            "entries"
        )

        if entries:
            for entry in entries:
                if not entry:
                    continue

                content_id = entry.get(
                    "id"
                )

                for filepath in collect_files(
                    entry,
                    folder
                ):
                    result.append(
                        (
                            content_id,
                            filepath
                        )
                    )
        else:
            content_id = info.get(
                "id"
            )

            for filepath in collect_files(
                info,
                folder
            ):
                result.append(
                    (
                        content_id,
                        filepath
                    )
                )

        return result

    return await asyncio.to_thread(
        run
    )


async def download_audio(url, folder):
    def run():
        options = {
            "outtmpl": str(
                Path(folder) /
                "%(title)s.%(ext)s"
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

        files = collect_files(
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

    cached = get_file(
        content_id,
        "audio",
        "voice"
    )

    if cached:
        return (
            content_id,
            None,
            cached
        )

    output = str(
        Path(folder) /
        "audio.ogg"
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

    return (
        content_id,
        output,
        None
    )


async def download_youtube_voice(
    title,
    folder
):
    def run():
        options = {
            "outtmpl": str(
                Path(folder) /
                "%(title)s.%(ext)s"
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

        entries = info.get(
            "entries"
        ) or []

        if not entries:
            raise FileNotFoundError()

        entry = entries[0]

        files = collect_files(
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

    cached = get_file(
        content_id,
        "youtube_voice",
        "voice"
    )

    if cached:
        return (
            content_id,
            None,
            cached
        )

    output = str(
        Path(folder) /
        "voice.ogg"
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

    return (
        content_id,
        output,
        None
    )


async def send_file(
    message,
    filepath,
    content_id,
    mode
):
    media_type = await asyncio.to_thread(
        detect_type,
        filepath
    )

    cached = get_file(
        content_id,
        mode,
        media_type
    )

    reply = message.as_reply_parameters()

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
        filename=Path(
            filepath
        ).name
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

    save_file(
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
    reply = message.as_reply_parameters()

    if cached:
        return await bot.send_voice(
            message.chat.id,
            cached,
            message_thread_id=message.message_thread_id,
            reply_parameters=reply
        )

    file = FSInputFile(
        filepath,
        filename="voice.ogg"
    )

    sent = await bot.send_voice(
        message.chat.id,
        file,
        message_thread_id=message.message_thread_id,
        reply_parameters=reply
    )

    save_file(
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
        ALBUM_SIZE
    ):
        batch = items[
            start:start + ALBUM_SIZE
        ]

        media = []
        metadata = []

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

            cached = get_file(
                content_id,
                mode,
                media_type
            )

            media_file = cached or FSInputFile(
                filepath,
                filename=Path(
                    filepath
                ).name
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

            metadata.append(
                (
                    content_id,
                    media_type
                )
            )

        if not media:
            continue

        if len(media) == 1:
            content_id, filepath = batch[0]

            await send_file(
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
            reply_parameters=message.as_reply_parameters()
        )

        for sent_message, item in zip(
            sent,
            metadata
        ):
            content_id, media_type = item

            if media_type == "photo":
                file_id = (
                    sent_message
                    .photo[-1]
                    .file_id
                )
            else:
                file_id = (
                    sent_message
                    .video
                    .file_id
                )

            save_file(
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
    folder = tempfile.mkdtemp(
        prefix="download_"
    )

    try:
        if mode == "audio":
            (
                content_id,
                filepath,
                cached
            ) = await download_audio(
                message.text,
                folder
            )

            await send_voice(
                message,
                filepath,
                content_id,
                mode,
                cached
            )

        elif mode == "youtube_voice":
            title = youtube_query(
                message.text
            )

            (
                content_id,
                filepath,
                cached
            ) = await download_youtube_voice(
                title,
                folder
            )

            await send_voice(
                message,
                filepath,
                content_id,
                mode,
                cached
            )

        else:
            items = await download_default(
                message.text,
                folder
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

                await send_file(
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

        await start_waiting(key)


async def start_waiting(key):
    async with locks[key]:
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


async def add_download(
    message,
    mode
):
    key = get_key(message)

    async with locks[key]:
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


async def settings_command(message):
    key = get_key(message)
    mode = get_setting(key)

    await message.answer(
        "تستطيع تغيير وضع عمل البوت من هذه\n"
        "الازرار",
        reply_markup=settings_keyboard(
            mode
        ),
        reply_parameters=message.as_reply_parameters()
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


async def handle(message: Message):
    if not message.text:
        return

    text = message.text.strip()

    if text.casefold() == "ادت":
        await settings_command(message)
        return

    if is_youtube_command(text):
        await add_download(
            message,
            "youtube_voice"
        )
        return

    if not is_url(text):
        return

    key = get_key(message)
    mode = get_setting(key)

    await add_download(
        message,
        mode
    )


async def callback_handler(
    callback: CallbackQuery
):
    data = callback.data or ""

    if data.startswith(
        "settings:"
    ):
        await settings_callback(
            callback
        )
        return

    await callback.answer()


async def main():
    global bot

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    init_file_database()

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


settings = {}


if __name__ == "__main__":
    asyncio.run(main())