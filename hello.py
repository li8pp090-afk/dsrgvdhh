import os
import re
import json
import asyncio
import mimetypes
import tempfile
import subprocess
import aiohttp
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
ID_FILE = "id-file.json"

bot = Bot(TOKEN)
dp = Dispatcher()
router = Router()

dp.include_router(router)

REPLIES = (
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
)

URL_FAILED = (
    "الرابط غير مدعوم او الموقع غير مدعوم\n"
    "ههع شم كسي يلا"
)

YOUTUBE_FAILED = (
    "المعذرة منك\n"
    "ماقدرت اوفرلك هذا العنوان"
)

YOUTUBE_PREFIX = "يوت "
SPECIAL_ENGLISH = set("atgnmfjlu")


def load_id_file():
    if not os.path.exists(ID_FILE):
        return {}

    try:
        with open(ID_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def save_id_file():
    temporary = f"{ID_FILE}.tmp"

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(
            id_file,
            file,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temporary, ID_FILE)


id_file = load_id_file()


def get_private_data(user_id):
    key = f"private:{user_id}"

    if key not in id_file:
        id_file[key] = {
            "mode": "default",
            "reply_index": 0,
        }

    return id_file[key]


def get_chat_data(chat_id):
    key = f"chat:{chat_id}"

    if key not in id_file:
        id_file[key] = {
            "mode": "default",
            "disabled": False,
            "users": {},
        }

    return id_file[key]


def get_chat_user_data(chat_id, user_id):
    data = get_chat_data(chat_id)
    key = str(user_id)

    if key not in data["users"]:
        data["users"][key] = {
            "reply_index": 0,
        }

    return data["users"][key]


def format_youtube_reply_title(title):
    result = title.lower()

    for char in SPECIAL_ENGLISH:
        result = result.replace(
            char,
            char.upper(),
        )

    return result


def normalize_search_text(text):
    return re.sub(
        r"\s+",
        " ",
        text.casefold(),
    ).strip()


def tokenize_search_text(text):
    return set(
        re.findall(
            r"[a-z0-9\u0600-\u06ff]+",
            normalize_search_text(text),
        )
    )


def calculate_title_match(query, title):
    query_tokens = tokenize_search_text(query)
    title_tokens = tokenize_search_text(title)

    if not query_tokens or not title_tokens:
        return 0.0

    matched = len(query_tokens & title_tokens)

    coverage = matched / len(query_tokens)
    precision = matched / len(title_tokens)

    return coverage * 0.7 + precision * 0.3


def calculate_popularity_score(info):
    views = info.get("view_count") or 0
    likes = info.get("like_count") or 0
    comments = info.get("comment_count") or 0

    return (
        min(views / 10_000_000, 1.0) * 0.65
        + min(likes / 500_000, 1.0) * 0.25
        + min(comments / 100_000, 1.0) * 0.10
    )


def youtube_result_score(query, info):
    title = info.get("title") or ""

    title_score = calculate_title_match(
        query,
        title,
    )

    popularity = calculate_popularity_score(
        info
    )

    exact_phrase = (
        normalize_search_text(query)
        == normalize_search_text(title)
    )

    phrase_bonus = 0.35 if exact_phrase else 0.0

    return (
        title_score * 0.55
        + popularity * 0.45
        + phrase_bonus
    )


def select_best_youtube_result(query, results):
    valid = [
        item
        for item in results
        if isinstance(item, dict)
        and item.get("id")
    ]

    if not valid:
        return None

    valid.sort(
        key=lambda item: youtube_result_score(
            query,
            item,
        ),
        reverse=True,
    )

    return valid[0]


def get_mime_type(path):
    mime, _ = mimetypes.guess_type(str(path))

    if mime:
        return mime

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

    return "application/octet-stream"


def media_kind(path):
    mime = get_mime_type(path)

    if mime.startswith("image/"):
        return "image"

    if mime.startswith("video/"):
        return "video"

    if mime.startswith("audio/"):
        return "audio"

    return "document"


def find_files(directory):
    return sorted(
        [
            path
            for path in Path(directory).rglob("*")
            if path.is_file()
        ]
    )


def extract_urls(text):
    if not text:
        return []

    urls = []

    for item in text.split():
        item = item.strip(
            "()[]{}<>\"'"
        )

        if item.startswith(
            (
                "http://",
                "https://",
            )
        ):
            urls.append(item)

    return urls


def mode_keyboard(mode):
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


async def is_chat_owner(chat_id, user_id):
    member = await bot.get_chat_member(
        chat_id,
        user_id,
    )

    return member.status == "creator"


async def send_mode_message(message, mode):
    await message.reply(
        "تستطيع تغيير وضع عمل البوت\n"
        "من هنا",
        reply_markup=mode_keyboard(mode),
    )


async def edit_progress(progress_message, value):
    value = max(
        0,
        min(
            100,
            int(value),
        ),
    )

    if value % 25 != 0:
        return

    try:
        if progress_message.text != f"{value}%":
            await progress_message.edit_text(
                f"{value}%"
            )
    except Exception:
        pass


def progress_hook_factory(progress_message):
    state = {
        "last": -1,
    }

    def hook(data):
        if data.get("status") != "downloading":
            return

        total = (
            data.get("total_bytes")
            or data.get("total_bytes_estimate")
        )

        downloaded = data.get(
            "downloaded_bytes",
            0,
        )

        if not total:
            return

        percent = int(
            downloaded * 100 / total
        )

        percent = (
            percent // 25
        ) * 25

        if percent > 100:
            percent = 100

        if percent != state["last"]:
            state["last"] = percent

            asyncio.run_coroutine_threadsafe(
                edit_progress(
                    progress_message,
                    percent,
                ),
                asyncio.get_running_loop(),
            )

    return hook


def inspect_formats(url):
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


async def get_media_info(url):
    try:
        return await asyncio.to_thread(
            inspect_formats,
            url,
        )
    except Exception:
        return None


def select_audio_format(info):
    formats = info.get("formats") or []

    audio = [
        item
        for item in formats
        if item.get("acodec")
        and item.get("acodec") != "none"
        and (
            not item.get("vcodec")
            or item.get("vcodec") == "none"
        )
    ]

    if not audio:
        return None

    audio.sort(
        key=lambda item: (
            item.get("abr") or 0,
            item.get("tbr") or 0,
            item.get("filesize") or 0,
        ),
        reverse=True,
    )

    return audio[0]


def select_video_formats(info):
    formats = info.get("formats") or []

    combined = [
        item
        for item in formats
        if item.get("vcodec")
        and item.get("vcodec") != "none"
        and item.get("acodec")
        and item.get("acodec") != "none"
    ]

    video_only = [
        item
        for item in formats
        if item.get("vcodec")
        and item.get("vcodec") != "none"
        and (
            not item.get("acodec")
            or item.get("acodec") == "none"
        )
    ]

    combined.sort(
        key=lambda item: (
            item.get("height") or 0,
            item.get("tbr") or 0,
            item.get("filesize") or 0,
        ),
        reverse=True,
    )

    video_only.sort(
        key=lambda item: (
            item.get("height") or 0,
            item.get("tbr") or 0,
            item.get("filesize") or 0,
        ),
        reverse=True,
    )

    if combined:
        return combined[0], None

    audio = select_audio_format(info)

    if video_only and audio:
        return video_only[0], audio

    return None, None


def download_format(
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


async def download_audio(
    url,
    directory,
    progress_message=None,
):
    try:
        info = await get_media_info(url)

        if not info:
            return None, []

        audio = select_audio_format(info)

        if not audio:
            return None, []

        hook = None

        if progress_message:
            hook = progress_hook_factory(
                progress_message
            )

        downloaded = await asyncio.to_thread(
            download_format,
            url,
            directory,
            audio["format_id"],
            hook,
        )

        return (
            downloaded,
            find_files(directory),
        )

    except Exception:
        return None, []


async def download_media(
    url,
    directory,
    progress_message=None,
):
    try:
        info = await get_media_info(url)

        if not info:
            return None, []

        video, audio = select_video_formats(
            info
        )

        if not video:
            return None, []

        if audio:
            selector = (
                f'{video["format_id"]}+'
                f'{audio["format_id"]}'
            )
        else:
            selector = video["format_id"]

        hook = None

        if progress_message:
            hook = progress_hook_factory(
                progress_message
            )

        downloaded = await asyncio.to_thread(
            download_format,
            url,
            directory,
            selector,
            hook,
        )

        return (
            downloaded,
            find_files(directory),
        )

    except Exception:
        return None, []


async def convert_to_voice(
    source,
    destination,
):
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


async def edit_message_to_voice(
    message,
    source,
):
    with tempfile.TemporaryDirectory() as directory:
        destination = os.path.join(
            directory,
            "voice.ogg",
        )

        success = await convert_to_voice(
            source,
            destination,
        )

        if not success:
            return False

        url = (
            f"https://api.telegram.org/"
            f"bot{TOKEN}/editMessageMedia"
        )

        media = json.dumps(
            {
                "type": "voice_note",
                "media": "attach://voice",
            }
        )

        data = aiohttp.FormData()

        data.add_field(
            "chat_id",
            str(message.chat.id),
        )

        data.add_field(
            "message_id",
            str(message.message_id),
        )

        data.add_field(
            "media",
            media,
        )

        with open(
            destination,
            "rb",
        ) as voice_file:
            data.add_field(
                "voice",
                voice_file,
                filename="voice.ogg",
                content_type="audio/ogg",
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=data,
                ) as response:
                    result = await response.json()

        return bool(
            result.get("ok")
        )


async def send_photo(
    message,
    path,
    reply_to=None,
):
    kwargs = {}

    if reply_to:
        kwargs[
            "reply_to_message_id"
        ] = reply_to

    return await message.reply_photo(
        FSInputFile(path),
        **kwargs,
    )


async def send_video(
    message,
    path,
    reply_to=None,
):
    kwargs = {}

    if reply_to:
        kwargs[
            "reply_to_message_id"
        ] = reply_to

    return await message.reply_video(
        FSInputFile(path),
        **kwargs,
    )


async def send_document(
    message,
    path,
    reply_to=None,
):
    kwargs = {}

    if reply_to:
        kwargs[
            "reply_to_message_id"
        ] = reply_to

    return await message.reply_document(
        FSInputFile(path),
        **kwargs,
    )


async def send_album(
    message,
    files,
):
    media = []

    for path in files:
        kind = media_kind(path)

        if kind == "image":
            media.append(
                InputMediaPhoto(
                    media=FSInputFile(path)
                )
            )

        elif kind == "video":
            media.append(
                InputMediaVideo(
                    media=FSInputFile(path)
                )
            )

    previous_first_message = None

    for start in range(
        0,
        len(media),
        10,
    ):
        batch = media[
            start:start + 10
        ]

        if not batch:
            continue

        reply_to = (
            previous_first_message.message_id
            if previous_first_message
            else message.message_id
        )

        sent = await bot.send_media_group(
            chat_id=message.chat.id,
            media=batch,
            reply_to_message_id=reply_to,
        )

        previous_first_message = sent[0]


async def process_voice_url(
    message,
    url,
):
    progress = await message.reply("0%")

    with tempfile.TemporaryDirectory() as directory:
        _, files = await download_audio(
            url,
            directory,
            progress,
        )

        audio_files = [
            path
            for path in files
            if media_kind(path) == "audio"
        ]

        if not audio_files:
            await progress.edit_text(
                URL_FAILED
            )
            return

        try:
            await progress.delete()
        except Exception:
            pass

        await send_voice(
            message,
            audio_files[0],
        )


async def process_default_url(
    message,
    url,
):
    progress = await message.reply("0%")

    with tempfile.TemporaryDirectory() as directory:
        _, files = await download_media(
            url,
            directory,
            progress,
        )

        if not files:
            await progress.edit_text(
                URL_FAILED
            )
            return

        images = [
            path
            for path in files
            if media_kind(path) == "image"
        ]

        videos = [
            path
            for path in files
            if media_kind(path) == "video"
        ]

        audios = [
            path
            for path in files
            if media_kind(path) == "audio"
        ]

        media = images + videos

        try:
            await progress.delete()
        except Exception:
            pass

        if len(media) == 1:
            kind = media_kind(media[0])

            if kind == "image":
                await send_photo(
                    message,
                    media[0],
                )
                return

            if kind == "video":
                await send_video(
                    message,
                    media[0],
                )
                return

        if len(media) > 1:
            await send_album(
                message,
                media,
            )
            return

        if audios:
            with tempfile.TemporaryDirectory() as voice_dir:
                destination = os.path.join(
                    voice_dir,
                    "voice.ogg",
                )

                if await convert_to_voice(
                    audios[0],
                    destination,
                ):
                    await message.reply_voice(
                        FSInputFile(destination)
                    )
                    return

        await message.reply(
            URL_FAILED
        )


async def process_urls(
    message,
    mode,
    urls,
):
    for url in urls:
        if mode == "voice":
            await process_voice_url(
                message,
                url,
            )
        else:
            await process_default_url(
                message,
                url,
            )


async def search_youtube(query):
    def run():
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
        }

        with YoutubeDL(options) as ydl:
            data = ydl.extract_info(
                f"ytsearch10:{query}",
                download=False,
            )

            return data.get(
                "entries"
            ) or []

    try:
        results = await asyncio.to_thread(
            run
        )

        return select_best_youtube_result(
            query,
            results,
        )

    except Exception:
        return None


async def process_youtube_search(
    message,
    query,
):
    result = await search_youtube(
        query
    )

    if not result:
        await message.reply(
            YOUTUBE_FAILED
        )
        return

    title = result.get(
        "title"
    ) or query

    formatted_title = (
        format_youtube_reply_title(
            title
        )
    )

    result_message = await message.reply(
        f"ها تريد {formatted_title}\n"
        "تدلل عبي 🍧"
    )

    url = (
        result.get("webpage_url")
        or result.get("url")
    )

    if not url:
        await result_message.edit_text(
            YOUTUBE_FAILED
        )
        return

    with tempfile.TemporaryDirectory() as directory:
        try:
            info = await get_media_info(
                url
            )

            if not info:
                raise RuntimeError()

            audio = select_audio_format(
                info
            )

            if not audio:
                raise RuntimeError()

            await asyncio.to_thread(
                download_format,
                url,
                directory,
                audio["format_id"],
            )

            files = find_files(
                directory
            )

            audio_files = [
                path
                for path in files
                if media_kind(path) == "audio"
            ]

            if not audio_files:
                raise RuntimeError()

            success = await edit_message_to_voice(
                result_message,
                audio_files[0],
            )

            if not success:
                raise RuntimeError()

        except Exception:
            try:
                await result_message.edit_text(
                    YOUTUBE_FAILED
                )
            except Exception:
                pass


@router.message()
async def message_handler(
    message: Message,
):
    text = (
        message.text.strip()
        if message.text
        else ""
    )

    chat_type = message.chat.type

    if chat_type == ChatType.PRIVATE:
        user_id = message.from_user.id
        data = get_private_data(
            user_id
        )

        if text.startswith(
            YOUTUBE_PREFIX
        ):
            query = text[
                len(YOUTUBE_PREFIX):
            ].strip()

            if query:
                await process_youtube_search(
                    message,
                    query,
                )

                save_id_file()
                return

        urls = extract_urls(text)

        if urls:
            await process_urls(
                message,
                data["mode"],
                urls,
            )

            save_id_file()
            return

        index = data[
            "reply_index"
        ]

        await message.reply(
            REPLIES[index]
        )

        data[
            "reply_index"
        ] = (
            index + 1
        ) % len(REPLIES)

        await send_mode_message(
            message,
            data["mode"],
        )

        save_id_file()
        return

    if chat_type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        if not message.from_user:
            return

        chat_id = message.chat.id
        user_id = message.from_user.id

        data = get_chat_data(
            chat_id
        )

        if text == "تعطيل":
            if await is_chat_owner(
                chat_id,
                user_id,
            ):
                data["disabled"] = True

                save_id_file()

                await message.reply(
                    "تم تعطيل عمل البوت\n"
                    "لن يعمل بعد الان الا اذا صدر منك امر تفعيل"
                )

            return

        if text == "تفعيل":
            if await is_chat_owner(
                chat_id,
                user_id,
            ):
                data["disabled"] = False

                save_id_file()

                await message.reply(
                    "البوت يعمل بدون مشاكل\n"
                    "لن يتعطل بعد الان الا اذا صدر منك امر تعطيل"
                )

            return

        if data["disabled"]:
            return

        if text == "بوت":
            user_data = get_chat_user_data(
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

            save_id_file()
            return

        urls = extract_urls(text)

        if urls:
            await process_urls(
                message,
                data["mode"],
                urls,
            )

            save_id_file()
            return

        if await is_chat_owner(
            chat_id,
            user_id,
        ):
            await send_mode_message(
                message,
                data["mode"],
            )

            save_id_file()

        return

    if chat_type == ChatType.CHANNEL:
        data = get_chat_data(
            message.chat.id
        )

        if data["disabled"]:
            return

        if text == "بوت":
            user_id = (
                message.from_user.id
                if message.from_user
                else message.chat.id
            )

            user_data = get_chat_user_data(
                message.chat.id,
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

            save_id_file()
            return

        urls = extract_urls(text)

        if urls:
            await process_urls(
                message,
                data["mode"],
                urls,
            )

            save_id_file()


@router.callback_query(
    lambda query: query.data in {
        "mode:voice",
        "mode:default",
    }
)
async def mode_callback_handler(
    query: CallbackQuery,
):
    if not query.message:
        return

    user_id = query.from_user.id
    chat = query.message.chat

    selected_mode = query.data.split(
        ":",
        1,
    )[1]

    if chat.type == ChatType.PRIVATE:
        data = get_private_data(
            user_id
        )

    elif chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        if not await is_chat_owner(
            chat.id,
            user_id,
        ):
            await query.answer(
                "ليس مصرح لك\n"
                "بالقيام بهذا",
                show_alert=True,
            )
            return

        data = get_chat_data(
            chat.id
        )

        if data["disabled"]:
            await query.answer()
            return

    else:
        await query.answer()
        return

    current_mode = data[
        "mode"
    ]

    if (
        current_mode == "default"
        and selected_mode == "default"
    ):
        await query.answer(
            "لا يمكنك تعطيل الوضع الافتراضي\n"
            "تستطيع تبديل الوضع وليس تعطيل كل الاوضاع",
            show_alert=True,
        )
        return

    if (
        current_mode == "voice"
        and selected_mode == "voice"
    ):
        data["mode"] = "default"
    else:
        data["mode"] = selected_mode

    save_id_file()

    await query.message.edit_reply_markup(
        reply_markup=mode_keyboard(
            data["mode"]
        )
    )

    await query.answer()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())