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
    Message,
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

YOUTUBE_REPLY = (
    "ها تريد {query}\n"
    "تمام عبي"
)


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


user_modes = {}
reply_indexes = {}
mode_messages = {}
chat_queues = {}
running_tasks = set()


MAX_ACTIVE = 3
MAX_WAITING = 3


def state_key(message):
    user_id = (
        message.from_user.id
        if message.from_user
        else 0
    )

    return (
        message.chat.id,
        user_id,
    )


def reply_to(message):
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


def get_mode(key):
    return user_modes.get(
        key,
        "default",
    )


def get_mode_keyboard(key):
    mode = get_mode(key)

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
    key = state_key(message)

    old_message_id = mode_messages.get(key)

    if old_message_id:
        try:
            await bot.delete_message(
                chat_id=message.chat.id,
                message_id=old_message_id,
            )
        except Exception:
            pass

    sent = await message.answer(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=get_mode_keyboard(key),
        reply_parameters=reply_to(message),
    )

    mode_messages[key] = sent.message_id


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


def search_youtube(query):
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }

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

    url = result.get("webpage_url")

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


def download_audio(url, directory):
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(
            Path(directory)
            / "%(title)s.%(ext)s"
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
            Path(directory)
            / "%(title)s.%(ext)s"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(options) as ydl:
        ydl.download([url])

    return get_latest_file(directory)


async def convert_to_opus(
    source,
    directory,
):
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


async def process_voice(
    message,
    url,
):
    with tempfile.TemporaryDirectory() as directory:
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
            ),
            reply_parameters=reply_to(
                message
            ),
        )


async def process_video(
    message,
    url,
):
    with tempfile.TemporaryDirectory() as directory:
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
            reply_parameters=reply_to(
                message
            ),
        )


async def run_download(
    message,
    url,
    mode,
    status_message,
):
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

        try:
            await status_message.delete()
        except Exception:
            pass

    except Exception:
        try:
            await status_message.delete()
        except Exception:
            pass

        await message.answer(
            FAIL_REPLY,
            reply_parameters=reply_to(
                message
            ),
        )


async def youtube_job(
    message,
    query,
):
    try:
        url = await asyncio.to_thread(
            search_youtube,
            query,
        )

        status_message = await message.answer(
            START_REPLY,
            reply_parameters=reply_to(
                message
            ),
        )

        try:
            await process_voice(
                message,
                url,
            )

            try:
                await status_message.delete()
            except Exception:
                pass

        except Exception:
            try:
                await status_message.delete()
            except Exception:
                pass

            await message.answer(
                FAIL_REPLY,
                reply_parameters=reply_to(
                    message
                ),
            )

    except Exception:
        await message.answer(
            FAIL_REPLY,
            reply_parameters=reply_to(
                message
            ),
        )


async def worker(chat_id):
    queue = chat_queues[chat_id]

    while True:
        job = await queue["queue"].get()

        queue["waiting"] -= 1
        queue["active"] += 1

        try:
            await job()
        except Exception:
            pass
        finally:
            queue["active"] -= 1
            queue["queue"].task_done()


async def ensure_chat(chat_id):
    if chat_id not in chat_queues:
        chat_queues[chat_id] = {
            "active": 0,
            "waiting": 0,
            "queue": asyncio.Queue(),
            "workers": [],
        }

        for _ in range(MAX_ACTIVE):
            task = asyncio.create_task(
                worker(chat_id)
            )

            chat_queues[chat_id][
                "workers"
            ].append(task)


async def add_job(message, job):
    chat_id = message.chat.id

    await ensure_chat(chat_id)

    queue = chat_queues[chat_id]

    if (
        queue["active"]
        + queue["waiting"]
        >= MAX_ACTIVE + MAX_WAITING
    ):
        return False

    queue["waiting"] += 1
    await queue["queue"].put(job)

    return True


@dp.message(CommandStart())
async def start_handler(message):
    key = state_key(message)

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
    key = state_key(message)

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

    user_modes[key] = selected_mode

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_mode_keyboard(
                key
            )
        )
    except Exception:
        pass

    await callback.answer()


@dp.message(F.text)
async def text_handler(message):
    key = state_key(message)

    user_modes.setdefault(
        key,
        "default",
    )

    reply_indexes.setdefault(
        key,
        0,
    )

    text = message.text or ""
    stripped = text.strip()

    if (
        stripped.startswith("يوت")
        and len(stripped) > 3
        and stripped[3].isspace()
    ):
        query = stripped[3:].strip()

        if query:
            await message.answer(
                YOUTUBE_REPLY.format(
                    query=query
                ),
                reply_parameters=reply_to(
                    message
                ),
            )

            await add_job(
                message,
                lambda: youtube_job(
                    message,
                    query,
                ),
            )

            return

    url = extract_url(text)

    if url and not is_telegram_url(url):
        mode = get_mode(key)

        status_message = await message.answer(
            START_REPLY,
            reply_parameters=reply_to(
                message
            ),
        )

        accepted = await add_job(
            message,
            lambda: run_download(
                message,
                url,
                mode,
                status_message,
            ),
        )

        if not accepted:
            try:
                await status_message.delete()
            except Exception:
                pass

        return

    index = reply_indexes[key]

    await message.answer(
        REPLIES[index],
        reply_parameters=reply_to(
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