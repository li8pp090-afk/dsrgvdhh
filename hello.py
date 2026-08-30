import asyncio
import os
import re
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from yt_dlp import YoutubeDL


TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


bot = Bot(TOKEN)
dp = Dispatcher()


REPLIES = (
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
)

FAIL_REPLY = "الرابط غير مدعوم او الموقع مو مدعوم\nشم كسي يلا"


user_modes = {}
reply_indexes = {}
user_tasks = set()


URL_RE = re.compile(
    r"https?://\S+",
    re.IGNORECASE,
)

TELEGRAM_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?"
    r"(?:t\.me|telegram\.me|telegram\.dog)"
    r"(?:/|$)",
    re.IGNORECASE,
)


def extract_url(text):
    match = URL_RE.search(text or "")

    if not match:
        return None

    return match.group(0).rstrip(
        ".,!?)]}>\"'"
    )


def is_telegram_url(url):
    return bool(TELEGRAM_RE.match(url))


def get_mode_keyboard(user_id):
    mode = user_modes.get(
        user_id,
        "default",
    )

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
                    text="صوت",
                    callback_data="mode:voice",
                    style=voice_style,
                ),
                InlineKeyboardButton(
                    text="افتراضي",
                    callback_data="mode:default",
                    style=default_style,
                ),
            ]
        ]
    )


def get_files(directory):
    return [
        path
        for path in Path(directory).rglob("*")
        if path.is_file()
    ]


def get_latest_file(directory):
    files = get_files(directory)

    if not files:
        raise RuntimeError(
            "No downloaded file"
        )

    return max(
        files,
        key=lambda path: path.stat().st_mtime,
    )


def download_audio(url, directory):
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(
            Path(directory) / "%(title)s.%(ext)s"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(options) as ydl:
        ydl.download([url])

    return get_latest_file(directory)


def download_video(url, directory):
    options = {
        "format": (
            "bestvideo*+bestaudio/"
            "best"
        ),
        "outtmpl": str(
            Path(directory) / "%(title)s.%(ext)s"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": None,
    }

    with YoutubeDL(options) as ydl:
        ydl.download([url])

    return get_latest_file(directory)


async def convert_to_opus(source, directory):
    output = Path(directory) / "voice.ogg"

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "libopus",
        "-b:a",
        "0",
        "-f",
        "ogg",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="ignore")
        )

    if not output.exists():
        raise RuntimeError(
            "Opus conversion failed"
        )

    return output


async def process_voice(message, url):
    with tempfile.TemporaryDirectory(
        prefix="media_voice_"
    ) as directory:

        source = await asyncio.to_thread(
            download_audio,
            url,
            directory,
        )

        output = await convert_to_opus(
            source,
            directory,
        )

        data = await asyncio.to_thread(
            output.read_bytes
        )

        await message.answer_voice(
            BufferedInputFile(
                data,
                filename="voice.ogg",
            )
        )


async def process_video(message, url):
    with tempfile.TemporaryDirectory(
        prefix="media_video_"
    ) as directory:

        source = await asyncio.to_thread(
            download_video,
            url,
            directory,
        )

        data = await asyncio.to_thread(
            source.read_bytes
        )

        await message.answer_video(
            BufferedInputFile(
                data,
                filename=source.name,
            ),
            supports_streaming=True,
        )


async def run_download(message, url, mode):
    try:
        if mode == "voice":
            await process_voice(
                message,
                url,
            )
        else:
            await process_video(
                message,
                url,
            )

    except Exception:
        await message.answer(
            FAIL_REPLY
        )


def register_task(task):
    user_tasks.add(task)
    task.add_done_callback(
        user_tasks.discard
    )


@dp.message(
    CommandStart(),
    F.chat.type == ChatType.PRIVATE,
)
async def start_handler(message):
    user_id = message.from_user.id

    user_modes.setdefault(
        user_id,
        "default",
    )

    reply_indexes.setdefault(
        user_id,
        0,
    )

    await message.answer(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=get_mode_keyboard(
            user_id
        ),
    )


@dp.message(
    F.chat.type == ChatType.PRIVATE,
    F.text == "ادت",
)
async def mode_handler(message):
    user_id = message.from_user.id

    user_modes.setdefault(
        user_id,
        "default",
    )

    reply_indexes.setdefault(
        user_id,
        0,
    )

    await message.answer(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=get_mode_keyboard(
            user_id
        ),
    )


@dp.callback_query(
    F.data.startswith("mode:")
)
async def mode_callback(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return

    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer()
        return

    selected_mode = callback.data.split(
        ":",
        1,
    )[1]

    if selected_mode not in {
        "voice",
        "default",
    }:
        await callback.answer()
        return

    user_id = callback.from_user.id

    user_modes[user_id] = selected_mode

    await callback.message.edit_reply_markup(
        reply_markup=get_mode_keyboard(
            user_id
        )
    )

    await callback.answer()


@dp.message(
    F.chat.type == ChatType.PRIVATE,
    F.text,
)
async def text_handler(message):
    text = message.text or ""
    user_id = message.from_user.id

    user_modes.setdefault(
        user_id,
        "default",
    )

    reply_indexes.setdefault(
        user_id,
        0,
    )

    url = extract_url(text)

    if url and not is_telegram_url(url):
        mode = user_modes[user_id]

        task = asyncio.create_task(
            run_download(
                message,
                url,
                mode,
            )
        )

        register_task(task)
        return

    index = reply_indexes[user_id]

    await message.answer(
        REPLIES[index]
    )

    reply_indexes[user_id] = (
        index + 1
    ) % len(REPLIES)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())