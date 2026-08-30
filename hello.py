import os
import re
import json
import asyncio
import tempfile
import subprocess
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
)
from yt_dlp import YoutubeDL


TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(TOKEN)
dp = Dispatcher()
router = Router()

dp.include_router(router)

DATA_FILE = "id-file.json"

YOUTUBE_PREFIX = "يوت "

URL_FAILED = (
    "الرابط غير مدعوم او الموقع غير مدعوم\n"
    "ههع شم كسي يلا"
)

YOUTUBE_FAILED = (
    "المعذرة منك\n"
    "ماقدرت اوفرلك هذا العنوان"
)

SPECIAL_LETTERS = set("atgnmfjlu")

REPLIES = (
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
)

data_store = {}


# =========================
# DATA
# =========================

def load_data():
    global data_store

    if not os.path.exists(DATA_FILE):
        data_store = {}
        return

    try:
        with open(
            DATA_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data_store = json.load(f)
    except Exception:
        data_store = {}


def save_data():
    temp_file = DATA_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data_store,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp_file,
        DATA_FILE,
    )


load_data()


def private_key(user_id):
    return f"private:{user_id}"


def chat_key(chat_id):
    return f"chat:{chat_id}"


def get_private_data(user_id):
    key = private_key(user_id)

    if key not in data_store:
        data_store[key] = {
            "mode": "default",
            "reply_index": 0,
            "last_settings_message": None,
        }

    return data_store[key]


def get_chat_data(chat_id):
    key = chat_key(chat_id)

    if key not in data_store:
        data_store[key] = {
            "mode": "default",
            "disabled": False,
            "reply_users": {},
            "last_settings_message": None,
        }

    return data_store[key]


def get_user_reply_data(chat_id, user_id):
    chat_data = get_chat_data(chat_id)

    user_key = str(user_id)

    if user_key not in chat_data["reply_users"]:
        chat_data["reply_users"][user_key] = {
            "reply_index": 0,
        }

    return chat_data["reply_users"][user_key]


# =========================
# SETTINGS
# =========================

def build_mode_keyboard(mode):
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


async def delete_previous_settings(
    chat_id,
    message_id,
):
    if not message_id:
        return

    try:
        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception:
        pass


async def send_settings(
    message,
    mode,
    owner_data,
):
    old_message_id = owner_data.get(
        "last_settings_message"
    )

    if old_message_id:
        await delete_previous_settings(
            message.chat.id,
            old_message_id,
        )

    settings_message = await message.reply(
        "تستطيع تغيير وضع عمل البوت\n"
        "من هنا",
        reply_markup=build_mode_keyboard(
            mode
        ),
    )

    owner_data[
        "last_settings_message"
    ] = settings_message.message_id

    save_data()


# =========================
# OWNER
# =========================

async def is_owner(
    chat_id,
    user_id,
):
    try:
        member = await bot.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status == "creator"

    except Exception:
        return False


# =========================
# URL
# =========================

def extract_urls(text):
    if not text:
        return []

    result = []

    for word in text.split():
        word = word.strip(
            "()[]{}<>\"'"
        )

        if (
            word.startswith("http://")
            or word.startswith("https://")
        ):
            result.append(word)

    return result


def normalize_text(text):
    text = text.casefold()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# =========================
# YOUTUBE
# =========================

def popularity_key(info):
    views = info.get("view_count") or 0
    likes = info.get("like_count") or 0
    comments = info.get("comment_count") or 0

    return (
        views,
        likes,
        comments,
    )


def select_youtube_result(results):
    valid = [
        item
        for item in results
        if isinstance(item, dict)
        and item.get("id")
    ]

    if not valid:
        return None

    valid.sort(
        key=popularity_key,
        reverse=True,
    )

    return valid[0]


def format_reply_title(title):
    result = title.lower()

    for char in SPECIAL_LETTERS:
        result = result.replace(
            char,
            char.upper(),
        )

    return result


async def youtube_search(query):
    def search():
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
        }

        with YoutubeDL(options) as ydl:
            result = ydl.extract_info(
                f"ytsearch20:{query}",
                download=False,
            )

        return result.get("entries") or []

    try:
        results = await asyncio.to_thread(
            search
        )

        return select_youtube_result(
            results
        )

    except Exception:
        return None


# =========================
# MIME
# =========================

def get_mime(path):
    try:
        result = subprocess.run(
            [
                "file",
                "--mime-type",
                "-b",
                str(path),
            ],
            capture_output=True,
            text=True,
        )

        mime = result.stdout.strip()

        if mime:
            return mime

    except Exception:
        pass

    return ""


def media_type(path):
    mime = get_mime(path)

    if mime.startswith("image/"):
        return "image"

    if mime.startswith("video/"):
        return "video"

    if mime.startswith("audio/"):
        return "audio"

    return "document"


# =========================
# FILES
# =========================

def files_in(directory):
    return [
        p
        for p in Path(directory).rglob("*")
        if p.is_file()
    ]


# =========================
# YT-DLP INFO
# =========================

def inspect_url(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=False,
        )


async def get_info(url):
    try:
        return await asyncio.to_thread(
            inspect_url,
            url,
        )
    except Exception:
        return None


# =========================
# FORMAT SELECTION
# =========================

def best_audio(info):
    formats = info.get("formats") or []

    audio = [
        f
        for f in formats
        if f.get("acodec")
        and f.get("acodec") != "none"
        and (
            not f.get("vcodec")
            or f.get("vcodec") == "none"
        )
    ]

    if not audio:
        return None

    audio.sort(
        key=lambda f: (
            f.get("abr") or 0,
            f.get("tbr") or 0,
            f.get("filesize") or 0,
        ),
        reverse=True,
    )

    return audio[0]


def best_video(info):
    formats = info.get("formats") or []

    combined = [
        f
        for f in formats
        if f.get("vcodec")
        and f.get("vcodec") != "none"
        and f.get("acodec")
        and f.get("acodec") != "none"
    ]

    separate = [
        f
        for f in formats
        if f.get("vcodec")
        and f.get("vcodec") != "none"
        and (
            not f.get("acodec")
            or f.get("acodec") == "none"
        )
    ]

    combined.sort(
        key=lambda f: (
            f.get("height") or 0,
            f.get("tbr") or 0,
            f.get("filesize") or 0,
        ),
        reverse=True,
    )

    separate.sort(
        key=lambda f: (
            f.get("height") or 0,
            f.get("tbr") or 0,
            f.get("filesize") or 0,
        ),
        reverse=True,
    )

    if combined:
        return combined[0], None

    audio = best_audio(info)

    if separate and audio:
        return separate[0], audio

    return None, None


# =========================
# DOWNLOAD
# =========================

def download(
    url,
    directory,
    selector,
    progress_hook=None,
):
    output = os.path.join(
        directory,
        "%(playlist_index)05d-%(title)s.%(ext)s",
    )

    options = {
        "format": selector,
        "outtmpl": output,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
    }

    if progress_hook:
        options["progress_hooks"] = [
            progress_hook
        ]

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=True,
        )


# =========================
# PROGRESS
# =========================

async def update_progress(
    progress_message,
    value,
):
    value = max(
        0,
        min(100, value),
    )

    value = (
        value // 25
    ) * 25

    try:
        await progress_message.edit_text(
            f"{value}%"
        )
    except Exception:
        pass


def make_progress_hook(
    progress_message,
    loop,
):
    state = {
        "value": -1,
    }

    def hook(status):
        if status.get("status") != "downloading":
            return

        total = (
            status.get("total_bytes")
            or status.get("total_bytes_estimate")
        )

        downloaded = (
            status.get("downloaded_bytes")
            or 0
        )

        if not total:
            return

        percent = int(
            downloaded * 100 / total
        )

        percent = (
            percent // 25
        ) * 25

        if percent == state["value"]:
            return

        state["value"] = percent

        asyncio.run_coroutine_threadsafe(
            update_progress(
                progress_message,
                percent,
            ),
            loop,
        )

    return hook


# =========================
# AUDIO
# =========================

async def download_audio(
    url,
    directory,
    progress_message,
):
    info = await get_info(url)

    if not info:
        return []

    audio = best_audio(info)

    if not audio:
        return []

    loop = asyncio.get_running_loop()

    hook = make_progress_hook(
        progress_message,
        loop,
    )

    try:
        await asyncio.to_thread(
            download,
            url,
            directory,
            audio["format_id"],
            hook,
        )
    except Exception:
        return []

    return files_in(directory)


# =========================
# VIDEO
# =========================

async def download_video(
    url,
    directory,
    progress_message,
):
    info = await get_info(url)

    if not info:
        return []

    video, audio = best_video(info)

    if not video:
        return []

    if audio:
        selector = (
            f'{video["format_id"]}+'
            f'{audio["format_id"]}'
        )
    else:
        selector = video["format_id"]

    loop = asyncio.get_running_loop()

    hook = make_progress_hook(
        progress_message,
        loop,
    )

    try:
        await asyncio.to_thread(
            download,
            url,
            directory,
            selector,
            hook,
        )
    except Exception:
        return []

    return files_in(directory)


# =========================
# VOICE
# =========================

async def convert_voice(
    source,
    destination,
):
    process = (
        await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "libopus",
            destination,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )

    await process.communicate()

    return (
        process.returncode == 0
        and os.path.exists(destination)
    )


async def send_voice(
    message,
    source,
):
    with tempfile.TemporaryDirectory() as d:
        destination = os.path.join(
            d,
            "voice.ogg",
        )

        if not await convert_voice(
            source,
            destination,
        ):
            return None

        return await message.reply_voice(
            FSInputFile(destination)
        )


# =========================
# MEDIA
# =========================

async def send_media(
    message,
    files,
):
    media = [
        f
        for f in files
        if media_type(f)
        in {"image", "video"}
    ]

    if not media:
        return False

    if len(media) == 1:
        path = media[0]

        if media_type(path) == "image":
            await message.reply_photo(
                FSInputFile(path)
            )
        else:
            await message.reply_video(
                FSInputFile(path)
            )

        return True

    batches = [
        media[i:i + 10]
        for i in range(
            0,
            len(media),
            10,
        )
    ]

    previous_message_ids = []

    for index, batch in enumerate(batches):
        telegram_media = []

        for path in batch:
            if media_type(path) == "image":
                telegram_media.append(
                    InputMediaPhoto(
                        media=FSInputFile(path)
                    )
                )
            else:
                telegram_media.append(
                    InputMediaVideo(
                        media=FSInputFile(path)
                    )
                )

        sent = await bot.send_media_group(
            chat_id=message.chat.id,
            media=telegram_media,
            reply_to_message_id=(
                message.message_id
                if index == 0
                else previous_message_ids[-1]
            ),
        )

        if sent:
            previous_message_ids.append(
                sent[-1].message_id
            )

    return True


# =========================
# URL PROCESSING
# =========================

async def process_url(
    message,
    url,
    mode,
):
    progress = await message.reply(
        "0%"
    )

    with tempfile.TemporaryDirectory() as d:

        if mode == "voice":
            files = await download_audio(
                url,
                d,
                progress,
            )
        else:
            files = await download_video(
                url,
                d,
                progress,
            )

        if not files:
            try:
                await progress.edit_text(
                    URL_FAILED
                )
            except Exception:
                pass

            return

        if mode == "voice":
            audio = [
                f
                for f in files
                if media_type(f) == "audio"
            ]

            if not audio:
                try:
                    await progress.edit_text(
                        URL_FAILED
                    )
                except Exception:
                    pass

                return

            try:
                await progress.delete()
            except Exception:
                pass

            await send_voice(
                message,
                audio[0],
            )

            return

        media = [
            f
            for f in files
            if media_type(f)
            in {"image", "video"}
        ]

        if not media:
            try:
                await progress.edit_text(
                    URL_FAILED
                )
            except Exception:
                pass

            return

        try:
            await progress.delete()
        except Exception:
            pass

        await send_media(
            message,
            media,
        )


# =========================
# YOUTUBE PROCESSING
# =========================

async def process_youtube(
    message,
    query,
):
    result = await youtube_search(
        query
    )

    if not result:
        await message.reply(
            YOUTUBE_FAILED
        )
        return

    title = (
        result.get("title")
        or query
    )

    reply_title = format_reply_title(
        title
    )

    result_message = await message.reply(
        f"ها تريد {reply_title}\n"
        "تدلل عبي 🍧"
    )

    video_id = result.get("id")

    if not video_id:
        await result_message.edit_text(
            YOUTUBE_FAILED
        )
        return

    url = (
        result.get("webpage_url")
        or f"https://www.youtube.com/watch?v={video_id}"
    )

    with tempfile.TemporaryDirectory() as d:

        info = await get_info(url)

        if not info:
            await result_message.edit_text(
                YOUTUBE_FAILED
            )
            return

        audio = best_audio(info)

        if not audio:
            await result_message.edit_text(
                YOUTUBE_FAILED
            )
            return

        try:
            await asyncio.to_thread(
                download,
                url,
                d,
                audio["format_id"],
            )
        except Exception:
            await result_message.edit_text(
                YOUTUBE_FAILED
            )
            return

        files = files_in(d)

        audio_files = [
            f
            for f in files
            if media_type(f) == "audio"
        ]

        if not audio_files:
            await result_message.edit_text(
                YOUTUBE_FAILED
            )
            return

        with tempfile.TemporaryDirectory() as vd:

            voice_path = os.path.join(
                vd,
                "voice.ogg",
            )

            if not await convert_voice(
                audio_files[0],
                voice_path,
            ):
                await result_message.edit_text(
                    YOUTUBE_FAILED
                )
                return

            try:
                await result_message.delete()
            except Exception:
                pass

            try:
                await message.reply_voice(
                    FSInputFile(voice_path)
                )
            except Exception:
                await message.reply(
                    YOUTUBE_FAILED
                )


# =========================
# PRIVATE
# =========================

@router.message()
async def message_handler(
    message: Message,
):
    text = (
        message.text.strip()
        if message.text
        else ""
    )

    if message.chat.type == ChatType.PRIVATE:

        user_id = message.from_user.id

        private = get_private_data(
            user_id
        )

        if text == "ادت":
            await send_settings(
                message,
                private["mode"],
                private,
            )
            return

        if text.startswith(
            YOUTUBE_PREFIX
        ):
            query = text[
                len(YOUTUBE_PREFIX):
            ].strip()

            if query:
                await process_youtube(
                    message,
                    query,
                )

            return

        urls = extract_urls(text)

        if urls:
            for url in urls:
                await process_url(
                    message,
                    url,
                    private["mode"],
                )

            return

        index = private[
            "reply_index"
        ]

        await message.reply(
            REPLIES[index]
        )

        private[
            "reply_index"
        ] = (
            index + 1
        ) % len(REPLIES)

        save_data()

        return


    # =====================
    # GROUP / SUPERGROUP
    # =====================

    if message.chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:

        if not message.from_user:
            return

        chat_id = message.chat.id
        user_id = message.from_user.id

        chat = get_chat_data(
            chat_id
        )

        if text == "ادت":

            if await is_owner(
                chat_id,
                user_id,
            ):
                await send_settings(
                    message,
                    chat["mode"],
                    chat,
                )

            return

        if text == "تعطيل":

            if await is_owner(
                chat_id,
                user_id,
            ):
                chat["disabled"] = True

                save_data()

                await message.reply(
                    "تم تعطيل عمل البوت\n"
                    "لن يعمل بعد الان الا اذا صدر منك امر تفعيل"
                )

            return

        if text == "تفعيل":

            if await is_owner(
                chat_id,
                user_id,
            ):
                chat["disabled"] = False

                save_data()

                await message.reply(
                    "البوت يعمل بدون مشاكل\n"
                    "لن يتعطل بعد الان الا اذا صدر منك امر تعطيل"
                )

            return

        if chat["disabled"]:
            return

        if text == "بوت":

            user_data = get_user_reply_data(
                chat_id,
                user_id,
            )

            index = user_data[
                "reply_index"
            ]

            await message.reply(
                REPLIES[index]
            )

            user_data[
                "reply_index"
            ] = (
                index + 1
            ) % len(REPLIES)

            save_data()

            return

        if text.startswith(
            YOUTUBE_PREFIX
        ):
            query = text[
                len(YOUTUBE_PREFIX):
            ].strip()

            if query:
                await process_youtube(
                    message,
                    query,
                )

            return

        urls = extract_urls(text)

        if urls:
            for url in urls:
                await process_url(
                    message,
                    url,
                    chat["mode"],
                )

            return

        return


    # =====================
    # CHANNEL
    # =====================

    if message.chat.type == ChatType.CHANNEL:

        chat_id = message.chat.id

        chat = get_chat_data(
            chat_id
        )

        if chat["disabled"]:
            return

        if text == "بوت":

            user_id = (
                message.from_user.id
                if message.from_user
                else chat_id
            )

            user_data = get_user_reply_data(
                chat_id,
                user_id,
            )

            index = user_data[
                "reply_index"
            ]

            await message.reply(
                REPLIES[index]
            )

            user_data[
                "reply_index"
            ] = (
                index + 1
            ) % len(REPLIES)

            save_data()

            return

        if text.startswith(
            YOUTUBE_PREFIX
        ):
            query = text[
                len(YOUTUBE_PREFIX):
            ].strip()

            if query:
                await process_youtube(
                    message,
                    query,
                )

            return

        urls = extract_urls(text)

        if urls:
            for url in urls:
                await process_url(
                    message,
                    url,
                    chat["mode"],
                )


# =========================
# CALLBACKS
# =========================

@router.callback_query(
    lambda c:
    c.data in {
        "mode:voice",
        "mode:default",
    }
)
async def mode_callback(
    callback: CallbackQuery,
):

    if not callback.message:
        return

    chat = callback.message.chat
    user_id = callback.from_user.id

    selected = callback.data.split(
        ":",
        1,
    )[1]

    if chat.type == ChatType.PRIVATE:

        owner_data = get_private_data(
            user_id
        )

    elif chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:

        if not await is_owner(
            chat.id,
            user_id,
        ):
            await callback.answer(
                "ليس مصرح لك\n"
                "بالقيام بهذا",
                show_alert=True,
            )
            return

        owner_data = get_chat_data(
            chat.id
        )

    else:
        await callback.answer()
        return

    current = owner_data["mode"]

    if (
        current == "default"
        and selected == "default"
    ):
        await callback.answer(
            "لا يمكنك تعطيل الوضع الافتراضي\n"
            "تستطيع تبديل الوضع وليس تعطيل كل الاوضاع",
            show_alert=True,
        )
        return

    if (
        current == "voice"
        and selected == "voice"
    ):
        owner_data["mode"] = "default"
    else:
        owner_data["mode"] = selected

    await callback.message.edit_reply_markup(
        reply_markup=build_mode_keyboard(
            owner_data["mode"]
        )
    )

    owner_data[
        "last_settings_message"
    ] = callback.message.message_id

    save_data()

    await callback.answer()


# =========================
# MAIN
# =========================

async def main():
    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(main())