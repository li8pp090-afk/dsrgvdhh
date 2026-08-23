import asyncio
import os
import re
import unicodedata
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

TOKEN = os.environ["BOT_TOKEN"]

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
    "مو ناوي تستعملني مثل البوتات ترى اذا اضوج\n"
    "اصيح المولاي يغصص بلاعيمك",

    "اهو فطستني بسوالفك هاي ديلا دز رابط اريد\n"
    "انفذلك طلباتك علمود انام",

    "ترى يمكن انطيك بلوك واعوفك ملبوس\n"
    "ها شتكول بيبي",
]

BUTTON_STYLES = [
    "primary",
    "success",
    "danger",
]

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

MAX_ACTIVE = 3
MAX_QUEUE = 3

users = {}
users_lock = asyncio.Lock()

EN_UPPER = set("ATGUFNJML")
RU_UPPER = set("АИБ")


def is_telegram_url(url):
    match = re.match(
        r"^https?://([^/]+)",
        url.lower(),
    )

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


def clean_publisher(name):
    name = unicodedata.normalize(
        "NFC",
        name or "",
    )

    result = []

    for char in name:
        if char.isascii() and char.isalpha():
            upper = char.upper()

            result.append(
                upper
                if upper in EN_UPPER
                else char.lower()
            )

        elif "\u0400" <= char <= "\u04FF":
            upper = char.upper()

            result.append(
                upper
                if upper in RU_UPPER
                else char.lower()
            )

        elif char.isdigit() or char in "_ ":
            result.append(char)

    return "".join(result).strip()


def clean_title(name):
    return re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "",
        name or "",
    ).strip()


def make_filename(
    publisher,
    title,
    extension,
):
    publisher = clean_publisher(publisher)

    if title:
        return (
            f"{publisher} - "
            f"{clean_title(title)}."
            f"{extension}"
        )

    return f"{publisher}.{extension}"


def get_user_state(user_id):
    if user_id not in users:
        reply_ids = ADMIN_IDS.copy()
        styles = BUTTON_STYLES.copy()

        users[user_id] = {
            "mode": "normal",
            "queue": asyncio.Queue(
                maxsize=MAX_QUEUE
            ),
            "running": 0,
            "tasks": set(),
            "reply_index": 0,
            "reply_ids": reply_ids,
            "reply_styles": styles,
        }

    return users[user_id]


def get_rotating_reply(user_id):
    state = get_user_state(user_id)

    reply = ROTATING_REPLIES[
        state["reply_index"]
    ]

    state["reply_index"] = (
        state["reply_index"] + 1
    ) % len(ROTATING_REPLIES)

    if not state["reply_ids"]:
        state["reply_ids"] = ADMIN_IDS.copy()

    reply_id = state["reply_ids"].pop(0)

    if not state["reply_styles"]:
        state["reply_styles"] = BUTTON_STYLES.copy()

    button_style = state[
        "reply_styles"
    ].pop(0)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="مولاي",
                    url=(
                        f"tg://user?id="
                        f"{reply_id}"
                    ),
                    style=button_style,
                )
            ]
        ]
    )

    return reply, keyboard


def mode_keyboard(user_id):
    mode = get_user_state(user_id)["mode"]

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
            ],
            [
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
        ],
    )


async def update_progress(
    status_message,
    percent,
    progress_state,
    loop,
):
    if not percent:
        return

    percent = min(
        100,
        max(0, int(percent)),
    )

    step = (percent // 5) * 5

    if step < 10:
        return

    if step > 100:
        step = 100

    if step == progress_state["last"]:
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

    asyncio.run_coroutine_threadsafe(
        edit(),
        loop,
    )


def download(
    url,
    folder,
    mode,
    status_message,
    loop,
):
    progress_state = {
        "last": 0,
    }

    def progress_hook(data):
        if data.get("status") != "downloading":
            return

        total = data.get("total_bytes")

        if not total:
            total = data.get(
                "total_bytes_estimate"
            )

        downloaded = data.get(
            "downloaded_bytes",
            0,
        )

        if not total:
            return

        percent = (
            downloaded / total
        ) * 100

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

    options = {
        "format": fmt,
        "outtmpl": str(
            folder / "%(title)s.%(ext)s"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "progress_hooks": [
            progress_hook
        ],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

        path = Path(
            ydl.prepare_filename(info)
        )

        if not path.exists():
            files = list(
                folder.glob("*")
            )

            if not files:
                raise FileNotFoundError(
                    "Download failed"
                )

            path = max(
                files,
                key=lambda file:
                file.stat().st_mtime,
            )

        return path, info


async def convert_to_voice(
    input_path,
    output_path,
):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-c:a",
        "libopus",
        str(output_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    return_code = await process.wait()

    if return_code != 0:
        raise RuntimeError(
            "FFmpeg voice conversion failed"
        )


async def send_voice_file(
    bot,
    user_id,
    path,
    reply_to,
):
    await bot.send_voice(
        chat_id=user_id,
        voice=FSInputFile(path),
        reply_to_message_id=reply_to,
    )


async def send_animation_file(
    bot,
    user_id,
    path,
    reply_to,
):
    await bot.send_animation(
        chat_id=user_id,
        animation=FSInputFile(
            path,
            filename=path.name,
        ),
        reply_to_message_id=reply_to,
    )


async def process(
    user_id,
    url,
    bot,
    mode,
    original_message,
    status_message,
):
    folder = DOWNLOAD_DIR / str(user_id)

    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = None
    final_path = None
    voice_path = None

    loop = asyncio.get_running_loop()

    try:
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

        title = (
            info.get("title")
            or path.stem
        )

        if mode == "audio":
            voice_path = folder / (
                f"{clean_publisher(publisher)}.ogg"
            )

            await convert_to_voice(
                path,
                voice_path,
            )

            await send_voice_file(
                bot,
                user_id,
                voice_path,
                original_message.message_id,
            )

        elif mode == "sticker":
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

            final_path = path.with_name(
                filename
            )

            if path != final_path:
                if final_path.exists():
                    final_path.unlink()

                path.rename(final_path)

            await send_animation_file(
                bot,
                user_id,
                final_path,
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

            final_path = path.with_name(
                filename
            )

            if path != final_path:
                if final_path.exists():
                    final_path.unlink()

                path.rename(final_path)

            await bot.send_document(
                chat_id=user_id,
                document=FSInputFile(
                    final_path,
                    filename=filename,
                ),
                reply_to_message_id=(
                    original_message.message_id
                ),
            )

        await status_message.edit_text(
            "طلبك نُفذ بدون أدنى مشكلة كل ماعليك\n"
            "هو إرسال رابط المنشور"
        )

    except Exception:
        try:
            await status_message.edit_text(
                "الرابط غير مدعوم او الموقع مو مدعوم\n"
                "شم كسي يلا"
            )
        except Exception:
            pass

    finally:
        for file in (
            voice_path,
            final_path,
            path,
        ):
            if file and file.exists():
                try:
                    file.unlink()
                except Exception:
                    pass


async def cleanup_task(
    user_id,
    task,
):
    async with users_lock:
        state = users.get(user_id)

        if state:
            state["tasks"].discard(task)


async def run_download(
    user_id,
    url,
    bot,
    mode,
    original_message,
    status_message,
):
    try:
        await process(
            user_id,
            url,
            bot,
            mode,
            original_message,
            status_message,
        )

    finally:
        async with users_lock:
            state = users.get(user_id)

            if not state:
                return

            state["running"] -= 1

            if not state["queue"].empty():
                (
                    next_url,
                    next_mode,
                    next_message,
                    next_status,
                ) = await state["queue"].get()

                state["running"] += 1

                task = asyncio.create_task(
                    run_download(
                        user_id,
                        next_url,
                        bot,
                        next_mode,
                        next_message,
                        next_status,
                    )
                )

                state["tasks"].add(task)

                task.add_done_callback(
                    lambda task,
                    uid=user_id:
                    asyncio.create_task(
                        cleanup_task(
                            uid,
                            task,
                        )
                    )
                )


async def add_download(
    user_id,
    url,
    bot,
    original_message,
):
    async with users_lock:
        state = get_user_state(user_id)

        total = (
            state["running"]
            + state["queue"].qsize()
        )

        if total >= (
            MAX_ACTIVE + MAX_QUEUE
        ):
            return

        status_message = await (
            original_message.reply(
                "يتم العثور على طلبك دادور انتظر\n"
                "اي هذا 0%"
            )
        )

        mode = state["mode"]

        if state["running"] < MAX_ACTIVE:
            state["running"] += 1

            task = asyncio.create_task(
                run_download(
                    user_id,
                    url,
                    bot,
                    mode,
                    original_message,
                    status_message,
                )
            )

            state["tasks"].add(task)

            task.add_done_callback(
                lambda task,
                uid=user_id:
                asyncio.create_task(
                    cleanup_task(
                        uid,
                        task,
                    )
                )
            )

        else:
            await state["queue"].put(
                (
                    url,
                    mode,
                    original_message,
                    status_message,
                )
            )


bot = Bot(TOKEN)
dp = Dispatcher()


async def notify_startup():
    for user_id in ADMIN_IDS:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="رب العالمين",
                            url=(
                                f"tg://user?id="
                                f"{user_id}"
                            ),
                            style="primary",
                        )
                    ]
                ]
            )

            await bot.send_message(
                user_id,
                "اشتغل البوت مرتلخ\n"
                "استعملني ؟!",
                reply_markup=keyboard,
            )

        except Exception:
            pass


@dp.message(CommandStart())
async def start(message):
    reply, keyboard = get_rotating_reply(
        message.from_user.id
    )

    await message.reply(
        reply,
        reply_markup=keyboard,
    )


@dp.message(F.text == "ادت")
async def download_settings(message):
    user_id = message.from_user.id

    async with users_lock:
        get_user_state(user_id)

    await message.reply(
        "تستطيع تغيير وضع عمل البوت من صوت الى\n"
        "ستيكر الى الوضع الافتراضي من هذه الأزرار",
        reply_markup=mode_keyboard(
            user_id
        ),
    )


@dp.callback_query(
    F.data == "mode_audio"
)
async def select_audio(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    async with users_lock:
        state = get_user_state(user_id)
        state["mode"] = "audio"

        keyboard = mode_keyboard(
            user_id
        )

    await callback.message.edit_reply_markup(
        reply_markup=keyboard
    )

    await callback.answer()


@dp.callback_query(
    F.data == "mode_sticker"
)
async def select_sticker(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    async with users_lock:
        state = get_user_state(user_id)
        state["mode"] = "sticker"

        keyboard = mode_keyboard(
            user_id
        )

    await callback.message.edit_reply_markup(
        reply_markup=keyboard
    )

    await callback.answer()


@dp.callback_query(
    F.data == "mode_normal"
)
async def select_normal(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    async with users_lock:
        state = get_user_state(user_id)

        if state["mode"] == "normal":
            await callback.answer(
                "لا يمكنك تعطيل الوضع الافتراضي\n"
                "تستطيع تبديل الوضع وليس تعطيل كل الاوضاع",
                show_alert=True,
            )
            return

        state["mode"] = "normal"

        keyboard = mode_keyboard(
            user_id
        )

    await callback.message.edit_reply_markup(
        reply_markup=keyboard
    )

    await callback.answer()


@dp.message(F.text)
async def link_handler(message):
    url = message.text.strip()

    if message.text == "ادت":
        return

    if not re.match(
        r"^https?://",
        url,
    ):
        reply, keyboard = get_rotating_reply(
            message.from_user.id
        )

        await message.reply(
            reply,
            reply_markup=keyboard,
        )

        return

    if is_telegram_url(url):
        return

    await add_download(
        message.from_user.id,
        url,
        bot,
        message,
    )


@dp.message()
async def non_text_handler(message):
    reply, keyboard = get_rotating_reply(
        message.from_user.id
    )

    await message.reply(
        reply,
        reply_markup=keyboard,
    )


async def main():
    await notify_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())