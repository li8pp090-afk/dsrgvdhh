import asyncio
import difflib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import redis.asyncio as redis
import yt_dlp

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")

MAX_ACTIVE = 3
MAX_WAITING = 3

MODE_PREFIX = "media:mode:"
CACHE_PREFIX = "media:idfile:"
ROTATION_PREFIX = "media:rotation:"
JOB_PREFIX = "media:job:"
ACTIVE_KEY = "media:queue:active"
WAITING_KEY = "media:queue:waiting"

DEFAULT_MODE = "default"
VOICE_MODE = "voice"

NORMAL_START = "ههع شم كسي\nيلا"
NORMAL_FAIL = "الرابط غير مدعوم او الموقع مو راضي يتعاون\nشم طيزي يلا"

YOUTUBE_START = "ها تريد هوف\nتمام عبي"
YOUTUBE_FAIL = "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا"

CHAT_REPLIES = [
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
    "راح انزع وتنيكني بدال هذا\nالنيج شو داضوج",
]

SUCCESS_REPLIES = [
    "هلو باي\nاه",
    "باي هلو\nاه",
    "اه باي\nهلو",
    "اه هلو\nباي",
]

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
)

storage = RedisStorage(
    redis=redis_client,
    key_builder=DefaultKeyBuilder(
        with_bot_id=True,
    ),
)

dp = Dispatcher(
    storage=storage,
    fsm_strategy=FSMStrategy.USER_IN_TOPIC,
)

waiting_events = {}
waiting_events_lock = asyncio.Lock()


def normalize_spaces(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_filename_part(value):
    value = normalize_spaces(value)
    value = re.sub(r"[^\w\s]", "", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def format_english_letters(value):
    value = str(value or "").lower()
    return "".join(
        char.upper()
        if char in {"a", "t", "f", "n", "m", "j", "u", "l", "g"}
        else char
        for char in value
    )


def make_filename(info, actual_suffix):
    uploader = info.get("channel") or info.get("uploader") or ""
    title = info.get("title") or info.get("fulltitle") or ""

    uploader = format_english_letters(clean_filename_part(uploader))
    title = format_english_letters(clean_filename_part(title))

    if uploader and title:
        name = f"{uploader} - {title}"
    else:
        name = uploader or title or "media"

    suffix = actual_suffix or ""
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix

    return f"{name}{suffix}"


def normalize_url(url):
    parsed = urlparse(url.strip())

    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() not in {
            "si",
            "feature",
            "pp",
            "app",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "utm_term",
        }:
            query.append((key, value))

    normalized_query = urlencode(sorted(query))

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            normalized_query,
            "",
        )
    )


def is_telegram_url(url):
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False

    return (
        host == "t.me"
        or host.endswith(".t.me")
        or host == "telegram.me"
        or host.endswith(".telegram.me")
        or host == "telegram.dog"
        or host.endswith(".telegram.dog")
    )


def extract_url(text):
    if not text:
        return None

    match = re.search(
        r"https?://[^\s<>\"]+",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    url = match.group(0).rstrip(".,!?)]}>")

    return url


def is_youtube_command(text):
    if not text:
        return False

    value = text.strip()

    if not value:
        return False

    parts = value.split(maxsplit=1)

    return parts[0].casefold() == "يوت"


def youtube_query(text):
    parts = text.strip().split(maxsplit=1)

    if len(parts) != 2:
        return None

    query = parts[1].strip()

    return query or None


def get_scope(message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else message.sender_chat.id if message.sender_chat else 0
    thread_id = message.message_thread_id or 0

    return f"{chat_id}:{thread_id}:{user_id}"


def mode_key(chat_id):
    return f"{MODE_PREFIX}{chat_id}"


def rotation_key(kind, message):
    return f"{ROTATION_PREFIX}{kind}:{get_scope(message)}"


def cache_key(mode, identity):
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    return f"{CACHE_PREFIX}{mode}:{digest}"


async def get_mode(chat_id):
    value = await redis_client.get(mode_key(chat_id))

    if value in {DEFAULT_MODE, VOICE_MODE}:
        return value

    return DEFAULT_MODE


async def set_mode(chat_id, mode):
    await redis_client.set(
        mode_key(chat_id),
        mode,
    )


def settings_keyboard(mode):
    voice_style = "primary" if mode == VOICE_MODE else "danger"
    default_style = "primary" if mode == DEFAULT_MODE else "danger"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="فويس",
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


async def is_authorized(message_or_callback):
    if isinstance(message_or_callback, Message):
        chat = message_or_callback.chat
        user = message_or_callback.from_user
    else:
        message = message_or_callback.message
        if not message:
            return False

        chat = message.chat
        user = message_or_callback.from_user

    if chat.type == ChatType.PRIVATE:
        return True

    if chat.type == ChatType.CHANNEL:
        if isinstance(message_or_callback, Message):
            if message_or_callback.sender_chat and message_or_callback.sender_chat.id == chat.id:
                return True

        if not user:
            return False

    if not user:
        return False

    try:
        member = await bot.get_chat_member(
            chat_id=chat.id,
            user_id=user.id,
        )
    except Exception:
        return False

    return member.status in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }


async def get_rotation(kind, message):
    key = rotation_key(kind, message)
    value = await redis_client.get(key)

    try:
        return int(value or 0)
    except Exception:
        return 0


async def advance_rotation(kind, message, count):
    key = rotation_key(kind, message)

    current = await get_rotation(kind, message)
    await redis_client.set(
        key,
        (current + 1) % count,
    )

    return current


async def reply_success(message):
    index = await advance_rotation(
        "success_default",
        message,
        len(SUCCESS_REPLIES),
    )

    await message.reply(
        SUCCESS_REPLIES[index],
    )


async def reply_chat_rotation(message):
    index = await advance_rotation(
        "chat_replies",
        message,
        len(CHAT_REPLIES),
    )

    await message.reply(
        CHAT_REPLIES[index],
    )


async def reserve_job(job_id):
    script = """
    local active = KEYS[1]
    local waiting = KEYS[2]
    local active_limit = tonumber(ARGV[1])
    local waiting_limit = tonumber(ARGV[2])
    local job = ARGV[3]

    if redis.call("SCARD", active) < active_limit then
        redis.call("SADD", active, job)
        return 1
    end

    if redis.call("LLEN", waiting) < waiting_limit then
        redis.call("RPUSH", waiting, job)
        return 2
    end

    return 0
    """

    return int(
        await redis_client.eval(
            script,
            2,
            ACTIVE_KEY,
            WAITING_KEY,
            MAX_ACTIVE,
            MAX_WAITING,
            job_id,
        )
    )


async def promote_after_release(job_id):
    script = """
    local active = KEYS[1]
    local waiting = KEYS[2]
    local job_prefix = ARGV[1]
    local finished = ARGV[2]
    local active_limit = tonumber(ARGV[3])

    redis.call("SREM", active, finished)

    if redis.call("SCARD", active) >= active_limit then
        return ""
    end

    local next_job = redis.call("LPOP", waiting)

    if not next_job then
        return ""
    end

    redis.call("SADD", active, next_job)
    redis.call("SET", job_prefix .. next_job, "active", "EX", 86400)

    return next_job
    """

    next_job = await redis_client.eval(
        script,
        2,
        ACTIVE_KEY,
        WAITING_KEY,
        JOB_PREFIX,
        job_id,
        MAX_ACTIVE,
    )

    if not next_job:
        return None

    async with waiting_events_lock:
        event = waiting_events.get(next_job)

    if event:
        event.set()

    return next_job


async def wait_for_job(job_id):
    status_key = f"{JOB_PREFIX}{job_id}"

    status = await redis_client.get(status_key)

    if status == "active":
        return True

    event = asyncio.Event()

    async with waiting_events_lock:
        waiting_events[job_id] = event

    try:
        while True:
            status = await redis_client.get(status_key)

            if status == "active":
                return True

            if status in {"failed", "cancelled"}:
                return False

            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                pass

            event.clear()
    finally:
        async with waiting_events_lock:
            waiting_events.pop(job_id, None)


async def release_job(job_id):
    await redis_client.delete(
        f"{JOB_PREFIX}{job_id}",
    )

    await promote_after_release(job_id)


async def create_job():
    job_id = uuid.uuid4().hex

    result = await reserve_job(job_id)

    if result == 0:
        return None, False

    await redis_client.set(
        f"{JOB_PREFIX}{job_id}",
        "active" if result == 1 else "waiting",
        ex=86400,
    )

    return job_id, result == 1


def identity_from_info(info, fallback):
    extractor = info.get("extractor_key") or info.get("extractor") or ""
    media_id = info.get("id") or ""
    webpage_url = info.get("webpage_url") or info.get("original_url") or ""

    identity = f"{extractor}|{media_id}|{normalize_url(webpage_url)}"

    if identity.strip("|"):
        return identity

    return normalize_url(fallback)


async def get_cached(mode, identity):
    key = cache_key(
        mode,
        identity,
    )

    data = await redis_client.hgetall(key)

    if not data:
        return None

    if data.get("file_id") and data.get("kind"):
        return data

    return None


async def save_cached(mode, identity, file_id, kind, filename):
    key = cache_key(
        mode,
        identity,
    )

    await redis_client.hset(
        key,
        mapping={
            "file_id": file_id,
            "kind": kind,
            "filename": filename,
        },
    )

    await redis_client.expire(
        key,
        60 * 60 * 24 * 365 * 5,
    )


def build_ydl_options(temp_dir):
    return {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(Path(temp_dir) / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "merge_output_format": None,
    }


def find_downloaded_file(temp_dir):
    candidates = []

    for path in Path(temp_dir).iterdir():
        if not path.is_file():
            continue

        if path.name.endswith(".part"):
            continue

        if path.name.endswith(".ytdl"):
            continue

        if path.name.endswith(".temp"):
            continue

        if path.suffix.lower() in {
            ".json",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".vtt",
            ".srt",
        }:
            continue

        candidates.append(path)

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    return candidates[0]


async def ytdlp_extract(url, download, temp_dir):
    def run():
        options = build_ydl_options(temp_dir)
        options["skip_download"] = not download

        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(
                url,
                download=download,
            )

    return await asyncio.to_thread(
        run,
    )


async def search_youtube(query):
    def run():
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(
                f"ytsearch3:{query}",
                download=False,
            )

    result = await asyncio.to_thread(
        run,
    )

    entries = result.get("entries") or []

    return [entry for entry in entries if entry]


def similarity(query, title):
    query_words = normalize_spaces(query).casefold()
    title_words = normalize_spaces(title).casefold()

    ratio = difflib.SequenceMatcher(
        None,
        query_words,
        title_words,
    ).ratio()

    query_tokens = set(
        re.findall(
            r"\w+",
            query_words,
            flags=re.UNICODE,
        )
    )

    title_tokens = set(
        re.findall(
            r"\w+",
            title_words,
            flags=re.UNICODE,
        )
    )

    if query_tokens:
        overlap = len(query_tokens & title_tokens) / len(query_tokens)
    else:
        overlap = 0.0

    return ratio * 0.6 + overlap * 0.4


def choose_youtube_result(query, entries):
    if not entries:
        return None

    return max(
        entries,
        key=lambda entry: similarity(
            query,
            entry.get("title") or "",
        ),
    )


async def download_media(url, temp_dir):
    info = await ytdlp_extract(
        url,
        True,
        temp_dir,
    )

    if not info:
        raise RuntimeError("download failed")

    file_path = find_downloaded_file(temp_dir)

    if not file_path:
        raise RuntimeError("file not found")

    return info, file_path


async def convert_to_voice(source, target):
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

    returncode = await process.wait()

    if returncode != 0 or not target.exists():
        raise RuntimeError("voice conversion failed")


async def upload_document(message, path, filename):
    sent = await message.reply_document(
        document=FSInputFile(
            path,
            filename=filename,
        ),
    )

    return sent.document.file_id


async def upload_voice(message, path):
    sent = await message.reply_voice(
        voice=FSInputFile(path),
    )

    return sent.voice.file_id


async def send_cached(message, cached):
    if cached["kind"] == "document":
        await message.reply_document(
            document=cached["file_id"],
        )
        return

    await message.reply_voice(
        voice=cached["file_id"],
    )


async def process_download(message, url, mode, youtube=False):
    job_id, active = await create_job()

    if not job_id:
        return

    start_message = None

    try:
        if youtube:
            start_message = await message.reply(
                YOUTUBE_START,
            )
        else:
            if mode == VOICE_MODE:
                start_message = await message.reply(
                    YOUTUBE_START,
                )
            else:
                start_message = await message.reply(
                    NORMAL_START,
                )

        if not active:
            if not await wait_for_job(job_id):
                raise RuntimeError("queue failed")

        with tempfile.TemporaryDirectory(
            prefix="media_",
        ) as temp_dir:
            info, downloaded = await download_media(
                url,
                temp_dir,
            )

            identity = identity_from_info(
                info,
                url,
            )

            cached = await get_cached(
                mode,
                identity,
            )

            if cached:
                await send_cached(
                    message,
                    cached,
                )

                if start_message:
                    await start_message.delete()

                return

            if mode == VOICE_MODE:
                voice_path = Path(temp_dir) / "voice.ogg"

                await convert_to_voice(
                    downloaded,
                    voice_path,
                )

                file_id = await upload_voice(
                    message,
                    voice_path,
                )

                await save_cached(
                    mode,
                    identity,
                    file_id,
                    "voice",
                    "voice.ogg",
                )
            else:
                suffix = downloaded.suffix

                filename = make_filename(
                    info,
                    suffix,
                )

                final_path = Path(temp_dir) / filename

                if downloaded.resolve() != final_path.resolve():
                    downloaded.rename(final_path)

                file_id = await upload_document(
                    message,
                    final_path,
                    filename,
                )

                await save_cached(
                    mode,
                    identity,
                    file_id,
                    "document",
                    filename,
                )

            if start_message:
                await start_message.delete()

            if mode == DEFAULT_MODE:
                await reply_success(
                    message,
                )

    except Exception:
        if start_message:
            try:
                await start_message.delete()
            except Exception:
                pass

        if youtube:
            await message.reply(
                YOUTUBE_FAIL,
            )
        elif mode == VOICE_MODE:
            await message.reply(
                YOUTUBE_FAIL,
            )
        else:
            await message.reply(
                NORMAL_FAIL,
            )

    finally:
        await release_job(
            job_id,
        )


async def process_youtube(message, query, mode):
    job_id, active = await create_job()

    if not job_id:
        return

    start_message = None

    try:
        start_message = await message.reply(
            YOUTUBE_START,
        )

        if not active:
            if not await wait_for_job(job_id):
                raise RuntimeError("queue failed")

        entries = await search_youtube(
            query,
        )

        selected = choose_youtube_result(
            query,
            entries,
        )

        if not selected:
            raise RuntimeError("youtube search failed")

        selected_url = (
            selected.get("webpage_url")
            or selected.get("original_url")
        )

        if not selected_url:
            video_id = selected.get("id")

            if not video_id:
                raise RuntimeError("youtube result has no url")

            selected_url = f"https://www.youtube.com/watch?v={video_id}"

        with tempfile.TemporaryDirectory(
            prefix="media_",
        ) as temp_dir:
            metadata = await ytdlp_extract(
                selected_url,
                False,
                temp_dir,
            )

            if not metadata:
                raise RuntimeError("youtube metadata failed")

            identity = identity_from_info(
                metadata,
                selected_url,
            )

            cached = await get_cached(
                mode,
                identity,
            )

            if cached:
                await send_cached(
                    message,
                    cached,
                )

                await start_message.delete()

                return

            info, downloaded = await download_media(
                selected_url,
                temp_dir,
            )

            identity = identity_from_info(
                info,
                selected_url,
            )

            cached = await get_cached(
                mode,
                identity,
            )

            if cached:
                await send_cached(
                    message,
                    cached,
                )

                await start_message.delete()

                return

            if mode == VOICE_MODE:
                voice_path = Path(temp_dir) / "voice.ogg"

                await convert_to_voice(
                    downloaded,
                    voice_path,
                )

                file_id = await upload_voice(
                    message,
                    voice_path,
                )

                await save_cached(
                    mode,
                    identity,
                    file_id,
                    "voice",
                    "voice.ogg",
                )
            else:
                filename = make_filename(
                    info,
                    downloaded.suffix,
                )

                final_path = Path(temp_dir) / filename

                if downloaded.resolve() != final_path.resolve():
                    downloaded.rename(final_path)

                file_id = await upload_document(
                    message,
                    final_path,
                    filename,
                )

                await save_cached(
                    mode,
                    identity,
                    file_id,
                    "document",
                    filename,
                )

            await start_message.delete()

            if mode == DEFAULT_MODE:
                await reply_success(
                    message,
                )

    except Exception:
        if start_message:
            try:
                await start_message.delete()
            except Exception:
                pass

        await message.reply(
            YOUTUBE_FAIL,
        )

    finally:
        await release_job(
            job_id,
        )


@dp.message(F.text)
async def text_handler(message: Message):
    text = message.text.strip()

    if text.casefold() == "ادت":
        if not await is_authorized(message):
            return

        mode = await get_mode(
            message.chat.id,
        )

        await message.reply(
            "تستطيع تغيير وضع عمل البوت\nمن هنا",
            reply_markup=settings_keyboard(mode),
        )

        return

    if is_youtube_command(text):
        query = youtube_query(text)

        if not query:
            await message.reply(
                YOUTUBE_FAIL,
            )
            return

        mode = await get_mode(
            message.chat.id,
        )

        await process_youtube(
            message,
            query,
            mode,
        )

        return

    url = extract_url(text)

    if url:
        if is_telegram_url(url):
            return

        mode = await get_mode(
            message.chat.id,
        )

        await process_download(
            message,
            url,
            mode,
            youtube=False,
        )

        return

    if message.chat.type == ChatType.PRIVATE:
        await reply_chat_rotation(
            message,
        )

        return

    if text == "بوت":
        await reply_chat_rotation(
            message,
        )


@dp.callback_query(F.data.in_({"mode:voice", "mode:default"}))
async def mode_callback(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return

    if not await is_authorized(callback):
        await callback.answer(
            "عزيزي\nليس مصرح لك بذلك",
            show_alert=True,
        )
        return

    chat_id = callback.message.chat.id
    current_mode = await get_mode(
        chat_id,
    )

    requested_mode = (
        VOICE_MODE
        if callback.data == "mode:voice"
        else DEFAULT_MODE
    )

    if requested_mode == current_mode:
        if requested_mode == DEFAULT_MODE:
            await callback.answer(
                "زر افتراضي مُفعل\nبالفعل",
                show_alert=True,
            )
        else:
            await callback.answer()
        return

    await set_mode(
        chat_id,
        requested_mode,
    )

    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(
            requested_mode,
        ),
    )

    await callback.answer()


async def main():
    global bot

    bot = Bot(
        token=TOKEN,
    )

    try:
        await dp.start_polling(
            bot,
        )
    finally:
        await bot.session.close()
        await storage.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
    )

    asyncio.run(
        main(),
    )