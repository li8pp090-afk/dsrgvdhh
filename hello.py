import os
import re
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
    KeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = Bot(TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DATA_FILE = Path("data.json")

DEVELOPER_ID = 8436425159

ADMINS = {
    8750024481,
    8554632449,
    8845740736,
    8606430342,
    8800673233,
    8255680206,
    8436425159
}

DEFAULT_NAME = "تع"
DEFAULT_URL = f"tg://user?id={DEVELOPER_ID}"

MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024
MAX_URL_LENGTH = 4096

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


def chat_data(chat_id):
    key = str(chat_id)

    if key not in settings:
        settings[key] = {
            "name": {
                "id_file": new_id(),
                "value": DEFAULT_NAME
            },
            "url": {
                "id_file": new_id(),
                "value": DEFAULT_URL
            },
            "media": None,
            "downloads": {}
        }
        save_data()

    data = settings[key]

    data.setdefault(
        "name",
        {
            "id_file": new_id(),
            "value": DEFAULT_NAME
        }
    )

    data.setdefault(
        "url",
        {
            "id_file": new_id(),
            "value": DEFAULT_URL
        }
    )

    data.setdefault("media", None)
    data.setdefault("downloads", {})

    return data


def is_admin(user_id):
    return user_id in ADMINS


def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="تغيير اسم الزر"
                ),
                KeyboardButton(
                    text="تعيين رابط الزر"
                )
            ],
            [
                KeyboardButton(
                    text="تعيين ميديا الرد"
                )
            ]
        ],
        resize_keyboard=True
    )


def result_keyboard(chat_id):
    data = chat_data(chat_id)

    name = data["name"]["value"] or DEFAULT_NAME
    url = data["url"]["value"] or DEFAULT_URL

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="المطور",
                    url=f"tg://user?id={DEVELOPER_ID}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="اضفني",
                    url=bot_add_link
                ),
                InlineKeyboardButton(
                    text=name,
                    url=url
                )
            ]
        ]
    )


def valid_username(value):
    value = value.strip()

    if value.startswith("@"):
        value = value[1:]

    if not 5 <= len(value) <= 32:
        return False

    if not re.fullmatch(
        r"[A-Za-z0-9_]+",
        value
    ):
        return False

    if value[0].isdigit():
        return False

    if value[0] == "_":
        return False

    if value[-1] == "_":
        return False

    return True


def valid_user_id(value):
    if not value.isdigit():
        return False

    try:
        number = int(value)
    except Exception:
        return False

    return 1 <= number <= 999999999999999


def parse_button_url(value):
    value = value.strip()

    if not value or len(value) > MAX_URL_LENGTH:
        return None

    if valid_user_id(value):
        return f"tg://user?id={value}"

    if valid_username(value):
        username = value.lstrip("@")
        return f"https://t.me/{username}"

    match = re.fullmatch(
        r"https?://t\.me/([A-Za-z0-9_]{5,32})/?",
        value,
        re.IGNORECASE
    )

    if match and valid_username(
        match.group(1)
    ):
        return f"https://t.me/{match.group(1)}"

    return None


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

    if host == "t.me" or host.endswith(".t.me"):
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
            if item.is_file() or item.is_symlink():
                try:
                    item.unlink()
                except Exception:
                    pass

            elif item.is_dir():
                await cleanup(item)

        try:
            folder.rmdir()
        except Exception:
            pass

    except Exception:
        pass


async def youtube_search(query):
    if not query or len(query) > 300:
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


async def live_typing(
    message,
    words,
    stop_event
):
    if not words:
        return

    current = []
    index = 0
    total = len(words)

    while not stop_event.is_set():
        count = random.randint(3, 6)

        for _ in range(count):
            if index >= total:
                index = 0

            current.append(words[index])
            index += 1

        text = " ".join(current)

        if len(text) > 3900:
            current = current[-30:]
            text = " ".join(current)

        try:
            await message.edit_text(
                text
            )
        except Exception:
            pass

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=0.3
            )
        except asyncio.TimeoutError:
            pass


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
            not in {".part", ".ytdl"}
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
    status_message = item["message"]

    folder = Path(
        tempfile.mkdtemp(
            prefix="voice_"
        )
    )

    stop_event = asyncio.Event()

    words = (
        status_message.text or ""
    ).replace(
        "\n",
        " "
    ).split()

    typing_task = asyncio.create_task(
        live_typing(
            status_message,
            words,
            stop_event
        )
    )

    try:
        output = await download_voice(
            url,
            folder
        )

        stop_event.set()

        await typing_task

        try:
            await status_message.delete()
        except Exception:
            pass

        sent = await bot.send_voice(
            chat_id=chat_id,
            voice=FSInputFile(output),
            reply_markup=result_keyboard(
                chat_id
            )
        )

        file_id = None

        if sent.voice:
            file_id = sent.voice.file_id

        async with state_lock:
            data = chat_data(chat_id)

            id_file = new_id()

            data["downloads"][id_file] = {
                "id_file": id_file,
                "type": source_type,
                "query": query,
                "url": url,
                "telegram_file_id": file_id
            }

            save_data()

    except Exception:
        stop_event.set()

        try:
            await typing_task
        except Exception:
            pass

        try:
            await status_message.edit_text(
                failure_text(
                    source_type
                )
            )
        except Exception:
            pass

    finally:
        stop_event.set()

        if not typing_task.done():
            typing_task.cancel()

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
    worker_task = workers.get(chat_id)

    if (
        worker_task
        and not worker_task.done()
    ):
        return

    workers[chat_id] = asyncio.create_task(
        worker(chat_id)
    )


async def add_download(
    chat_id,
    url,
    query,
    source_type,
    status_message
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
                "message": status_message
            }
        )

        await start_worker(
            chat_id
        )

        return True


async def send_saved_media(message):
    data = chat_data(
        message.chat.id
    )

    media = data.get("media")

    if not media:
        return

    file_id = media.get("file_id")
    kind = media.get("type")

    if not file_id:
        return

    markup = result_keyboard(
        message.chat.id
    )

    try:
        if kind == "voice":
            await message.answer_voice(
                voice=file_id,
                reply_markup=markup
            )

        elif kind == "photo":
            await message.answer_photo(
                photo=file_id,
                reply_markup=markup
            )

        elif kind == "video":
            await message.answer_video(
                video=file_id,
                reply_markup=markup
            )

        elif kind == "animation":
            await message.answer_animation(
                animation=file_id,
                reply_markup=markup
            )

    except Exception:
        pass


@router.message(Command("ادت"))
@router.message(F.text == "ادت")
async def edit_command(message: Message):
    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    await message.answer(
        "",
        reply_markup=admin_keyboard()
    )


@router.message(F.text == "تغيير اسم الزر")
async def change_name(message: Message):
    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    data = chat_data(
        message.chat.id
    )

    name = data["name"]["value"]

    waiting[
        message.from_user.id
    ] = {
        "type": "name",
        "chat_id": message.chat.id
    }

    await message.answer(
        f"تريد تغير اسم زر {name}\n"
        "انطيني يلا"
    )


@router.message(F.text == "تعيين رابط الزر")
async def change_url(message: Message):
    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    data = chat_data(
        message.chat.id
    )

    name = data["name"]["value"]

    waiting[
        message.from_user.id
    ] = {
        "type": "url",
        "chat_id": message.chat.id
    }

    await message.answer(
        f"تريد تعين رابط زر {name}\n"
        "انطيني يلا"
    )


@router.message(F.text == "تعيين ميديا الرد")
async def set_media(message: Message):
    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    waiting[
        message.from_user.id
    ] = {
        "type": "media",
        "chat_id": message.chat.id
    }

    await message.answer(
        "أرسل نوع من أنواع الوسائط\n"
        "فويس / صورة / فيديو / ستيكر"
    )


@router.message(Command("تعطيل_اليوت"))
@router.message(F.text == "تعطيل اليوت")
async def disable_youtube(message: Message):
    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    settings.setdefault(
        "_global",
        {}
    )

    settings[
        "_global"
    ]["private_youtube"] = False

    save_data()


@router.message(Command("تفعيل_اليوت"))
@router.message(F.text == "تفعيل اليوت")
async def enable_youtube(message: Message):
    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    settings.setdefault(
        "_global",
        {}
    )

    settings[
        "_global"
    ]["private_youtube"] = True

    save_data()


async def handle_message(message: Message):
    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    if not message.from_user:
        return

    user_id = message.from_user.id

    task = waiting.get(user_id)

    if (
        task
        and task["chat_id"]
        == message.chat.id
    ):
        if not is_admin(user_id):
            waiting.pop(
                user_id,
                None
            )
            return

        if task["type"] == "name":
            waiting.pop(
                user_id,
                None
            )

            if not message.text:
                return

            name = message.text.strip()

            if not name or len(name) > 64:
                return

            data = chat_data(
                message.chat.id
            )

            data["name"] = {
                "id_file": new_id(),
                "value": name
            }

            save_data()
            return

        if task["type"] == "url":
            waiting.pop(
                user_id,
                None
            )

            value = (
                message.text or ""
            ).strip()

            url = parse_button_url(
                value
            )

            if not url:
                await message.answer(
                    "اهو فطستني بسوالفك هاي ديلا دز يوزر لو ايدي لو رابط اريد\n"
                    "انفذلك طلباتك علمود انام"
                )
                return

            data = chat_data(
                message.chat.id
            )

            data["url"] = {
                "id_file": new_id(),
                "value": url
            }

            save_data()
            return

        if task["type"] == "media":
            waiting.pop(
                user_id,
                None
            )

            media = None

            if message.voice:
                media = {
                    "id_file": new_id(),
                    "type": "voice",
                    "file_id":
                        message.voice.file_id
                }

            elif message.photo:
                media = {
                    "id_file": new_id(),
                    "type": "photo",
                    "file_id":
                        message.photo[-1].file_id
                }

            elif message.video:
                media = {
                    "id_file": new_id(),
                    "type": "video",
                    "file_id":
                        message.video.file_id
                }

            elif message.animation:
                media = {
                    "id_file": new_id(),
                    "type": "animation",
                    "file_id":
                        message.animation.file_id
                }

            if media:
                data = chat_data(
                    message.chat.id
                )

                data["media"] = media
                save_data()

            return

    if (
        message.text
        and message.text.startswith("يوت")
    ):
        query = message.text[3:].strip()

        if not query:
            return

        try:
            status = await message.answer(
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
                status
            )

            if not accepted:
                await status.edit_text(
                    failure_text(
                        "youtube"
                    )
                )

        except Exception:
            try:
                await message.answer(
                    failure_text(
                        "youtube"
                    )
                )
            except Exception:
                pass

        return

    if (
        message.text
        and message.text.startswith(
            (
                "http://",
                "https://"
            )
        )
    ):
        url = message.text.strip()

        if not valid_download_url(url):
            await message.answer(
                failure_text(
                    "url"
                )
            )
            return

        try:
            status = await message.answer(
                "بدأت بالعثور ع طلبك امهلني\n"
                "قليلا فضلا وليس امرا"
            )

            accepted = await add_download(
                message.chat.id,
                url,
                url,
                "url",
                status
            )

            if not accepted:
                await status.edit_text(
                    failure_text(
                        "url"
                    )
                )

        except Exception:
            try:
                await message.answer(
                    failure_text(
                        "url"
                    )
                )
            except Exception:
                pass

        return

    await send_saved_media(
        message
    )


@router.message()
async def group_handler(message: Message):
    await handle_message(message)


@router.channel_post()
async def channel_handler(message: Message):
    if (
        message.text
        and message.text.startswith("يوت")
    ):
        query = message.text[3:].strip()

        if not query:
            return

        try:
            status = await message.answer(
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
                status
            )

            if not accepted:
                await status.edit_text(
                    failure_text(
                        "youtube"
                    )
                )

        except Exception:
            try:
                await message.answer(
                    failure_text(
                        "youtube"
                    )
                )
            except Exception:
                pass

        return

    if (
        message.text
        and message.text.startswith(
            (
                "http://",
                "https://"
            )
        )
    ):
        url = message.text.strip()

        if not valid_download_url(url):
            await message.answer(
                failure_text(
                    "url"
                )
            )
            return

        try:
            status = await message.answer(
                "بدأت بالعثور ع طلبك امهلني\n"
                "قليلا فضلا وليس امرا"
            )

            accepted = await add_download(
                message.chat.id,
                url,
                url,
                "url",
                status
            )

            if not accepted:
                await status.edit_text(
                    failure_text(
                        "url"
                    )
                )

        except Exception:
            try:
                await message.answer(
                    failure_text(
                        "url"
                    )
                )
            except Exception:
                pass

        return

    await send_saved_media(
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