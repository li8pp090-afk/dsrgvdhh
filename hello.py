import os
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

URL_PROGRESS = "ههع تعال اضرط عليك\nالا تشم طيزي"

URL_FAILED = "الرابط غير مدعوم او الموقع غير مدعوم\nههع شم كسي يلا"

REPLIES = (
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
)

data_store = {}


def load_data():
    global data_store

    if not os.path.exists(DATA_FILE):
        data_store = {}
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data_store = json.load(f)
    except Exception:
        data_store = {}


def save_data():
    temp_file = DATA_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(
            data_store,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temp_file, DATA_FILE)


load_data()


def private_key(user_id):
    return f"private:{user_id}"


def chat_key(chat_id):
    return f"chat:{chat_id}"


def cache_key(url, mode):
    return f"cache:{mode}:{url}"


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


def get_cached(url, mode):
    return data_store.get(
        cache_key(url, mode)
    )


def save_cached(url, mode, value):
    data_store[cache_key(url, mode)] = value
    save_data()


def build_mode_keyboard(mode):
    voice_style = "primary" if mode == "voice" else "danger"
    default_style = "primary" if mode == "default" else "danger"

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


async def delete_previous_settings(chat_id, message_id):
    if not message_id:
        return

    try:
        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception:
        pass


async def send_settings(message, mode, owner_data):
    old_message_id = owner_data.get(
        "last_settings_message"
    )

    if old_message_id:
        await delete_previous_settings(
            message.chat.id,
            old_message_id,
        )

    settings_message = await message.reply(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=build_mode_keyboard(mode),
    )

    owner_data["last_settings_message"] = (
        settings_message.message_id
    )

    save_data()


async def is_owner(chat_id, user_id):
    try:
        member = await bot.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status == "creator"

    except Exception:
        return False


def extract_urls(text):
    if not text:
        return []

    result = []

    for word in text.split():
        word = word.strip("()[]{}<>\"'")

        if word.startswith("http://") or word.startswith("https://"):
            result.append(word)

    return result


def get_info_sync(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
    }

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=False,
        )


async def get_info(url):
    try:
        return await asyncio.to_thread(
            get_info_sync,
            url,
        )
    except Exception:
        return None


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

        return result.stdout.strip()

    except Exception:
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


def files_in(directory):
    return [
        p
        for p in Path(directory).rglob("*")
        if p.is_file()
    ]


def download_sync(
    url,
    directory,
    selector,
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

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=True,
        )


def choose_default_format(info):
    formats = info.get("formats") or []

    has_video = any(
        f.get("vcodec")
        and f.get("vcodec") != "none"
        for f in formats
    )

    has_audio = any(
        f.get("acodec")
        and f.get("acodec") != "none"
        for f in formats
    )

    if has_video and has_audio:
        return "bestvideo*+bestaudio/best"

    if has_video:
        return "bestvideo"

    if has_audio:
        return "bestaudio"

    return "best"


def choose_audio_format(info):
    formats = info.get("formats") or []

    has_audio = any(
        f.get("acodec")
        and f.get("acodec") != "none"
        for f in formats
    )

    if has_audio:
        return "bestaudio"

    return "best"


async def download_default(url, directory):
    info = await get_info(url)

    if not info:
        return []

    selector = choose_default_format(info)

    try:
        await asyncio.to_thread(
            download_sync,
            url,
            directory,
            selector,
        )
    except Exception:
        return []

    return files_in(directory)


async def download_audio(url, directory):
    info = await get_info(url)

    if not info:
        return []

    selector = choose_audio_format(info)

    try:
        await asyncio.to_thread(
            download_sync,
            url,
            directory,
            selector,
        )
    except Exception:
        return []

    return files_in(directory)


async def convert_voice(source, destination):
    process = await asyncio.create_subprocess_exec(
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

    await process.communicate()

    return (
        process.returncode == 0
        and os.path.exists(destination)
    )


async def send_cached(message, cached):
    if not cached:
        return False

    kind = cached.get("type")

    try:
        if kind == "voice":
            await message.reply_voice(
                cached["file_id"]
            )
            return True

        if kind == "photo":
            await message.reply_photo(
                cached["file_id"]
            )
            return True

        if kind == "video":
            await message.reply_video(
                cached["file_id"]
            )
            return True

        if kind == "audio":
            await message.reply_audio(
                cached["file_id"]
            )
            return True

        if kind == "document":
            await message.reply_document(
                cached["file_id"]
            )
            return True

        if kind == "album":
            items = cached.get("items") or []

            if not items:
                return False

            previous_message_id = message.message_id

            for start in range(0, len(items), 10):
                batch = items[start:start + 10]
                media = []

                for item in batch:
                    if item["type"] == "photo":
                        media.append(
                            InputMediaPhoto(
                                media=item["file_id"]
                            )
                        )
                    elif item["type"] == "video":
                        media.append(
                            InputMediaVideo(
                                media=item["file_id"]
                            )
                        )

                if not media:
                    continue

                sent = await bot.send_media_group(
                    chat_id=message.chat.id,
                    media=media,
                    reply_to_message_id=previous_message_id,
                )

                if sent:
                    previous_message_id = (
                        sent[-1].message_id
                    )

            return True

    except Exception:
        return False

    return False


async def send_single_file(message, path):
    kind = media_type(path)

    if kind == "image":
        sent = await message.reply_photo(
            FSInputFile(path)
        )

        return {
            "type": "photo",
            "file_id": sent.photo[-1].file_id,
        }

    if kind == "video":
        sent = await message.reply_video(
            FSInputFile(path)
        )

        return {
            "type": "video",
            "file_id": sent.video.file_id,
        }

    if kind == "audio":
        sent = await message.reply_audio(
            FSInputFile(path)
        )

        return {
            "type": "audio",
            "file_id": sent.audio.file_id,
        }

    sent = await message.reply_document(
        FSInputFile(path)
    )

    return {
        "type": "document",
        "file_id": sent.document.file_id,
    }


async def send_voice_file(message, path):
    with tempfile.TemporaryDirectory() as directory:
        destination = os.path.join(
            directory,
            "voice.ogg",
        )

        if not await convert_voice(
            path,
            destination,
        ):
            return None

        sent = await message.reply_voice(
            FSInputFile(destination)
        )

        return {
            "type": "voice",
            "file_id": sent.voice.file_id,
        }


async def send_album(message, files):
    media_files = [
        f
        for f in files
        if media_type(f) in {
            "image",
            "video",
        }
    ]

    if not media_files:
        return None

    if len(media_files) == 1:
        return await send_single_file(
            message,
            media_files[0],
        )

    cached_items = []
    previous_message_id = message.message_id

    for start in range(0, len(media_files), 10):
        batch = media_files[start:start + 10]

        telegram_media = []

        for path in batch:
            kind = media_type(path)

            if kind == "image":
                telegram_media.append(
                    InputMediaPhoto(
                        media=FSInputFile(path)
                    )
                )

            elif kind == "video":
                telegram_media.append(
                    InputMediaVideo(
                        media=FSInputFile(path)
                    )
                )

        if not telegram_media:
            continue

        sent = await bot.send_media_group(
            chat_id=message.chat.id,
            media=telegram_media,
            reply_to_message_id=previous_message_id,
        )

        if not sent:
            continue

        previous_message_id = sent[-1].message_id

        for msg in sent:
            if msg.photo:
                cached_items.append(
                    {
                        "type": "photo",
                        "file_id": msg.photo[-1].file_id,
                    }
                )
            elif msg.video:
                cached_items.append(
                    {
                        "type": "video",
                        "file_id": msg.video.file_id,
                    }
                )

    if not cached_items:
        return None

    return {
        "type": "album",
        "items": cached_items,
    }


async def process_url(message, url, mode):
    cached = get_cached(
        url,
        mode,
    )

    if cached:
        if await send_cached(
            message,
            cached,
        ):
            return

    progress = await message.reply(
        URL_PROGRESS
    )

    with tempfile.TemporaryDirectory() as directory:
        if mode == "voice":
            files = await download_audio(
                url,
                directory,
            )
        else:
            files = await download_default(
                url,
                directory,
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
            audio_files = [
                f
                for f in files
                if media_type(f) == "audio"
            ]

            if not audio_files:
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

            cached_result = await send_voice_file(
                message,
                audio_files[0],
            )

            if not cached_result:
                await message.reply(
                    URL_FAILED
                )
                return

            save_cached(
                url,
                mode,
                cached_result,
            )

            return

        media_files = [
            f
            for f in files
            if media_type(f) in {
                "image",
                "video",
            }
        ]

        if not media_files:
            audio_files = [
                f
                for f in files
                if media_type(f) == "audio"
            ]

            document_files = [
                f
                for f in files
                if media_type(f) == "document"
            ]

            try:
                await progress.delete()
            except Exception:
                pass

            if audio_files:
                result = await send_single_file(
                    message,
                    audio_files[0],
                )

                save_cached(
                    url,
                    mode,
                    result,
                )

                return

            if document_files:
                result = await send_single_file(
                    message,
                    document_files[0],
                )

                save_cached(
                    url,
                    mode,
                    result,
                )

                return

            await message.reply(
                URL_FAILED
            )

            return

        try:
            await progress.delete()
        except Exception:
            pass

        cached_result = await send_album(
            message,
            media_files,
        )

        if not cached_result:
            await message.reply(
                URL_FAILED
            )
            return

        save_cached(
            url,
            mode,
            cached_result,
        )


@router.message()
async def message_handler(message: Message):
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

        urls = extract_urls(text)

        if urls:
            for url in urls:
                await process_url(
                    message,
                    url,
                    private["mode"],
                )

            return

        index = private["reply_index"]

        await message.reply(
            REPLIES[index]
        )

        private["reply_index"] = (
            index + 1
        ) % len(REPLIES)

        save_data()

        return

    if message.chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        if not message.from_user:
            return

        chat_id = message.chat.id
        user_id = message.from_user.id

        chat = get_chat_data(chat_id)

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

            index = user_data["reply_index"]

            await message.reply(
                REPLIES[index]
            )

            user_data["reply_index"] = (
                index + 1
            ) % len(REPLIES)

            save_data()

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

    if message.chat.type == ChatType.CHANNEL:
        chat_id = message.chat.id
        chat = get_chat_data(chat_id)

        if chat["disabled"]:
            return

        urls = extract_urls(text)

        if urls:
            for url in urls:
                await process_url(
                    message,
                    url,
                    chat["mode"],
                )


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
                "ليس مصرح لك\nبالقيام بهذا",
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
            "الوضع الافتراضي مفعل بالفعل",
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

    owner_data["last_settings_message"] = (
        callback.message.message_id
    )

    save_data()

    await callback.answer()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())