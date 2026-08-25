import os
import json
import uuid
import random
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.enums import ChatType
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyParameters
)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = Bot(TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DATA_FILE = Path("data.json")

OWNER_ID = 8436425159

ADMINS = {
    8750024481,
    8554632449,
    8845740736,
    8606430342,
    8800673233,
    8255680206,
    8436425159
}

MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024
MAX_URL_LENGTH = 4096
MAX_QUERY_LENGTH = 300

settings = {}
waiting = {}

queues = {}
workers = {}
running = {}

state_lock = asyncio.Lock()

bot_add_link = ""


def new_id():
    return "ID-File-" + uuid.uuid4().hex


def load_data():
    global settings

    if not DATA_FILE.exists():
        settings = {}
        return

    try:
        settings = json.loads(
            DATA_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        settings = {}


def save_data():
    tmp = DATA_FILE.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            settings,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    tmp.replace(DATA_FILE)


def is_admin(user_id):
    return user_id in ADMINS


def is_allowed_chat(message):
    return message.chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
        ChatType.CHANNEL
    )


def owner_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="المالك",
                    url=f"tg://user?id={OWNER_ID}"
                )
            ]
        ]
    )


def reply_parameters(message):
    return ReplyParameters(
        message_id=message.message_id
    )


def valid_download_url(value):
    if not value:
        return False

    if len(value) > MAX_URL_LENGTH:
        return False

    try:
        parsed = urlparse(value)
    except Exception:
        return False

    if parsed.scheme.lower() not in {
        "http",
        "https"
    }:
        return False

    host = parsed.hostname

    if not host:
        return False

    host = host.lower()

    if (
        host == "t.me"
        or host.endswith(".t.me")
        or host == "telegram.me"
        or host.endswith(".telegram.me")
    ):
        return False

    return True


def failure_text(kind):
    ending = random.choice(
        (
            "شم كسي يلا",
            "شم طيزي يلا"
        )
    )

    if kind == "youtube":
        return (
            "اليوت غير مدعوم او العنوان غير متوفر\n"
            f"{ending}"
        )

    return (
        "الرابط غير مدعوم او الرابط غير مدعوم\n"
        f"{ending}"
    )


async def cleanup(folder):
    try:
        if not folder.exists():
            return

        for item in folder.iterdir():
            try:
                if item.is_file() or item.is_symlink():
                    item.unlink()
                elif item.is_dir():
                    await cleanup(item)
            except Exception:
                pass

        try:
            folder.rmdir()
        except Exception:
            pass

    except Exception:
        pass


async def youtube_search(query):
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise RuntimeError()

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "socket_timeout": 20
    }

    def search():
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(
                f"ytsearch1:{query}",
                download=False
            )

        entries = result.get("entries") or []

        if not entries:
            raise RuntimeError()

        entry = entries[0]

        url = (
            entry.get("webpage_url")
            or entry.get("url")
        )

        if not url:
            raise RuntimeError()

        return url

    return await asyncio.to_thread(search)


async def download_voice(url, folder):
    source = folder / "source.%(ext)s"
    output = folder / "voice.ogg"

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(source),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "max_filesize": MAX_DOWNLOAD_SIZE,
        "continuedl": False,
        "overwrites": True
    }

    def download():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )

            return Path(
                ydl.prepare_filename(info)
            )

    source_file = await asyncio.to_thread(
        download
    )

    if not source_file.exists():
        candidates = [
            x for x in folder.iterdir()
            if x.is_file()
            and x.suffix.lower()
            not in {
                ".part",
                ".ytdl"
            }
        ]

        if not candidates:
            raise RuntimeError()

        source_file = candidates[0]

    if source_file.stat().st_size > MAX_DOWNLOAD_SIZE:
        raise RuntimeError()

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_file),
        "-vn",
        "-c:a",
        "libopus",
        "-vbr",
        "on",
        "-application",
        "audio",
        "-f",
        "ogg",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    await process.wait()

    if process.returncode != 0:
        raise RuntimeError()

    if not output.exists():
        raise RuntimeError()

    if output.stat().st_size > MAX_DOWNLOAD_SIZE:
        raise RuntimeError()

    return output


async def process_download(item):
    chat_id = item["chat_id"]
    url = item["url"]
    query = item["query"]
    source_type = item["type"]
    status = item["status"]
    source_message = item["source_message"]

    folder = Path(
        tempfile.mkdtemp(
            prefix="voice_"
        )
    )

    try:
        output = await download_voice(
            url,
            folder
        )

        try:
            await status.delete()
        except Exception:
            pass

        sent = await bot.send_voice(
            chat_id=chat_id,
            voice=FSInputFile(output),
            reply_parameters=reply_parameters(
                source_message
            ),
            reply_markup=owner_keyboard()
        )

        file_id = None

        if sent.voice:
            file_id = sent.voice.file_id

        async with state_lock:
            key = str(chat_id)

            if key not in settings:
                settings[key] = {}

            downloads = settings[key].setdefault(
                "downloads",
                {}
            )

            id_file = new_id()

            downloads[id_file] = {
                "id_file": id_file,
                "type": source_type,
                "query": query,
                "url": url,
                "telegram_file_id": file_id
            }

            save_data()

    except Exception:
        try:
            await status.edit_text(
                failure_text(
                    source_type
                ),
                reply_markup=None
            )
        except Exception:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=failure_text(
                        source_type
                    ),
                    reply_parameters=reply_parameters(
                        source_message
                    )
                )
            except Exception:
                pass

    finally:
        await cleanup(folder)


async def worker(chat_id):
    try:
        while True:
            async with state_lock:
                queue = queues.get(
                    chat_id,
                    []
                )

                if not queue:
                    workers.pop(
                        chat_id,
                        None
                    )
                    return

                item = queue.pop(0)

                running[chat_id] = (
                    running.get(
                        chat_id,
                        0
                    ) + 1
                )

            try:
                await process_download(
                    item
                )

            finally:
                async with state_lock:
                    current = (
                        running.get(
                            chat_id,
                            0
                        ) - 1
                    )

                    if current <= 0:
                        running.pop(
                            chat_id,
                            None
                        )
                    else:
                        running[chat_id] = current

    except asyncio.CancelledError:
        raise

    except Exception:
        workers.pop(
            chat_id,
            None
        )


async def start_worker(chat_id):
    task = workers.get(chat_id)

    if task and not task.done():
        return

    workers[chat_id] = asyncio.create_task(
        worker(chat_id)
    )


async def add_download(
    chat_id,
    url,
    query,
    source_type,
    status,
    source_message
):
    async with state_lock:
        active = running.get(
            chat_id,
            0
        )

        queued = len(
            queues.get(
                chat_id,
                []
            )
        )

        if active >= 3 and queued >= 3:
            return False

        if active + queued >= 6:
            return False

        queues.setdefault(
            chat_id,
            []
        ).append(
            {
                "chat_id": chat_id,
                "url": url,
                "query": query,
                "type": source_type,
                "status": status,
                "source_message": source_message
            }
        )

        await start_worker(
            chat_id
        )

        return True


async def process_text_message(message):
    if not is_allowed_chat(message):
        return

    if not message.text:
        return

    text = message.text.strip()

    if text.startswith("يوت"):
        query = text[3:].strip()

        if not query:
            return

        try:
            status = await message.reply(
                f"¹# - بدأت بالعثور ع {query} امهلني\n"
                "قليلا فضلا وليس امرا"
            )

            url = await youtube_search(
                query
            )

            accepted = await add_download(
                message.chat.id,
                url,
                query,
                "youtube",
                status,
                message
            )

            if not accepted:
                await status.edit_text(
                    failure_text(
                        "youtube"
                    )
                )

        except Exception:
            try:
                await message.reply(
                    failure_text(
                        "youtube"
                    )
                )
            except Exception:
                pass

        return

    if text.startswith(
        (
            "http://",
            "https://"
        )
    ):
        url = text

        if not valid_download_url(url):
            await message.reply(
                failure_text(
                    "url"
                )
            )
            return

        try:
            status = await message.reply(
                "بدأت بالعثور ع طلبك امهلني\n"
                "قليلا فضلا وليس امرا"
            )

            accepted = await add_download(
                message.chat.id,
                url,
                url,
                "url",
                status,
                message
            )

            if not accepted:
                await status.edit_text(
                    failure_text(
                        "url"
                    )
                )

        except Exception:
            try:
                await message.reply(
                    failure_text(
                        "url"
                    )
                )
            except Exception:
                pass


@router.message(Command("ادت"))
@router.message(F.text == "ادت")
async def edit_command(message: Message):
    if not is_allowed_chat(message):
        return

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    await message.reply(
        "الأوامر متاحة للمطور فقط"
    )


@router.message(Command("تعطيل_اليوت"))
@router.message(F.text == "تعطيل اليوت")
async def disable_youtube(message: Message):
    if not is_allowed_chat(message):
        return

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    global settings

    global_data = settings.setdefault(
        "_global",
        {}
    )

    if global_data.get(
        "private_youtube",
        True
    ) is False:
        await message.reply(
            "تم تعطيل اليوت\n"
            "بالفعل"
        )
        return

    global_data[
        "private_youtube"
    ] = False

    save_data()

    await message.reply(
        "تم تعطيل اليوت\n"
        "مولاي"
    )


@router.message(Command("تفعيل_اليوت"))
@router.message(F.text == "تفعيل اليوت")
async def enable_youtube(message: Message):
    if not is_allowed_chat(message):
        return

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    global_data = settings.setdefault(
        "_global",
        {}
    )

    global_data[
        "private_youtube"
    ] = True

    save_data()

    await message.reply(
        "تم تفعيل اليوت"
    )


@router.message()
async def message_handler(message: Message):
    if not is_allowed_chat(message):
        return

    if message.from_user:
        user_id = message.from_user.id

        task = waiting.get(user_id)

        if task:
            waiting.pop(
                user_id,
                None
            )

    await process_text_message(
        message
    )


@router.channel_post()
async def channel_handler(message: Message):
    if message.chat.type != ChatType.CHANNEL:
        return

    await process_text_message(
        message
    )


async def startup_message():
    for user_id in ADMINS:
        try:
            await bot.send_message(
                user_id,
                "اشتغل البوت مرتلخ\n"
                "استعملني ؟!"
            )
        except Exception:
            pass


async def main():
    global bot_add_link

    load_data()

    me = await bot.get_me()

    if me.username:
        bot_add_link = (
            f"https://t.me/{me.username}"
            "?startgroup=true"
        )

    await startup_message()

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "channel_post"
        ]
    )


if __name__ == "__main__":
    asyncio.run(main())