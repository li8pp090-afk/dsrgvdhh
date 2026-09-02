import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiosqlite
import yt_dlp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    ReplyParameters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")

ACTIVE_DOWNLOADS = 3
WAITING_DOWNLOADS = 3

START_TEXT = "ههع شم كسي\nيلا"
FAIL_TEXT = "الرابط غير مدعوم او الموقع مو راضي يتعاون\nشم طيزي يلا"
YT_START_TEXT = "ها تريد {query}\nتمام عبي"
YT_FAIL_TEXT = "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا"

EDIT_PROMPT = (
    "تستطيع تعديل مدة الصوتيات هكذا\n"
    "12:45 / 18:36 وللساعات 12.30:48"
)

EDIT_DURATION_FAIL = (
    "مدة هذه الصوتيه اصغر من المدة اللتي\n"
    "ارسلتها"
)

BOT_REPLIES = [
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
    "راح انزع وتنيكني بدال هذا\nالنيج شو داضوج",
]

DEFAULT_SUCCESS_REPLIES = [
    "هلو باي\nاه",
    "باي هلو\nاه",
    "اه باي\nهلو",
    "اه هلو\nباي",
]

reply_state = {}
success_state = {}
edit_state = {}

reply_state_lock = asyncio.Lock()
success_state_lock = asyncio.Lock()
edit_state_lock = asyncio.Lock()

router = Router()
download_queue = asyncio.Queue(maxsize=WAITING_DOWNLOADS)

UPPER_EXCEPTIONS = set("ATFNMJULG")

TELEGRAM_HOSTS = {
    "t.me",
    "telegram.me",
    "telegram.dog",
    "www.t.me",
    "www.telegram.me",
    "www.telegram.dog",
}


def scope_for_message(message: Message) -> str:
    if message.chat.type == "private":
        return f"user:{message.from_user.id}"
    return f"chat:{message.chat.id}"


def scope_for_callback(callback: CallbackQuery) -> str:
    if callback.message and callback.message.chat.type == "private":
        return f"user:{callback.from_user.id}"
    return f"chat:{callback.message.chat.id}"


def user_scope_key(message: Message) -> str:
    user_id = message.from_user.id if message.from_user else 0
    return f"{message.chat.id}:{user_id}"


def reply_parameters(message: Message) -> ReplyParameters:
    return ReplyParameters(message_id=message.message_id)


def clean_component(value: str) -> str:
    value = (value or "").strip()

    if not value:
        return ""

    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    if re.search(r"[A-Za-z]", value):
        chars = []

        for ch in value:
            if ch.isascii() and ch.isalpha():
                chars.append(
                    ch.upper()
                    if ch.upper() in UPPER_EXCEPTIONS
                    else ch.lower()
                )
            else:
                chars.append(ch)

        value = "".join(chars)

    return value


def build_filename(info: dict, actual_path: Path) -> str:
    publisher = clean_component(
        info.get("channel")
        or info.get("uploader")
        or info.get("creator")
        or ""
    )

    title = clean_component(info.get("title") or "")

    if publisher and title:
        stem = f"{publisher} - {title}"
    else:
        stem = (
            publisher
            or title
            or clean_component(actual_path.stem)
            or "file"
        )

    return f"{stem}{actual_path.suffix}"


def is_telegram_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()

        return (
            host in TELEGRAM_HOSTS
            or host.endswith(".telegram.org")
        )
    except Exception:
        return False


def normalize_url(text: str) -> str | None:
    match = re.search(r"https?://\S+", text or "")

    if not match:
        return None

    return match.group(0).rstrip(".,!?)]}")


def sha256_id(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def parse_duration(value: str) -> float | None:
    value = value.strip()

    hour_match = re.fullmatch(
        r"(\d+)\.(\d{1,2}):(\d{1,2})",
        value,
    )

    if hour_match:
        hours = int(hour_match.group(1))
        minutes = int(hour_match.group(2))
        seconds = int(hour_match.group(3))

        if minutes >= 60 or seconds >= 60:
            return None

        return (
            hours * 3600
            + minutes * 60
            + seconds
        )

    minute_match = re.fullmatch(
        r"(\d+):(\d{1,2})",
        value,
    )

    if minute_match:
        minutes = int(minute_match.group(1))
        seconds = int(minute_match.group(2))

        if seconds >= 60:
            return None

        return minutes * 60 + seconds

    return None


def parse_duration_range(text: str):
    parts = text.split("/")

    if len(parts) != 2:
        return None

    start = parse_duration(parts[0])
    end = parse_duration(parts[1])

    if start is None or end is None:
        return None

    if start >= end:
        return None

    return start, end


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                scope_key TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'default'
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS id_files (
                scope_key TEXT NOT NULL,
                mode TEXT NOT NULL,
                source_type TEXT NOT NULL,
                content_id TEXT NOT NULL,
                file_id TEXT NOT NULL,
                filename TEXT,
                PRIMARY KEY (
                    scope_key,
                    mode,
                    source_type,
                    content_id
                )
            )
            """
        )

        await db.commit()


async def get_mode(scope: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT mode
            FROM settings
            WHERE scope_key = ?
            """,
            (scope,),
        )

        row = await cur.fetchone()

        return row[0] if row else "default"


async def set_mode(scope: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings(scope_key, mode)
            VALUES (?, ?)
            ON CONFLICT(scope_key)
            DO UPDATE SET mode = excluded.mode
            """,
            (scope, mode),
        )

        await db.commit()


async def get_file_record(
    scope,
    mode,
    source_type,
    content_id,
):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT file_id, filename
            FROM id_files
            WHERE scope_key = ?
              AND mode = ?
              AND source_type = ?
              AND content_id = ?
            """,
            (
                scope,
                mode,
                source_type,
                content_id,
            ),
        )

        return await cur.fetchone()


async def get_file_record_by_file_id(
    scope,
    file_id,
):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT
                source_type,
                content_id,
                file_id,
                filename,
                mode
            FROM id_files
            WHERE scope_key = ?
              AND file_id = ?
            LIMIT 1
            """,
            (
                scope,
                file_id,
            ),
        )

        return await cur.fetchone()


async def save_file_record(
    scope,
    mode,
    source_type,
    content_id,
    file_id,
    filename,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO id_files
            (
                scope_key,
                mode,
                source_type,
                content_id,
                file_id,
                filename
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                mode,
                source_type,
                content_id,
                file_id,
                filename,
            ),
        )

        await db.commit()


async def is_chat_owner(message: Message) -> bool:
    if message.chat.type == "private":
        return True

    if not message.from_user:
        return False

    try:
        member = await message.bot.get_chat_member(
            message.chat.id,
            message.from_user.id,
        )

        return member.status == "creator"

    except Exception:
        return False


async def is_callback_owner(callback: CallbackQuery) -> bool:
    if not callback.message:
        return False

    if callback.message.chat.type == "private":
        return True

    try:
        member = await callback.bot.get_chat_member(
            callback.message.chat.id,
            callback.from_user.id,
        )

        return member.status == "creator"

    except Exception:
        return False


def settings_markup(mode: str) -> InlineKeyboardMarkup:
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


async def next_default_success(message: Message):
    key = user_scope_key(message)

    async with success_state_lock:
        index = success_state.get(key, 0)
        success_state[key] = (
            index + 1
        ) % len(DEFAULT_SUCCESS_REPLIES)

    await message.bot.send_message(
        chat_id=message.chat.id,
        text=DEFAULT_SUCCESS_REPLIES[index],
        reply_parameters=reply_parameters(message),
    )


async def send_saved_file(
    bot: Bot,
    message: Message,
    mode: str,
    file_id: str,
):
    if mode == "voice":
        await bot.send_voice(
            chat_id=message.chat.id,
            voice=file_id,
            reply_parameters=reply_parameters(message),
        )
    else:
        await bot.send_document(
            chat_id=message.chat.id,
            document=file_id,
            reply_parameters=reply_parameters(message),
        )


@router.message(F.text == "ادت")
async def edit_mode(message: Message):
    if not await is_chat_owner(message):
        return

    scope = scope_for_message(message)
    mode = await get_mode(scope)

    await message.bot.send_message(
        chat_id=message.chat.id,
        text="تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=settings_markup(mode),
        reply_parameters=reply_parameters(message),
    )


@router.callback_query(F.data.startswith("mode:"))
async def mode_callback(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return

    if not await is_callback_owner(callback):
        await callback.answer(
            "عزيزي\nليس مصرح لك بذلك",
            show_alert=True,
        )
        return

    requested = callback.data.split(
        ":",
        1,
    )[1]

    scope = scope_for_callback(callback)
    current = await get_mode(scope)

    if requested == "default" and current == "default":
        await callback.answer(
            "زر افتراضي مُفعل\nبالفعل",
            show_alert=True,
        )
        return

    if requested == "voice" and current == "voice":
        requested = "default"

    await set_mode(
        scope,
        requested,
    )

    await callback.message.edit_reply_markup(
        reply_markup=settings_markup(requested)
    )

    await callback.answer()


def ytdlp_options(
    workdir: str,
    mode: str,
) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "paths": {
            "home": workdir,
        },
        "outtmpl": "%(id)s.%(ext)s",
    }

    if mode == "voice":
        options["format"] = "bestaudio/best"
    else:
        options["format"] = (
            "bestvideo+bestaudio/best"
        )

    return options


def youtube_search_3(query: str) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.extract_info(
            f"ytsearch3:{query}",
            download=False,
        )

    entries = [
        entry
        for entry in (result.get("entries") or [])
        if entry
    ]

    entries.sort(
        key=lambda entry: int(
            entry.get("view_count") or 0
        ),
        reverse=True,
    )

    if not entries:
        raise RuntimeError(
            "no youtube results"
        )

    return entries[0]


def download_with_ytdlp(
    url: str,
    mode: str,
    workdir: str,
):
    options = ytdlp_options(
        workdir,
        mode,
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

        prepared = Path(
            ydl.prepare_filename(info)
        )

        if prepared.exists():
            return prepared, info

        files = [
            path
            for path in Path(workdir).iterdir()
            if path.is_file()
        ]

        if not files:
            raise RuntimeError(
                "download failed"
            )

        return max(
            files,
            key=lambda p: p.stat().st_mtime,
        ), info


async def convert_to_ogg_opus(
    source: Path,
    target: Path,
):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libopus",
        "-f",
        "ogg",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    code = await process.wait()

    if code != 0 or not target.exists():
        raise RuntimeError(
            "opus conversion failed"
        )


async def get_media_duration(
    path: Path,
) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    stdout, _ = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            "duration check failed"
        )

    return float(stdout.decode().strip())


async def trim_voice(
    source: Path,
    target: Path,
    start: float,
    end: float,
):
    duration = end - start

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(source),
        "-t",
        str(duration),
        "-vn",
        "-c:a",
        "libopus",
        "-f",
        "ogg",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    code = await process.wait()

    if code != 0 or not target.exists():
        raise RuntimeError(
            "voice trim failed"
        )


async def download_telegram_file(
    bot: Bot,
    file_id: str,
    target: Path,
):
    telegram_file = await bot.get_file(
        file_id
    )

    await bot.download_file(
        telegram_file.file_path,
        destination=target,
    )


@router.message(
    F.text == "تعديل",
    F.reply_to_message,
)
async def start_voice_edit(
    message: Message,
):
    replied = message.reply_to_message

    if not replied:
        return

    if not replied.voice:
        return

    scope = scope_for_message(message)

    record = await get_file_record_by_file_id(
        scope,
        replied.voice.file_id,
    )

    if not record:
        return

    if record[4] != "voice":
        return

    key = user_scope_key(message)

    async with edit_state_lock:
        edit_state[key] = {
            "file_id": replied.voice.file_id,
            "source_type": record[0],
            "content_id": record[1],
            "filename": record[3],
        }

    await message.bot.send_message(
        chat_id=message.chat.id,
        text=EDIT_PROMPT,
        reply_parameters=reply_parameters(message),
    )


@router.message(F.text)
async def voice_edit_handler(
    message: Message,
):
    text = (message.text or "").strip()

    if not text:
        return

    key = user_scope_key(message)

    async with edit_state_lock:
        state = edit_state.get(key)

    if not state:
        return

    parsed = parse_duration_range(text)

    if not parsed:
        return

    start, end = parsed

    workdir = Path(
        tempfile.mkdtemp(
            prefix="voice_edit_"
        )
    )

    source = workdir / "source.ogg"
    output = workdir / "edited.ogg"

    try:
        await download_telegram_file(
            message.bot,
            state["file_id"],
            source,
        )

        total_duration = await get_media_duration(
            source
        )

        if (
            start >= total_duration
            or end > total_duration
        ):
            await message.bot.send_message(
                chat_id=message.chat.id,
                text=EDIT_DURATION_FAIL,
                reply_parameters=reply_parameters(
                    message
                ),
            )
            return

        await trim_voice(
            source,
            output,
            start,
            end,
        )

        sent = await message.bot.send_voice(
            chat_id=message.chat.id,
            voice=FSInputFile(output),
            reply_parameters=reply_parameters(
                message
            ),
        )

        scope = scope_for_message(message)

        new_content_id = sha256_id(
            f"{state['content_id']}:{start}:{end}:{sent.voice.file_unique_id}"
        )

        await save_file_record(
            scope,
            "voice",
            state["source_type"],
            new_content_id,
            sent.voice.file_id,
            "voice.ogg",
        )

        async with edit_state_lock:
            edit_state.pop(
                key,
                None,
            )

    except Exception:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=FAIL_TEXT,
            reply_parameters=reply_parameters(
                message
            ),
        )

    finally:
        shutil.rmtree(
            workdir,
            ignore_errors=True,
        )


async def process_url(
    bot: Bot,
    message: Message,
    url: str,
    mode: str,
):
    if is_telegram_url(url):
        return

    scope = scope_for_message(message)

    source_type = "url"
    content_id = sha256_id(url)

    existing = await get_file_record(
        scope,
        mode,
        source_type,
        content_id,
    )

    if existing:
        await send_saved_file(
            bot,
            message,
            mode,
            existing[0],
        )

        if mode == "default":
            await next_default_success(
                message
            )

        return

    status = await message.bot.send_message(
        chat_id=message.chat.id,
        text=START_TEXT,
        reply_parameters=reply_parameters(
            message
        ),
    )

    workdir = tempfile.mkdtemp(
        prefix="download_"
    )

    try:
        path, info = await asyncio.to_thread(
            download_with_ytdlp,
            url,
            mode,
            workdir,
        )

        if mode == "voice":
            output = (
                Path(workdir)
                / "voice.ogg"
            )

            await convert_to_ogg_opus(
                path,
                output,
            )

            sent = await bot.send_voice(
                chat_id=message.chat.id,
                voice=FSInputFile(output),
                reply_parameters=reply_parameters(
                    message
                ),
            )

            await save_file_record(
                scope,
                mode,
                source_type,
                content_id,
                sent.voice.file_id,
                "voice.ogg",
            )

        else:
            filename = build_filename(
                info,
                path,
            )

            sent = await bot.send_document(
                chat_id=message.chat.id,
                document=FSInputFile(
                    path,
                    filename=filename,
                ),
                reply_parameters=reply_parameters(
                    message
                ),
            )

            await save_file_record(
                scope,
                mode,
                source_type,
                content_id,
                sent.document.file_id,
                filename,
            )

            await next_default_success(
                message
            )

    except Exception:
        await bot.send_message(
            chat_id=message.chat.id,
            text=FAIL_TEXT,
            reply_parameters=reply_parameters(
                message
            ),
        )

    finally:
        try:
            await status.delete()
        except Exception:
            pass

        shutil.rmtree(
            workdir,
            ignore_errors=True,
        )


async def process_youtube(
    bot: Bot,
    message: Message,
    query: str,
):
    scope = scope_for_message(message)

    source_type = "youtube"
    mode = "voice"

    status = None
    workdir = tempfile.mkdtemp(
        prefix="youtube_"
    )

    try:
        result = await asyncio.to_thread(
            youtube_search_3,
            query,
        )

        video_id = result.get("id")
        video_url = (
            result.get("webpage_url")
            or result.get("url")
        )

        if not video_id or not video_url:
            raise RuntimeError(
                "youtube result has no URL"
            )

        existing = await get_file_record(
            scope,
            mode,
            source_type,
            video_id,
        )

        if existing:
            await send_saved_file(
                bot,
                message,
                "voice",
                existing[0],
            )
            return

        status = await bot.send_message(
            chat_id=message.chat.id,
            text=YT_START_TEXT.format(
                query=query
            ),
            reply_parameters=reply_parameters(
                message
            ),
        )

        path, info = await asyncio.to_thread(
            download_with_ytdlp,
            video_url,
            "voice",
            workdir,
        )

        output = (
            Path(workdir)
            / f"{video_id}.ogg"
        )

        await convert_to_ogg_opus(
            path,
            output,
        )

        sent = await bot.send_voice(
            chat_id=message.chat.id,
            voice=FSInputFile(output),
            reply_parameters=reply_parameters(
                message
            ),
        )

        await save_file_record(
            scope,
            "voice",
            "youtube",
            video_id,
            sent.voice.file_id,
            output.name,
        )

    except Exception:
        await bot.send_message(
            chat_id=message.chat.id,
            text=YT_FAIL_TEXT,
            reply_parameters=reply_parameters(
                message
            ),
        )

    finally:
        if status:
            try:
                await status.delete()
            except Exception:
                pass

        shutil.rmtree(
            workdir,
            ignore_errors=True,
        )


async def submit_job(
    bot: Bot,
    message: Message,
    value: str,
    mode: str,
    youtube: bool,
):
    try:
        download_queue.put_nowait(
            (
                bot,
                message,
                value,
                mode,
                youtube,
            )
        )

    except asyncio.QueueFull:
        return


@router.message(F.text.startswith("يوت "))
async def youtube_handler(
    message: Message,
):
    query = message.text[4:].strip()

    if not query:
        return

    await submit_job(
        message.bot,
        message,
        query,
        "voice",
        True,
    )


async def rotating_reply(
    message: Message,
):
    if not message.from_user:
        return

    key = user_scope_key(message)

    async with reply_state_lock:
        index = reply_state.get(
            key,
            0,
        )

        reply_state[key] = (
            index + 1
        ) % len(BOT_REPLIES)

    await message.bot.send_message(
        chat_id=message.chat.id,
        text=BOT_REPLIES[index],
        reply_parameters=reply_parameters(
            message
        ),
    )


@router.message(F.text)
async def text_handler(
    message: Message,
):
    text = (message.text or "").strip()

    if not text:
        return

    if text == "تعديل":
        return

    if text == "ادت":
        return

    url = normalize_url(text)

    if url:
        if is_telegram_url(url):
            return

        mode = await get_mode(
            scope_for_message(message)
        )

        await submit_job(
            message.bot,
            message,
            url,
            mode,
            False,
        )

        return

    if text.startswith("يوت "):
        return

    if (
        message.chat.type == "private"
        or text == "بوت"
    ):
        await rotating_reply(
            message
        )


async def worker():
    while True:
        (
            bot,
            message,
            value,
            mode,
            youtube,
        ) = await download_queue.get()

        try:
            if youtube:
                await process_youtube(
                    bot,
                    message,
                    value,
                )
            else:
                await process_url(
                    bot,
                    message,
                    value,
                    mode,
                )

        finally:
            download_queue.task_done()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set"
        )

    await init_db()

    bot = Bot(BOT_TOKEN)

    dp = Dispatcher()
    dp.include_router(router)

    workers = [
        asyncio.create_task(worker())
        for _ in range(ACTIVE_DOWNLOADS)
    ]

    try:
        await dp.start_polling(bot)

    finally:
        for task in workers:
            task.cancel()

        await asyncio.gather(
            *workers,
            return_exceptions=True,
        )

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())