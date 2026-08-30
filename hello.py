import asyncio
import os
import re
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
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

START_REPLY = "ههع شم كسي\nيلا"

FAIL_REPLY = (
    "الرابط غير مدعوم او الموقع مو مدعوم\n"
    "شم طيزي يلا"
)

YOUTUBE_REPLY = "ها تريد {query}\nتمام عبي"


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


MAX_ACTIVE = 3
MAX_WAITING = 3


user_modes = {}
reply_indexes = {}
mode_messages = {}
chat_queues = {}


def key_for(message):
    user_id = (
        message.from_user.id
        if message.from_user
        else 0
    )

    return message.chat.id, user_id


def reply_params(message):
    return ReplyParameters(
        message_id=message.message_id,
        allow_sending_without_reply=True,
    )


def extract_url(text):
    match = URL_RE.search(text or "")

    if not match:
        return None

    return match.group(0).rstrip(
        ".,!?)]}>\"'"
    )


def is_telegram_url(url):
    return bool(
        TELEGRAM_RE.match(url)
    )


def ytdlp_options(**extra):
    options = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "remote_components": (
            "ejs:github",
        ),
    }

    options.update(extra)

    return options


def latest_file(directory):
    files = [
        path
        for path in Path(directory).rglob("*")
        if path.is_file()
    ]

    if not files:
        raise RuntimeError(
            "No downloaded file"
        )

    return max(
        files,
        key=lambda path: path.stat().st_mtime,
    )


def download_file(
    url,
    directory,
    mode,
):
    if mode == "audio":
        file_format = "bestaudio/best"
    else:
        file_format = (
            "bestvideo*+bestaudio/"
            "best"
        )

    options = ytdlp_options(
        format=file_format,
        outtmpl=str(
            Path(directory)
            / "%(title)s.%(ext)s"
        ),
    )

    with YoutubeDL(options) as ydl:
        ydl.download([url])

    return latest_file(directory)


def youtube_search(query):
    options = ytdlp_options(
        extract_flat=True,
        skip_download=True,
    )

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"ytsearch1:{query}",
            download=False,
        )

    entries = info.get("entries") or []

    if not entries:
        raise RuntimeError(
            "No YouTube result"
        )

    result = entries[0]

    url = result.get(
        "webpage_url"
    )

    if not url:
        video_id = result.get("id")

        if not video_id:
            raise RuntimeError(
                "No YouTube URL"
            )

        url = (
            "https://www.youtube.com/watch?v="
            + video_id
        )

    return url


async def convert_to_opus(
    source,
    directory,
):
    output = (
        Path(directory)
        / "voice.ogg"
    )

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
        "-f",
        "ogg",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(
                errors="ignore"
            )
        )

    if not output.exists():
        raise RuntimeError(
            "Opus conversion failed"
        )

    return output


async def send_opus(
    message,
    url,
):
    with tempfile.TemporaryDirectory() as directory:
        source = await asyncio.to_thread(
            download_file,
            url,
            directory,
            "audio",
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
            ),
            reply_parameters=reply_params(
                message
            ),
        )


async def send_video(
    message,
    url,
):
    with tempfile.TemporaryDirectory() as directory:
        source = await asyncio.to_thread(
            download_file,
            url,
            directory,
            "video",
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
            reply_parameters=reply_params(
                message
            ),
        )


def mode_keyboard(mode):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="صوت",
                    callback_data="mode:voice",
                    style=(
                        "primary"
                        if mode == "voice"
                        else "danger"
                    ),
                ),
                InlineKeyboardButton(
                    text="افتراضي",
                    callback_data="mode:default",
                    style=(
                        "primary"
                        if mode == "default"
                        else "danger"
                    ),
                ),
            ]
        ]
    )


async def send_mode_panel(message):
    key = key_for(message)

    old_message = mode_messages.get(key)

    if old_message:
        try:
            await bot.delete_message(
                chat_id=message.chat.id,
                message_id=old_message,
            )
        except Exception:
            pass

    mode = user_modes.get(
        key,
        "default",
    )

    sent = await message.answer(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=mode_keyboard(mode),
        reply_parameters=reply_params(
            message
        ),
    )

    mode_messages[key] = sent.message_id


async def normal_download(
    message,
    url,
    mode,
    status,
):
    try:
        if mode == "voice":
            await send_opus(
                message,
                url,
            )
        else:
            await send_video(
                message,
                url,
            )

        await status.delete()

    except Exception:
        try:
            await status.delete()
        except Exception:
            pass

        await message.answer(
            FAIL_REPLY,
            reply_parameters=reply_params(
                message
            ),
        )


async def youtube_download(
    message,
    query,
):
    status = None

    try:
        status = await message.answer(
            START_REPLY,
            reply_parameters=reply_params(
                message
            ),
        )

        url = await asyncio.to_thread(
            youtube_search,
            query,
        )

        await send_opus(
            message,
            url,
        )

        await status.delete()

    except Exception:
        if status:
            try:
                await status.delete()
            except Exception:
                pass

        await message.answer(
            FAIL_REPLY,
            reply_parameters=reply_params(
                message
            ),
        )


async def worker(chat_id):
    data = chat_queues[chat_id]

    while True:
        job = await data["queue"].get()

        data["waiting"] -= 1
        data["active"] += 1

        try:
            await job()
        finally:
            data["active"] -= 1
            data["queue"].task_done()


async def ensure_chat(chat_id):
    if chat_id in chat_queues:
        return

    chat_queues[chat_id] = {
        "queue": asyncio.Queue(),
        "active": 0,
        "waiting": 0,
    }

    for _ in range(MAX_ACTIVE):
        asyncio.create_task(
            worker(chat_id)
        )


async def add_job(
    message,
    job,
):
    chat_id = message.chat.id

    await ensure_chat(chat_id)

    data = chat_queues[chat_id]

    if (
        data["active"]
        + data["waiting"]
        >= MAX_ACTIVE
        + MAX_WAITING
    ):
        return False

    data["waiting"] += 1

    await data["queue"].put(job)

    return True


@dp.message(CommandStart())
async def start_handler(message):
    key = key_for(message)

    user_modes.setdefault(
        key,
        "default",
    )

    reply_indexes.setdefault(
        key,
        0,
    )

    await send_mode_panel(message)


@dp.message(F.text == "ادت")
async def mode_handler(message):
    key = key_for(message)

    user_modes.setdefault(
        key,
        "default",
    )

    reply_indexes.setdefault(
        key,
        0,
    )

    await send_mode_panel(message)


@dp.callback_query(
    F.data.startswith("mode:")
)
async def mode_callback(
    callback: CallbackQuery,
):
    if not callback.message:
        await callback.answer()
        return

    key = (
        callback.message.chat.id,
        callback.from_user.id,
    )

    mode = callback.data.split(
        ":",
        1,
    )[1]

    if mode not in (
        "voice",
        "default",
    ):
        await callback.answer()
        return

    user_modes[key] = mode

    try:
        await callback.message.edit_reply_markup(
            reply_markup=mode_keyboard(
                mode
            )
        )
    except Exception:
        pass

    await callback.answer()


@dp.message(F.text)
async def text_handler(message):
    key = key_for(message)

    user_modes.setdefault(
        key,
        "default",
    )

    reply_indexes.setdefault(
        key,
        0,
    )

    text = message.text.strip()

    if (
        text.startswith("يوت")
        and len(text) > 3
        and text[3].isspace()
    ):
        query = text[3:].strip()

        if query:
            await message.answer(
                YOUTUBE_REPLY.format(
                    query=query
                ),
                reply_parameters=reply_params(
                    message
                ),
            )

            await add_job(
                message,
                lambda: youtube_download(
                    message,
                    query,
                ),
            )

            return

    url = extract_url(text)

    if url and not is_telegram_url(url):
        mode = user_modes[key]

        status = await message.answer(
            START_REPLY,
            reply_parameters=reply_params(
                message
            ),
        )

        accepted = await add_job(
            message,
            lambda: normal_download(
                message,
                url,
                mode,
                status,
            ),
        )

        if not accepted:
            try:
                await status.delete()
            except Exception:
                pass

        return

    index = reply_indexes[key]

    await message.answer(
        REPLIES[index],
        reply_parameters=reply_params(
            message
        ),
    )

    reply_indexes[key] = (
        index + 1
    ) % len(REPLIES)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())