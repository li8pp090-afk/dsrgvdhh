import os
import re
import json
import uuid
import shutil
import sqlite3
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from difflib import SequenceMatcher

import yt_dlp
from youtubesearchpython import VideosSearch

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

BASE_DIR = Path("data")
TEMP_DIR = BASE_DIR / "temp"
VOICE_DIR = BASE_DIR / "voice"
DEFAULT_DIR = BASE_DIR / "default"
DB_PATH = BASE_DIR / "bot.db"

BASE_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)
VOICE_DIR.mkdir(exist_ok=True)
DEFAULT_DIR.mkdir(exist_ok=True)

router = Router()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS id_files (
    category TEXT NOT NULL,
    mode TEXT NOT NULL,
    file_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (category, mode, file_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS chat_modes (
    chat_key TEXT PRIMARY KEY,
    mode TEXT NOT NULL
)
""")
db.commit()

db_lock = asyncio.Lock()
state_lock = asyncio.Lock()
download_slots = asyncio.Semaphore(3)

waiting_count = 0
waiting_lock = asyncio.Lock()

user_rotations = {}

DEFAULT_SUCCESS = [
    "تم التحميل\nجاهز",
    "تم التحميل\nتفضل",
    "جاهز\nتم التحميل",
    "تفضل\nجاهز"
]

NORMAL_REPLIES = [
    "أهلين وسهلين\nأستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nوالفيديو أرسلكياه",
    "استخدم البوت للروابط\nوخلك على التحميل",
    "دز الرابط وأنا أتعامل وياه\nبسرعة"
]

URL_START = "جاري التحميل\nانتظر"
URL_FAIL = "الرابط غير مدعوم أو الموقع مو راضي يتعاون\nحاول مرة ثانية"

YT_START = "ها تريد {title}\nتمام عبي"
YT_FAIL = "الرابط غير مدعوم أو اليوتيوب مو راضي يتعاون\nحاول مرة ثانية"


def chat_key(message: Message) -> str:
    topic = getattr(message, "message_thread_id", None)
    return f"{message.chat.id}:{topic or 0}"


def clean_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^\w\s.\-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*-\s*", " - ", value)
    value = re.sub(r"\.{2,}", ".", value)
    value = value.strip(" .-")

    allowed_upper = set("ATFNMJULG")

    result = []
    for char in value:
        if "a" <= char.lower() <= "z":
            result.append(char.upper() if char.upper() in allowed_upper else char.lower())
        else:
            result.append(char)

    return "".join(result)


def get_extension(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix[1:] if suffix else "bin"


def make_filename(publisher: str, title: str, extension: str) -> str:
    publisher = clean_name(publisher)
    title = clean_name(title)

    if publisher and title:
        name = f"{publisher} - {title}"
    elif title:
        name = title
    elif publisher:
        name = publisher
    else:
        name = "media"

    return f"{name}.{extension}"


def extract_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s]+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,!?)]}")


def is_telegram_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return host == "t.me" or host.endswith(".t.me") or host == "telegram.me" or host.endswith(".telegram.me")
    except Exception:
        return False


def is_youtube_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return (
            host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtu.be"
            or host.endswith(".youtu.be")
        )
    except Exception:
        return False


def similarity(a: str, b: str) -> float:
    a = re.sub(r"[^\w\s]", " ", a.lower())
    b = re.sub(r"[^\w\s]", " ", b.lower())

    direct = SequenceMatcher(None, a, b).ratio()

    aw = set(a.split())
    bw = set(b.split())

    if not aw or not bw:
        overlap = 0
    else:
        overlap = len(aw & bw) / max(len(aw), len(bw))

    return direct * 0.7 + overlap * 0.3


async def youtube_search(title: str):
    def run():
        result = VideosSearch(title, limit=3).result()
        return result.get("result", [])

    return await asyncio.to_thread(run)


async def select_youtube_result(title: str):
    results = await youtube_search(title)

    if not results:
        return None

    best = None
    best_score = -1

    for item in results[:3]:
        item_title = item.get("title", "")
        score = similarity(title, item_title)

        if score > best_score:
            best_score = score
            best = item

    return best


async def get_mode(message: Message) -> str:
    key = chat_key(message)

    async with db_lock:
        row = db.execute(
            "SELECT mode FROM chat_modes WHERE chat_key = ?",
            (key,)
        ).fetchone()

    return row[0] if row else "default"


async def set_mode(message: Message, mode: str):
    key = chat_key(message)

    async with db_lock:
        db.execute(
            """
            INSERT INTO chat_modes(chat_key, mode)
            VALUES (?, ?)
            ON CONFLICT(chat_key)
            DO UPDATE SET mode = excluded.mode
            """,
            (key, mode)
        )
        db.commit()


async def is_authorized(message: Message) -> bool:
    if message.chat.type == ChatType.PRIVATE:
        return True

    if not message.from_user:
        return False

    try:
        member = await message.bot.get_chat_member(
            message.chat.id,
            message.from_user.id
        )

        return member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR
        }

    except Exception:
        return False


def settings_keyboard(mode: str):
    voice_active = mode == "voice"

    voice_style = "primary" if voice_active else "danger"
    default_style = "primary" if not voice_active else "danger"

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


async def save_file_id(category: str, mode: str, file_id: str):
    async with db_lock:
        db.execute(
            """
            INSERT OR IGNORE INTO id_files
            (category, mode, file_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (category, mode, file_id, asyncio.get_running_loop().time())
        )
        db.commit()


async def file_id_exists(category: str, mode: str, file_id: str) -> bool:
    async with db_lock:
        row = db.execute(
            """
            SELECT 1
            FROM id_files
            WHERE category = ? AND mode = ? AND file_id = ?
            """,
            (category, mode, file_id)
        ).fetchone()

    return row is not None


async def get_rotation(user_id: int, key: str) -> int:
    async with state_lock:
        state = user_rotations.setdefault(user_id, {})
        value = state.get(key, 0)
        state[key] = (value + 1) % len(NORMAL_REPLIES)
        return value


async def cleanup_directory(path: Path):
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def find_media_file(directory: Path) -> Path | None:
    files = [
        p for p in directory.rglob("*")
        if p.is_file()
        and p.suffix.lower() not in {".part", ".ytdl", ".json"}
    ]

    if not files:
        return None

    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files[0]


def download_sync(url: str, directory: Path, voice: bool):
    output_template = str(directory / "%(uploader|Unknown)s - %(title|media)s.%(ext)s")

    if voice:
        format_value = "bestaudio/best"
    else:
        format_value = "bestvideo+bestaudio/best"

    options = {
        "format": format_value,
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "overwrites": False,
        "restrictfilenames": False,
        "windowsfilenames": False
    }

    if not voice:
        options["merge_output_format"] = None

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    return info


async def download_media(url: str, directory: Path, voice: bool):
    return await asyncio.to_thread(
        download_sync,
        url,
        directory,
        voice
    )


async def create_voice_ogg(source: Path, target: Path):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libopus",
        "-b:a",
        "128k",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    code = await process.wait()

    if code != 0 or not target.exists():
        raise RuntimeError("FFmpeg voice conversion failed")


async def queue_download():
    global waiting_count

    async with waiting_lock:
        if download_slots.locked():
            if waiting_count >= 3:
                return False
            waiting_count += 1
            waiting = True
        else:
            waiting = False

    await download_slots.acquire()

    if waiting:
        async with waiting_lock:
            waiting_count -= 1

    return True


def release_download():
    download_slots.release()


async def send_default(message: Message, file_path: Path, publisher: str, title: str):
    extension = get_extension(file_path)
    filename = make_filename(
        publisher,
        title,
        extension
    )

    sent = await message.answer_document(
        FSInputFile(
            file_path,
            filename=filename
        )
    )

    return sent


async def send_voice(message: Message, file_path: Path):
    ogg_path = file_path.parent / f"{uuid.uuid4().hex}.ogg"

    await create_voice_ogg(
        file_path,
        ogg_path
    )

    sent = await message.answer_voice(
        FSInputFile(ogg_path)
    )

    try:
        ogg_path.unlink(missing_ok=True)
    except Exception:
        pass

    return sent


async def process_url(message: Message, url: str):
    if is_telegram_url(url):
        await message.reply(URL_FAIL)
        return

    mode = await get_mode(message)

    start_message = await message.reply(URL_START)

    acquired = False
    work_dir = TEMP_DIR / uuid.uuid4().hex

    try:
        acquired = await queue_download()

        if not acquired:
            await start_message.delete()
            await message.reply(URL_FAIL)
            return

        work_dir.mkdir(parents=True, exist_ok=True)

        category = "youtube" if is_youtube_url(url) else "direct"

        info = await download_media(
            url,
            work_dir,
            mode == "voice"
        )

        media_file = find_media_file(work_dir)

        if not media_file:
            raise RuntimeError("No media file")

        publisher = ""
        title = ""

        if isinstance(info, dict):
            publisher = (
                info.get("uploader")
                or info.get("channel")
                or info.get("creator")
                or ""
            )

            title = (
                info.get("title")
                or ""
            )

        if mode == "voice":
            sent = await send_voice(
                message,
                media_file
            )

            file_id = sent.voice.file_id

        else:
            sent = await send_default(
                message,
                media_file,
                publisher,
                title
            )

            file_id = sent.document.file_id

        await save_file_id(
            category,
            mode,
            file_id
        )

        await start_message.delete()

    except Exception:
        try:
            await start_message.delete()
        except Exception:
            pass

        await message.reply(URL_FAIL)

    finally:
        if acquired:
            release_download()

        await cleanup_directory(work_dir)


async def process_youtube(message: Message, title: str):
    start_message = await message.reply(
        YT_START.format(title=title)
    )

    acquired = False
    work_dir = TEMP_DIR / uuid.uuid4().hex

    try:
        result = await select_youtube_result(title)

        if not result:
            raise RuntimeError("YouTube search failed")

        url = result.get("link")

        if not url:
            raise RuntimeError("No YouTube URL")

        mode = await get_mode(message)

        acquired = await queue_download()

        if not acquired:
            raise RuntimeError("Queue full")

        work_dir.mkdir(parents=True, exist_ok=True)

        info = await download_media(
            url,
            work_dir,
            mode == "voice"
        )

        media_file = find_media_file(work_dir)

        if not media_file:
            raise RuntimeError("No media file")

        publisher = ""
        selected_title = result.get("title") or title

        if isinstance(info, dict):
            publisher = (
                info.get("uploader")
                or info.get("channel")
                or ""
            )

        if mode == "voice":
            sent = await send_voice(
                message,
                media_file
            )

            file_id = sent.voice.file_id

        else:
            sent = await send_default(
                message,
                media_file,
                publisher,
                selected_title
            )

            file_id = sent.document.file_id

        await save_file_id(
            "youtube",
            mode,
            file_id
        )

        await start_message.delete()

    except Exception:
        try:
            await start_message.delete()
        except Exception:
            pass

        await message.reply(YT_FAIL)

    finally:
        if acquired:
            release_download()

        await cleanup_directory(work_dir)


@router.message(F.text)
async def message_handler(message: Message):
    text = message.text.strip()

    if text == "ادت":
        if await is_authorized(message):
            mode = await get_mode(message)

            await message.reply(
                "تستطيع تغيير وضع عمل البوت من هنا",
                reply_markup=settings_keyboard(mode)
            )

        return

    if message.chat.type == ChatType.PRIVATE:
        if text.startswith("يوت"):
            title = text[3:].strip()

            if title:
                await process_youtube(
                    message,
                    title
                )

            return

        url = extract_url(text)

        if url:
            await process_url(
                message,
                url
            )
            return

        index = await get_rotation(
            message.from_user.id,
            chat_key(message)
        )

        await message.reply(
            NORMAL_REPLIES[index]
        )
        return

    if text == "بوت":
        index = await get_rotation(
            message.from_user.id if message.from_user else 0,
            chat_key(message)
        )

        await message.reply(
            NORMAL_REPLIES[index]
        )


@router.callback_query(F.data.in_({"mode_voice", "mode_default"}))
async def mode_callback(callback: CallbackQuery):
    message = callback.message

    if not message:
        await callback.answer()
        return

    fake_message = message

    if not await is_authorized(fake_message):
        await callback.answer(
            "عزيزي ليس مصرح لك بذلك",
            show_alert=True
        )
        return

    current = await get_mode(message)

    if callback.data == "mode_voice":
        if current == "voice":
            await set_mode(
                message,
                "default"
            )
        else:
            await set_mode(
                message,
                "voice"
            )

    elif callback.data == "mode_default":
        if current == "default":
            await callback.answer(
                "زر افتراضي مُفعل بالفعل",
                show_alert=True
            )
            return

        await set_mode(
            message,
            "default"
        )

    new_mode = await get_mode(message)

    await message.edit_reply_markup(
        reply_markup=settings_keyboard(new_mode)
    )

    await callback.answer()


async def main():
    bot = Bot(TOKEN)
    dp = Dispatcher()

    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())