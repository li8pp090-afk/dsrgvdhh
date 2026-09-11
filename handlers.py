import asyncio
import tempfile

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from config import (
    DOWNLOAD_LIMIT,
    WAITING_LIMIT,
)
from database import get_mode
from files import process_default
from settings import router as settings_router
from utils import (
    chat_key,
    is_group_like,
    is_telegram_url,
    is_url,
    is_youtube_command,
    youtube_query,
)
from voice import process_voice
from youtube import resolve_youtube_query


router = Router()

router.include_router(
    settings_router
)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(
    DOWNLOAD_LIMIT
)

waiting_downloads = 0
waiting_lock = asyncio.Lock()

reply_rotation = {}
reply_rotation_lock = asyncio.Lock()

ROTATION_MESSAGES = [
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
    "راح انزع وتنيكني بدال هذا\nالنيج شو داضوج",
]


async def get_next_rotation(
    user_id,
):
    async with reply_rotation_lock:
        index = reply_rotation.get(
            user_id,
            0,
        )

        reply_rotation[user_id] = (
            index + 1
        ) % len(ROTATION_MESSAGES)

        return ROTATION_MESSAGES[index]


async def acquire_download_slot():
    global waiting_downloads

    async with waiting_lock:
        if DOWNLOAD_SEMAPHORE._value > 0:
            await DOWNLOAD_SEMAPHORE.acquire()
            return True

        if waiting_downloads >= WAITING_LIMIT:
            return False

        waiting_downloads += 1

    await DOWNLOAD_SEMAPHORE.acquire()

    async with waiting_lock:
        waiting_downloads -= 1

    return True


def release_download_slot():
    DOWNLOAD_SEMAPHORE.release()


@router.message()
async def main_handler(
    message: Message,
):
    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    if not text:
        return

    if is_youtube_command(text):
        query = youtube_query(text)

        if not query:
            await message.answer(
                "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا",
                reply_parameters=message.as_reply_parameters(),
            )
            return

        acquired = (
            await acquire_download_slot()
        )

        if not acquired:
            return

        start_message = None

        try:
            start_message = await message.answer(
                f"ها تريد {query}\nتمام عبي",
                reply_parameters=message.as_reply_parameters(),
            )

            youtube_url = (
                await resolve_youtube_query(
                    query
                )
            )

            mode = get_mode(
                chat_key(message)
            )

            if mode == "voice":
                await process_voice(
                    message,
                    youtube_url,
                    "youtube",
                )
            else:
                await process_default(
                    message,
                    youtube_url,
                    "youtube",
                )

            try:
                await start_message.delete()
            except Exception:
                pass

        except Exception:
            if start_message:
                try:
                    await start_message.delete()
                except Exception:
                    pass

            await message.answer(
                "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا",
                reply_parameters=message.as_reply_parameters(),
            )

        finally:
            release_download_slot()

        return

    if (
        is_url(text)
        and not is_telegram_url(text)
    ):
        acquired = (
            await acquire_download_slot()
        )

        if not acquired:
            return

        start_message = None

        try:
            start_message = await message.answer(
                "ههع شم كسي\nيلا",
                reply_parameters=message.as_reply_parameters(),
            )

            mode = get_mode(
                chat_key(message)
            )

            if mode == "voice":
                await process_voice(
                    message,
                    text,
                    "url",
                )
            else:
                await process_default(
                    message,
                    text,
                    "url",
                )

            try:
                await start_message.delete()
            except Exception:
                pass

        except Exception:
            if start_message:
                try:
                    await start_message.delete()
                except Exception:
                    pass

            await message.answer(
                "الرابط غير مدعوم او الموقع مو راضي يتعاون\nشم طيزي يلا",
                reply_parameters=message.as_reply_parameters(),
            )

        finally:
            release_download_slot()

        return

    if is_group_like(message):
        if text != "بوت":
            return

        if not message.from_user:
            return

        response = await get_next_rotation(
            message.from_user.id
        )

        await message.answer(
            response,
            reply_parameters=message.as_reply_parameters(),
        )

        return

    if message.chat.type == ChatType.PRIVATE:
        if not message.from_user:
            return

        response = await get_next_rotation(
            message.from_user.id
        )

        await message.answer(
            response,
            reply_parameters=message.as_reply_parameters(),
        )