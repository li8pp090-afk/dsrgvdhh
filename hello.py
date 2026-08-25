import asyncio
import os
import re
import shutil
import unicodedata
from pathlib import Path

import aiosqlite
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

TOKEN = os.environ["BOT_TOKEN"]
LOCAL_BOT_API = os.getenv("LOCAL_BOT_API", "").strip()

ADMIN_IDS = [
    8436425159,
    8255680206,
    8800673233,
    8606430342,
    8845740736,
    8554632449,
    8750024481,
]

ROTATING_REPLIES = [
    "مو ناوي تستعملني مثل البوتات ترى اذا اضوج\nاصيح المولاي يغصص بلاعيمك",
    "اهو فطستني بسوالفك هاي ديلا دز رابط اريد\nانفذلك طلباتك علمود انام",
    "ترى يمكن انطيك بلوك واعوفك ملبوس\nها شتكول بيبي",
]

BUTTON_STYLES = ["primary", "success", "danger"]

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

ID_FILE_DB = Path("ID-File.sqlite3")

MAX_ACTIVE = 3
MAX_QUEUE = 3

sessions = {}
sessions_lock = asyncio.Lock()
trim_states = {}


async def init_id_file():
    async with aiosqlite.connect(ID_FILE_DB) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS id_files (
                file_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def register_chat(chat_id: int, chat_type: str):
    async with aiosqlite.connect(ID_FILE_DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chats(chat_id, chat_type) VALUES (?, ?)",
            (chat_id, chat_type),
        )
        await db.commit()


async def save_id_file(file_id, user_id):
    async with aiosqlite.connect(ID_FILE_DB) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO id_files(file_id, user_id)
            VALUES (?, ?)
            """,
            (file_id, user_id),
        )
        await db.commit()


async def get_id_file(file_id):
    async with aiosqlite.connect(ID_FILE_DB) as db:
        async with db.execute(
            "SELECT file_id, user_id FROM id_files WHERE file_id = ?",
            (file_id,),
        ) as cursor:
            return await cursor.fetchone()


def is_telegram_url(url):
    match = re.match(r"^https?://([^/]+)", url.lower())
    if not match:
        return False

    domain = match.group(1).split(":")[0]

    return (
        domain == "t.me"
        or domain.endswith(".t.me")
        or domain == "telegram.me"
        or domain.endswith(".telegram.me")
        or domain == "telegram.dog"
        or domain.endswith(".telegram.dog")
    )


EN_UPPER = set("ATGUFNJML")
RU_UPPER = set("АИБ")


def clean_publisher(name):
    name = unicodedata.normalize("NFC", name or "")
    result = []

    for char in name:
        if char.isascii() and char.isalpha():
            upper = char.upper()
            result.append(
                upper if upper in EN_UPPER else char.lower()
            )
        elif "\u0400" <= char <= "\u04FF":
            upper = char.upper()
            result.append(
                upper if upper in RU_UPPER else char.lower()
            )
        elif char.isdigit() or char in "_ ":
            result.append(char)

    value = "".join(result).strip()
    return value or "unknown"


def clean_title(name):
    value = unicodedata.normalize("NFC", name or "")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", value)
    return value.strip() or "unknown"


def make_filename(publisher, title, extension):
    publisher = clean_publisher(publisher)
    title = clean_title(title)
    extension = re.sub(
        r"[^A-Za-z0-9]+",
        "",
        extension.lower(),
    )

    if not extension:
        extension = "bin"

    return f"{publisher} - {title}.{extension}"


def parse_time_to_seconds(time_str):
    parts = time_str.split(":")
    if len(parts) == 2:
        minutes, seconds = map(int, parts)
        return minutes * 60 + seconds
    elif len(parts) == 3:
        hours, minutes, seconds = map(int, parts)
        return hours * 3600 + minutes * 60 + seconds
    return None


def format_time_for_ffmpeg(time_str):
    parts = time_str.split(":")
    if len(parts) == 2:
        return f"00:{int(parts[0]):02d}:{int(parts[1]):02d}"
    elif len(parts) == 3:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
    return time_str


def get_session_state(chat_id: int, user_id: int):
    key = (chat_id, user_id)
    if key not in sessions:
        sessions[key] = {
            "mode": "normal",
            "queue": asyncio.Queue(maxsize=MAX_QUEUE),
            "running": 0,
            "tasks": set(),
            "reply_index": 0,
            "reply_styles": BUTTON_STYLES.copy(),
        }

    return sessions[key]


def get_rotating_reply(chat_id: int, user_id: int):
    state = get_session_state(chat_id, user_id)

    reply = ROTATING_REPLIES[state["reply_index"]]

    state["reply_index"] = (
        state["reply_index"] + 1
    ) % len(ROTATING_REPLIES)

    if not state["reply_styles"]:
        state["reply_styles"] = BUTTON_STYLES.copy()

    button_style = state["reply_styles"].pop(0)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="مولاي",
                    url="tg://user?id=8554632449",
                    style=button_style,
                )
            ]
        ]
    )

    return reply, keyboard


def mode_keyboard(chat_id: int, user_id: int):
    mode = get_session_state(chat_id, user_id)["mode"]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="صوت",
                    callback_data="mode_audio",
                    style=(
                        "success"
                        if mode == "audio"
                        else "danger"
                    ),
                ),
                InlineKeyboardButton(
                    text="ستيكر",
                    callback_data="mode_sticker",
                    style=(
                        "success"
                        if mode == "sticker"
                        else "danger"
                    ),
                ),
                InlineKeyboardButton(
                    text="الافتراضي",
                    callback_data="mode_normal",
                    style=(
                        "success"
                        if mode == "normal"
                        else "danger"
                    ),
                ),
            ],
        ]
    )


async def update_progress(
    status_message,
    percent,
    progress_state,
    loop,
):
    if not percent:
        return

    percent = min(100, max(0, int(percent)))
    step = (percent // 5) * 5

    if step < 10 or step == progress_state["last"]:
        return

    progress_state["last"] = step

    async def edit():
        try:
            await status_message.edit_text(
                "يتم العثور على طلبك دادور انتظر\n"
                f"اي هذا {step}%"
            )
        except Exception:
            pass

    asyncio.run_coroutine_threadsafe(edit(), loop)


def build_yt_options(fmt, folder, progress_hook):
    return {
        "format": fmt,
        "outtmpl": str(folder / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "socket_timeout": 12,
        "progress_hooks": [progress_hook],
    }


def download(
    url,
    folder,
    mode,
    status_message,
    loop,
):
    progress_state = {"last": 0}

    def progress_hook(data):
        if data.get("status") != "downloading":
            return

        total = (
            data.get("total_bytes")
            or data.get("total_bytes_estimate")
        )
        downloaded = data.get("downloaded_bytes", 0)

        if not total:
            return

        percent = downloaded / total * 100

        asyncio.run_coroutine_threadsafe(
            update_progress(
                status_message,
                percent,
                progress_state,
                loop,
            ),
            loop,
        )

    if mode == "audio":
        fmt = "bestaudio/best"
    elif mode == "sticker":
        fmt = "bestvideo/best"
    else:
        fmt = "bestvideo+bestaudio/best"

    options = build_yt_options(
        fmt,
        folder,
        progress_hook,
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        return path, info


def search_youtube(
    query,
    folder,
    status_message,
    loop,
):
    progress_state = {"last": 0}

    def progress_hook(data):
        if data.get("status") != "downloading":
            return

        total = (
            data.get("total_bytes")
            or data.get("total_bytes_estimate")
        )
        downloaded = data.get("downloaded_bytes", 0)

        if not total:
            return

        percent = downloaded / total * 100

        asyncio.run_coroutine_threadsafe(
            update_progress(
                status_message,
                percent,
                progress_state,
                loop,
            ),
            loop,
        )

    search_options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "socket_timeout": 12,
    }

    with yt_dlp.YoutubeDL(search_options) as ydl:
        info = ydl.extract_info(
            f"ytsearch1:{query}",
            download=False,
        )

        entries = info.get("entries") or []
        if not entries:
            raise LookupError("No YouTube result")

        video_url = entries[0].get("url") or entries[0].get("webpage_url")

    download_options = build_yt_options(
        "bestaudio/best",
        folder,
        progress_hook,
    )

    with yt_dlp.YoutubeDL(download_options) as ydl:
        video_info = ydl.extract_info(video_url, download=True)
        downloaded_path = Path(ydl.prepare_filename(video_info))

        if not downloaded_path.exists():
            for file in folder.glob(f"{downloaded_path.stem}.*"):
                downloaded_path = file
                break

        return downloaded_path, video_info


async def convert_to_voice(
    input_path,
    output_path,
):
    async with asyncio.timeout(12):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-c:a",
            "libopus",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        code = await process.wait()

        if code != 0 or not output_path.exists():
            raise RuntimeError(
                "FFmpeg voice conversion failed"
            )


async def trim_voice(
    input_path,
    output_path,
    start_str,
    end_str,
):
    start_formatted = format_time_for_ffmpeg(start_str)
    end_formatted = format_time_for_ffmpeg(end_str)

    async with asyncio.timeout(12):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss",
            start_formatted,
            "-to",
            end_formatted,
            "-i",
            str(input_path),
            "-vn",
            "-c:a",
            "libopus",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        code = await process.wait()

        if code != 0 or not output_path.exists():
            raise RuntimeError(
                "FFmpeg trim failed"
            )


async def convert_to_sticker(
    input_path,
    output_path,
):
    async with asyncio.timeout(12):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-an",
            "-c:v",
            "copy",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        code = await process.wait()

        if code != 0 or not output_path.exists():
            raise RuntimeError(
                "FFmpeg sticker conversion failed"
            )


async def send_voice_file(
    bot,
    target_id,
    path,
    reply_to,
):
    async with asyncio.timeout(12):
        message = await bot.send_voice(
            chat_id=target_id,
            voice=FSInputFile(path),
            reply_to_message_id=reply_to,
        )
        return message


async def send_animation_file(
    bot,
    target_id,
    path,
    reply_to,
):
    async with asyncio.timeout(12):
        message = await bot.send_animation(
            chat_id=target_id,
            animation=FSInputFile(path),
            reply_to_message_id=reply_to,
        )
        return message


async def send_document_file(
    bot,
    target_id,
    path,
    filename,
    reply_to,
):
    async with asyncio.timeout(12):
        return await bot.send_document(
            chat_id=target_id,
            document=FSInputFile(
                path,
                filename=filename,
            ),
            reply_to_message_id=reply_to,
        )


async def clean_download_folder(folder):
    if not folder.exists():
        return

    for item in folder.iterdir():
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        except Exception:
            pass


async def process_download(
    chat_id,
    user_id,
    url,
    bot,
    mode,
    original_message,
    status_message,
):
    folder = DOWNLOAD_DIR / f"{chat_id}_{user_id}"
    folder.mkdir(parents=True, exist_ok=True)

    path = None
    voice_path = None
    sticker_path = None

    loop = asyncio.get_running_loop()

    try:
        async with asyncio.timeout(12):
            path, info = await asyncio.to_thread(
                download,
                url,
                folder,
                mode,
                status_message,
                loop,
            )

        publisher = (
            info.get("uploader")
            or info.get("channel")
            or "unknown"
        )

        title = info.get("title") or path.stem

        if mode == "audio":
            voice_path = folder / (
                f"{clean_publisher(publisher)}.ogg"
            )

            await convert_to_voice(
                path,
                voice_path,
            )

            sent = await send_voice_file(
                bot,
                chat_id,
                voice_path,
                original_message.message_id,
            )

            if sent.voice and sent.voice.file_id:
                await save_id_file(
                    sent.voice.file_id,
                    user_id,
                )

        elif mode == "sticker":
            sticker_path = folder / "animation.mp4"

            await convert_to_sticker(
                path,
                sticker_path,
            )

            await send_animation_file(
                bot,
                chat_id,
                sticker_path,
                original_message.message_id,
            )

        else:
            extension = (
                path.suffix
                .lstrip(".")
                .lower()
            )

            filename = make_filename(
                publisher,
                title,
                extension,
            )

            await send_document_file(
                bot,
                chat_id,
                path,
                filename,
                original_message.message_id,
            )

        await status_message.edit_text(
            "طلبك نُفذ بدون أدنى مشكلة كل ماعليك\n"
            "هو إرسال رابط المنشور"
        )

    except asyncio.CancelledError:
        try:
            await status_message.edit_text(
                "الرابط غير مدعوم او الموقع مو مدعوم\n"
                "شم كسي يلا"
            )
        except Exception:
            pass
        raise

    except Exception:
        try:
            await status_message.edit_text(
                "الرابط غير مدعوم او الموقع مو مدعوم\n"
                "شم كسي يلا"
            )
        except Exception:
            pass

    finally:
        await clean_download_folder(folder)


async def process_youtube_query(
    chat_id,
    user_id,
    query,
    bot,
    original_message,
    status_message,
):
    folder = DOWNLOAD_DIR / f"{chat_id}_{user_id}"
    folder.mkdir(parents=True, exist_ok=True)

    path = None
    voice_path = None
    loop = asyncio.get_running_loop()

    try:
        await status_message.edit_text(
            f"يتم العثور على {query}\n"
            "اي هذا 0%"
        )

        async with asyncio.timeout(12):
            path, info = await asyncio.to_thread(
                search_youtube,
                query,
                folder,
                status_message,
                loop,
            )

        publisher = (
            info.get("uploader")
            or info.get("channel")
            or "unknown"
        )

        voice_path = folder / (
            f"{clean_publisher(publisher)}.ogg"
        )

        await convert_to_voice(
            path,
            voice_path,
        )

        sent = await send_voice_file(
            bot,
            chat_id,
            voice_path,
            original_message.message_id,
        )

        if sent.voice and sent.voice.file_id:
            await save_id_file(
                sent.voice.file_id,
                user_id,
            )

        await status_message.edit_text(
            "طلبك نُفذ بدون أدنى مشكلة كل ماعليك\n"
            "هو إرسال اسم البحث بعد يوت"
        )

    except asyncio.CancelledError:
        try:
            await status_message.edit_text(
                "الرابط غير مدعوم او الموقع مو مدعوم\n"
                "شم كسي يلا"
            )
        except Exception:
            pass
        raise

    except Exception:
        try:
            await status_message.edit_text(
                "الرابط غير مدعوم او الموقع مو مدعوم\n"
                "شم كسي يلا"
            )
        except Exception:
            pass

    finally:
        await clean_download_folder(folder)


async def cleanup_task(key, task):
    async with sessions_lock:
        state = sessions.get(key)

        if state:
            state["tasks"].discard(task)


async def run_job(key, job):
    kind = job[0]
    bot_instance = job[1]
    original_message = job[2]
    status_message = job[3]
    chat_id, user_id = key

    try:
        if kind == "url":
            await process_download(
                chat_id,
                user_id,
                job[4],
                bot_instance,
                job[5],
                original_message,
                status_message,
            )

        elif kind == "youtube":
            await process_youtube_query(
                chat_id,
                user_id,
                job[4],
                bot_instance,
                original_message,
                status_message,
            )

    finally:
        async with sessions_lock:
            state = sessions.get(key)

            if not state:
                return

            state["running"] -= 1

            if not state["queue"].empty():
                next_job = await state["queue"].get()
                state["running"] += 1

                task = asyncio.create_task(
                    run_job(
                        key,
                        next_job,
                    )
                )

                state["tasks"].add(task)

                task.add_done_callback(
                    lambda done_task,
                    k=key:
                    asyncio.create_task(
                        cleanup_task(
                            k,
                            done_task,
                        )
                    )
                )


async def add_job(chat_id, user_id, job):
    key = (chat_id, user_id)
    async with sessions_lock:
        state = get_session_state(chat_id, user_id)

        total = (
            state["running"]
            + state["queue"].qsize()
        )

        if total >= MAX_ACTIVE + MAX_QUEUE:
            return

        if state["running"] < MAX_ACTIVE:
            state["running"] += 1

            task = asyncio.create_task(
                run_job(
                    key,
                    job,
                )
            )

            state["tasks"].add(task)

            task.add_done_callback(
                lambda done_task,
                k=key:
                asyncio.create_task(
                    cleanup_task(
                        k,
                        done_task,
                    )
                )
            )

        else:
            await state["queue"].put(job)


async def add_download(
    chat_id,
    user_id,
    url,
    bot_instance,
    original_message,
):
    status_message = await original_message.reply(
        "يتم العثور على طلبك دادور انتظر\n"
        "اي هذا 0%"
    )

    state = get_session_state(chat_id, user_id)

    job = (
        "url",
        bot_instance,
        original_message,
        status_message,
        url,
        state["mode"],
    )

    await add_job(chat_id, user_id, job)


async def add_youtube_query(
    chat_id,
    user_id,
    query,
    bot_instance,
    original_message,
):
    status_message = await original_message.reply(
        f"يتم العثور على {query}\n"
        "اي هذا 0%"
    )

    job = (
        "youtube",
        bot_instance,
        original_message,
        status_message,
        query,
    )

    await add_job(chat_id, user_id, job)


def make_bot():
    if LOCAL_BOT_API:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(
                LOCAL_BOT_API,
                is_local=True,
            )
        )
        return Bot(TOKEN, session=session)

    return Bot(TOKEN)


bot = make_bot()
dp = Dispatcher()


async def notify_startup():
    target_admin_id = 8554632449
    try:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="رب العالمين",
                        url=f"tg://user?id={target_admin_id}",
                        style="primary",
                    )
                ]
            ]
        )

        await bot.send_message(
            target_admin_id,
            "اشتغل البوت مرتلخ\n"
            "استعملني ؟!",
            reply_markup=keyboard,
        )

    except Exception:
        pass


@dp.message(CommandStart())
async def start(message: Message):
    await register_chat(message.chat.id, message.chat.type)
    reply, keyboard = get_rotating_reply(
        message.chat.id,
        message.from_user.id,
    )

    await message.reply(
        reply,
        reply_markup=keyboard,
    )


@dp.message(F.text == "ادت")
async def download_settings(message: Message):
    await register_chat(message.chat.id, message.chat.type)
    user_id = message.from_user.id
    chat_id = message.chat.id

    async with sessions_lock:
        get_session_state(chat_id, user_id)

    await message.reply(
        "تستطيع تغيير وضع عمل البوت من صوت الى\n"
        "الوضع الافتراضي من هذه الأزرار",
        reply_markup=mode_keyboard(chat_id, user_id),
    )


@dp.callback_query(F.data == "mode_audio")
async def select_audio(callback: CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    async with sessions_lock:
        state = get_session_state(chat_id, user_id)

        if state["mode"] == "audio":
            state["mode"] = "normal"
        else:
            state["mode"] = "audio"

        keyboard = mode_keyboard(chat_id, user_id)

    await callback.message.edit_reply_markup(
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "mode_sticker")
async def select_sticker(callback: CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    async with sessions_lock:
        state = get_session_state(chat_id, user_id)

        if state["mode"] == "sticker":
            state["mode"] = "normal"
        else:
            state["mode"] = "sticker"

        keyboard = mode_keyboard(chat_id, user_id)

    await callback.message.edit_reply_markup(
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "mode_normal")
async def select_normal(callback: CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    async with sessions_lock:
        state = get_session_state(chat_id, user_id)

        if state["mode"] == "normal":
            await callback.answer(
                "لا يمكنك تعطيل الوضع الافتراضي\n"
                "تستطيع تبديل الوضع وليس تعطيل كل الاوضاع",
                show_alert=True,
            )
            return

        state["mode"] = "normal"
        keyboard = mode_keyboard(chat_id, user_id)

    await callback.message.edit_reply_markup(
        reply_markup=keyboard
    )

    await callback.answer()


@dp.message(
    F.reply_to_message
    & F.reply_to_message.voice
    & (F.text.lower() == "تعديل")
)
async def start_trim_from_reply(message: Message):
    await register_chat(message.chat.id, message.chat.type)
    user_id = message.from_user.id
    chat_id = message.chat.id
    voice = message.reply_to_message.voice

    record = await get_id_file(voice.file_id)

    if not record:
        await message.reply(
            "المعذرة\n"
            "لا يتوفر iD-File للرد"
        )
        return

    trim_states[(chat_id, user_id)] = {
        "file_id": voice.file_id,
        "duration": voice.duration or 0,
    }

    await message.reply(
        "اذا اردت تعديل المدة هكذا للدقائق\n"
        "00:20 00:10 وللساعات 00.0:00"
    )


@dp.message(F.voice)
async def start_trim_from_voice_upload(message: Message):
    await register_chat(message.chat.id, message.chat.type)
    user_id = message.from_user.id
    chat_id = message.chat.id
    voice = message.voice

    trim_states[(chat_id, user_id)] = {
        "file_id": voice.file_id,
        "duration": voice.duration or 0,
    }

    await message.reply(
        "اذا اردت تعديل المدة هكذا للدقائق\n"
        "00:20 00:10 وللساعات 00.0:00"
    )


@dp.message(F.text)
async def text_handler(message: Message):
    await register_chat(message.chat.id, message.chat.type)
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    key = (chat_id, user_id)

    if key in trim_states:
        state_data = trim_states.pop(key)

        time_match = re.match(
            r"^(\d{1,2}:\d{2}(?::\d{2})?)\s+(\d{1,2}:\d{2}(?::\d{2})?)$",
            text,
        )

        if not time_match:
            await message.reply(
                "توقيتك غلط تستطيع التعديل هكذا للدقائق\n"
                "00:20 00:10 وللساعات 00.0:00"
            )
            return

        start_str, end_str = time_match.groups()
        start_sec = parse_time_to_seconds(start_str)
        end_sec = parse_time_to_seconds(end_str)
        duration = state_data["duration"]

        if start_sec is None or end_sec is None or start_sec >= end_sec:
            await message.reply(
                "توقيتك غلط تستطيع التعديل هكذا للدقائق\n"
                "00:20 00:10 وللساعات 00.0:00"
            )
            return

        if duration > 0 and (start_sec >= duration or end_sec > duration):
            await message.reply(
                "توقيتك اكبر من الفويس ارسل مدة اقصر\n"
                "ههع يلا"
            )
            return

        folder = DOWNLOAD_DIR / f"{chat_id}_{user_id}"
        folder.mkdir(parents=True, exist_ok=True)

        input_voice_path = folder / "input.ogg"
        output_voice_path = folder / "trimmed.ogg"

        try:
            file_info = await bot.get_file(state_data["file_id"])
            await bot.download_file(
                file_info.file_path,
                destination=input_voice_path,
            )

            await trim_voice(
                input_voice_path,
                output_voice_path,
                start_str,
                end_str,
            )

            sent = await send_voice_file(
                bot,
                chat_id,
                output_voice_path,
                message.message_id,
            )

            if sent.voice and sent.voice.file_id:
                await save_id_file(
                    sent.voice.file_id,
                    user_id,
                )

        except Exception:
            await message.reply(
                "المعذرة لم استطع تعديل مدة الصوت\n"
                "انا اسف"
            )
        finally:
            await clean_download_folder(folder)
        return

    if text == "ادت":
        return

    if text.startswith("يوت"):
        query = text[3:].strip()

        if query:
            await add_youtube_query(
                chat_id,
                user_id,
                query,
                bot,
                message,
            )
            return

        is_group_or_channel = message.chat.type in ["group", "supergroup", "channel"]
        if not is_group_or_channel or "بوت" in text:
            reply, keyboard = get_rotating_reply(chat_id, user_id)
            await message.reply(reply, reply_markup=keyboard)
        return

    if text.startswith("ID-File"):
        file_id = text[7:].strip()

        if file_id:
            record = await get_id_file(file_id)

            if record:
                try:
                    await bot.send_voice(
                        message.chat.id,
                        voice=file_id,
                        reply_to_message_id=message.message_id,
                    )
                    return
                except Exception:
                    pass

        await message.reply(
            "المعذرة\n"
            "لا يتوفر iD-File للرد"
        )
        return

    if not re.match(r"^https?://", text):
        is_group_or_channel = message.chat.type in ["group", "supergroup", "channel"]
        if not is_group_or_channel or "بوت" in text:
            reply, keyboard = get_rotating_reply(chat_id, user_id)
            await message.reply(reply, reply_markup=keyboard)
        return

    if is_telegram_url(text):
        is_group_or_channel = message.chat.type in ["group", "supergroup", "channel"]
        if not is_group_or_channel or "بوت" in text:
            reply, keyboard = get_rotating_reply(chat_id, user_id)
            await message.reply(reply, reply_markup=keyboard)
        return

    await add_download(
        chat_id,
        user_id,
        text,
        bot,
        message,
    )


async def main():
    await init_id_file()
    await notify_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
