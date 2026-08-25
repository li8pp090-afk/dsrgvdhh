import os
import json
import uuid
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode

import yt_dlp

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.types import (
    Message,
    FSInputFile,
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

MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024
MAX_URL_LENGTH = 4096

settings = {}
queues = {}
workers = {}
running = {}

state_lock = asyncio.Lock()


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

    try:
        tmp.write_text(
            json.dumps(
                settings,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )
        tmp.replace(DATA_FILE)
    except Exception:
        pass


def is_admin(user_id):
    return user_id in ADMINS


async def check_chat_admin(message: Message) -> bool:
    if not message.from_user:
        return True

    if is_admin(message.from_user.id):
        return True

    try:
        member = await bot.get_chat_member(
            chat_id=message.chat.id,
            user_id=message.from_user.id
        )

        if member.status in (
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR
        ):
            return True

    except Exception:
        pass

    return False


def is_youtube_enabled(chat_id: int) -> bool:
    key = str(chat_id)
    chat_setting = settings.get(key, {})
    return chat_setting.get("youtube", True)


def set_youtube_enabled(chat_id: int, state: bool):
    key = str(chat_id)
    settings.setdefault(key, {})
    settings[key]["youtube"] = state
    save_data()


def reply_parameters(message):
    return ReplyParameters(
        message_id=message.message_id
    )


def failure_text(kind):
    if kind == "youtube":
        return (
            "اليوت غير مدعوم او العنوان غير متوفر\n"
            "شم كسي يلا"
        )

    return (
        "الرابط غير مدعوم او الموقع غير مدعوم\n"
        "شم كسي يلا"
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

    if host == "t.me" or host.endswith(".t.me"):
        return False

    return True


def clean_youtube_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if "youtube.com" in host or "youtu.be" in host:
            if "youtu.be" in host:
                vid = parsed.path.lstrip("/")
                if vid:
                    return f"https://www.youtube.com/watch?v={vid}"

            qs = parse_qs(parsed.query)
            if "v" in qs:
                new_query = urlencode({"v": qs["v"][0]})
                return urlunparse((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    new_query,
                    ""
                ))
    except Exception:
        pass

    return url


def yt_options(outtmpl=None):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnails": False,
        "addmetadata": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": False,
        "overwrites": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"]
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/139.0.0.0 "
                "Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9"
        }
    }

    if outtmpl:
        options["outtmpl"] = str(outtmpl)

    return options


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

    options = yt_options()
    options["extract_flat"] = True

    def search():
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(
                f"ytsearch1:{query}",
                download=False
            )

        if not result:
            raise RuntimeError()

        entries = result.get("entries") or []

        if not entries:
            raise RuntimeError()

        entry = entries[0]

        url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
        )

        if not url:
            raise RuntimeError()

        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"

        return url

    return await asyncio.wait_for(
        asyncio.to_thread(search),
        timeout=30
    )


async def download_voice(url, folder):
    clean_url = clean_youtube_url(url)
    source = folder / "source.%(ext)s"

    options = yt_options(source)

    options.update({
        "format": "bestaudio/best",
        "max_filesize": MAX_DOWNLOAD_SIZE
    })

    def download():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                clean_url,
                download=True
            )

            filename = ydl.prepare_filename(info)

            return Path(filename)

    source_file = await asyncio.wait_for(
        asyncio.to_thread(download),
        timeout=180
    )

    if not source_file.exists():
        candidates = [
            x
            for x in folder.iterdir()
            if (
                x.is_file()
                and x.suffix.lower()
                not in {
                    ".part",
                    ".ytdl"
                }
            )
        ]

        if not candidates:
            raise RuntimeError()

        source_file = candidates[0]

    if source_file.stat().st_size > MAX_DOWNLOAD_SIZE:
        raise RuntimeError()

    output = folder / "voice.ogg"

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_file),
        "-vn",
        "-map_metadata",
        "-1",
        "-c:a",
        "libopus",
        "-b:a",
        "128k",
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
    source_type = item["type"]
    original_message = item["original_message"]
    status_message = item["status_message"]

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

        await bot.send_voice(
            chat_id=chat_id,
            voice=FSInputFile(output),
            reply_parameters=reply_parameters(
                original_message
            )
        )

        try:
            await status_message.delete()
        except Exception:
            pass

        async with state_lock:
            key = str(chat_id)

            settings.setdefault(
                key,
                {}
            )

            settings[key].setdefault(
                "downloads",
                {}
            )

            id_file = new_id()

            settings[key]["downloads"][id_file] = {
                "id_file": id_file,
                "type": source_type,
                "url": url
            }

            save_data()

    except Exception:
        try:
            await status_message.edit_text(
                failure_text(source_type)
            )
        except Exception:
            try:
                await original_message.answer(
                    failure_text(source_type),
                    reply_parameters=reply_parameters(
                        original_message
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
                    running.get(chat_id, 0) + 1
                )

            try:
                await process_download(item)
            finally:
                async with state_lock:
                    current = (
                        running.get(chat_id, 0) - 1
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
    source_type,
    original_message,
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

        if active + queued >= 6:
            return False

        queues.setdefault(
            chat_id,
            []
        ).append({
            "chat_id": chat_id,
            "url": url,
            "type": source_type,
            "original_message": original_message,
            "status_message": status_message
        })

        await start_worker(chat_id)

        return True


async def handle_download(
    message,
    url,
    source_type
):
    try:
        status = await message.answer(
            "بدأت بالعثور ع طلبك امهلني\n"
            "قليلا فضلا وليس امرا",
            reply_parameters=reply_parameters(
                message
            )
        )

        accepted = await add_download(
            message.chat.id,
            url,
            source_type,
            message,
            status
        )

        if not accepted:
            await status.edit_text(
                failure_text(source_type)
            )

    except Exception:
        try:
            await message.answer(
                failure_text(source_type),
                reply_parameters=reply_parameters(
                    message
                )
            )
        except Exception:
            pass


async def handle_youtube(
    message,
    query
):
    if not is_youtube_enabled(message.chat.id):
        return

    status = None
    try:
        status = await message.answer(
            f"¹# - بدأت بالعثور ع {query} امهلني\n"
            "قليلا فضلا وليس امرا",
            reply_parameters=reply_parameters(
                message
            )
        )

        url = await youtube_search(query)

        accepted = await add_download(
            message.chat.id,
            url,
            "youtube",
            message,
            status
        )

        if not accepted:
            await status.edit_text(
                failure_text("youtube")
            )

    except Exception:
        if status:
            try:
                await status.edit_text(
                    failure_text("youtube")
                )
            except Exception:
                pass
        else:
            try:
                await message.answer(
                    failure_text("youtube"),
                    reply_parameters=reply_parameters(
                        message
                    )
                )
            except Exception:
                pass


@router.message(Command("تعطيل_اليوت"))
@router.message(F.text == "تعطيل اليوت")
async def disable_youtube(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        return

    if not await check_chat_admin(message):
        return

    current = is_youtube_enabled(message.chat.id)

    if current is False:
        text = (
            "تم تعطيل اليوت\n"
            "بالفعل"
        )
    else:
        set_youtube_enabled(message.chat.id, False)

        text = (
            "تم تعطيل اليوت\n"
            "مولاي"
        )

    await message.answer(
        text,
        reply_parameters=reply_parameters(
            message
        )
    )


@router.message(Command("تفعيل_اليوت"))
@router.message(F.text == "تفعيل اليوت")
async def enable_youtube(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        return

    if not await check_chat_admin(message):
        return

    current = is_youtube_enabled(message.chat.id)

    if current is True:
        text = (
            "تم تفعيل اليوت\n"
            "بالفعل"
        )
    else:
        set_youtube_enabled(message.chat.id, True)

        text = (
            "تم تفعيل اليوت\n"
            "مولاي"
        )

    await message.answer(
        text,
        reply_parameters=reply_parameters(
            message
        )
    )


@router.message()
async def group_handler(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        return

    if message.chat.type != ChatType.GROUP and \
       message.chat.type != ChatType.SUPERGROUP:
        return

    if not message.text:
        return

    text = message.text.strip()

    if text.startswith("يوت"):
        query = text[3:].strip()

        if not query:
            return

        await handle_youtube(
            message,
            query
        )

        return

    if text.startswith(
        (
            "http://",
            "https://"
        )
    ):
        if not valid_download_url(text):
            await message.answer(
                failure_text("url"),
                reply_parameters=reply_parameters(
                    message
                )
            )
            return

        await handle_download(
            message,
            text,
            "url"
        )


@router.channel_post()
async def channel_handler(message: Message):
    if message.chat.type != ChatType.CHANNEL:
        return

    if not message.text:
        return

    text = message.text.strip()

    if text in ("تعطيل اليوت", "/تعطيل_اليوت"):
        current = is_youtube_enabled(message.chat.id)
        if current is False:
            text_res = "تم تعطيل اليوت\nبالفعل"
        else:
            set_youtube_enabled(message.chat.id, False)
            text_res = "تم تعطيل اليوت\nمولاي"

        await message.answer(
            text_res,
            reply_parameters=reply_parameters(message)
        )
        return

    if text in ("تفعيل اليوت", "/تفعيل_اليوت"):
        current = is_youtube_enabled(message.chat.id)
        if current is True:
            text_res = "تم تفعيل اليوت\nبالفعل"
        else:
            set_youtube_enabled(message.chat.id, True)
            text_res = "تم تفعيل اليوت\nمولاي"

        await message.answer(
            text_res,
            reply_parameters=reply_parameters(message)
        )
        return

    if text.startswith("يوت"):
        query = text[3:].strip()

        if not query:
            return

        if not is_youtube_enabled(message.chat.id):
            return

        await handle_youtube(
            message,
            query
        )

        return

    if text.startswith(
        (
            "http://",
            "https://"
        )
    ):
        if not valid_download_url(text):
            await message.answer(
                failure_text("url"),
                reply_parameters=reply_parameters(
                    message
                )
            )
            return

        await handle_download(
            message,
            text,
            "url"
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
    load_data()
    await startup_message()

    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "channel_post"
            ]
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
