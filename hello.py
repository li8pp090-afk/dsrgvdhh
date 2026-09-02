import os
import re
import async_timeout
import asyncio
import difflib
import logging
from collections import deque, defaultdict
from typing import Dict, Any, Optional, Tuple, Set

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.enums import ChatType, ButtonStyle

import yt_dlp

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

MAX_CONCURRENT_DOWNLOADS = 3
MAX_QUEUE_SIZE = 3

OWNER_IDS: Set[int] = {123456789}

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
waiting_queue = deque()
active_tasks_count = 0

chat_settings: Dict[int, str] = defaultdict(lambda: "default")
user_reply_counters: Dict[int, int] = defaultdict(int)
default_success_counters: Dict[int, int] = defaultdict(int)

id_file_default: Dict[str, str] = {}
id_file_audio: Dict[str, str] = {}
id_file_yt: Dict[str, str] = {}

user_editing_state: Dict[int, str] = {}

UPPERCASE_TARGETS = set("ATFNMJULG")

DEFAULT_SUCCESS_MESSAGES = [
    "هلو باي\nاه",
    "باي هلو\nاه",
    "اه باي\nهلو",
    "اه هلو\nباي"
]

NON_LINK_RESPONSES = [
    "اهلين وسهلين\nاستاذ/ة",
    "وياك بوت ميديا دز رابط منشور\nالفيد وادزلكيا",
    "مو ناوي تستعملني مثل\nالبوتات ترى بس اضوج ينتفخ ديسي",
    "راح انزع وتنيكني بدال هذا\nالنيج شو داضوج"
]

URL_REGEX = re.compile(r'https?://[^\s]+')
TELEGRAM_URL_REGEX = re.compile(r'https?://(t\.me|telegram\.me|telegram\.org)/[^\s]+')

logging.basicConfig(level=logging.INFO)

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

def sanitize_and_format_title(channel_name: Optional[str], title: Optional[str]) -> str:
    def format_part(text: str) -> str:
        text = re.sub(r'[^\w\s]', '', text)
        result = []
        for char in text:
            if char.isalpha() and char.isascii():
                c_upper = char.upper()
                if c_upper in UPPERCASE_TARGETS:
                    result.append(c_upper)
                else:
                    result.append(char.lower())
            else:
                result.append(char)
        return "".join(result)

    clean_channel = format_part(channel_name) if channel_name else ""
    clean_title = format_part(title) if title else ""

    if clean_channel and clean_title:
        return f"{clean_channel} - {clean_title}"
    return clean_channel or clean_title or "file"

def parse_time_to_seconds(time_str: str) -> float:
    time_str = time_str.strip()
    if '.' in time_str:
        parts = time_str.split('.')
        hours = float(parts[0])
        time_part = parts[1]
    else:
        hours = 0.0
        time_part = time_str

    time_subparts = time_part.split(':')
    if len(time_subparts) == 2:
        minutes = float(time_subparts[0])
        seconds = float(time_subparts[1])
    elif len(time_subparts) == 1:
        minutes = 0.0
        seconds = float(time_subparts[0])
    else:
        minutes = float(time_subparts[0])
        seconds = float(time_subparts[1])

    return hours * 3600 + minutes * 60 + seconds

def format_seconds_to_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}.{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def calculate_similarity(query: str, title: str) -> float:
    return difflib.SequenceMatcher(None, query.lower(), title.lower()).ratio()

def get_edit_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    is_audio = (current_mode == "audio")
    is_default = (current_mode == "default")

    btn_audio = InlineKeyboardButton(
        text="زر صوت",
        callback_data="set_mode_audio",
        style=ButtonStyle.PRIMARY if is_audio else ButtonStyle.DANGER
    )
    btn_default = InlineKeyboardButton(
        text="زر افتراضي",
        callback_data="set_mode_default",
        style=ButtonStyle.PRIMARY if is_default else ButtonStyle.DANGER
    )

    return InlineKeyboardMarkup(inline_keyboard=[[btn_audio, btn_default]])

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def process_media_task(chat_id: int, user_id: int, task_type: str, payload: str, reply_to_message_id: int):
    global active_tasks_count

    if active_tasks_count >= MAX_CONCURRENT_DOWNLOADS:
        if len(waiting_queue) >= MAX_QUEUE_SIZE:
            return
        else:
            waiting_queue.append((chat_id, user_id, task_type, payload, reply_to_message_id))
            return

    active_tasks_count += 1
    asyncio.create_task(run_download_workflow(chat_id, user_id, task_type, payload, reply_to_message_id))

async def run_download_workflow(chat_id: int, user_id: int, task_type: str, payload: str, reply_to_message_id: int):
    global active_tasks_count

    async with download_semaphore:
        start_msg = None
        try:
            if task_type == "yt":
                start_msg = await bot.send_message(
                    chat_id,
                    f"ها تريد {payload}\nتمام عبي",
                    reply_to_message_id=reply_to_message_id
                )
            else:
                start_msg = await bot.send_message(
                    chat_id,
                    "ههع شم كسي\nيلا",
                    reply_to_message_id=reply_to_message_id
                )

            loop = asyncio.get_event_loop()

            if task_type == "yt":
                if payload in id_file_yt:
                    await bot.send_voice(
                        chat_id,
                        voice=id_file_yt[payload],
                        reply_to_message_id=reply_to_message_id
                    )
                    if start_msg:
                        await start_msg.delete()
                    return

                ydl_opts_search = {
                    'extract_flat': True,
                    'skip_download': True,
                    'quiet': True
                }

                def search_yt():
                    with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
                        return ydl.extract_info(f"ytsearch6:{payload}", download=False)

                info = await loop.run_in_executor(None, search_yt)
                entries = info.get('entries', [])

                if not entries:
                    raise Exception("No search results found")

                best_entry = max(entries, key=lambda x: calculate_similarity(payload, x.get('title', '')))
                video_url = best_entry.get('url') or f"https://www.youtube.com/watch?v={best_entry.get('id')}"

                out_tmpl = f"temp_yt_{user_id}_{asyncio.get_event_loop().time()}.%(ext)s"
                ydl_opts_dl = {
                    'format': 'bestaudio/best',
                    'outtmpl': out_tmpl,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'opus',
                    }],
                    'postprocessor_args': ['-f', 'ogg'],
                    'quiet': True
                }

                def download_audio():
                    with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
                        meta = ydl.extract_info(video_url, download=True)
                        filename = ydl.prepare_filename(meta)
                        base = os.path.splitext(filename)[0]
                        return base + ".opus"

                downloaded_file = await loop.run_in_executor(None, download_audio)

                sent_msg = await bot.send_voice(
                    chat_id,
                    voice=types.FSInputFile(downloaded_file),
                    reply_to_message_id=reply_to_message_id
                )
                if sent_msg.voice:
                    id_file_yt[payload] = sent_msg.voice.file_id

                if os.path.exists(downloaded_file):
                    os.remove(downloaded_file)

            elif task_type == "audio":
                if payload in id_file_audio:
                    await bot.send_voice(
                        chat_id,
                        voice=id_file_audio[payload],
                        reply_to_message_id=reply_to_message_id
                    )
                    if start_msg:
                        await start_msg.delete()
                    return

                out_tmpl = f"temp_audio_{user_id}_{asyncio.get_event_loop().time()}.%(ext)s"
                ydl_opts_dl = {
                    'format': 'bestaudio/best',
                    'outtmpl': out_tmpl,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'opus',
                    }],
                    'postprocessor_args': ['-f', 'ogg'],
                    'quiet': True
                }

                def download_direct_audio():
                    with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
                        meta = ydl.extract_info(payload, download=True)
                        filename = ydl.prepare_filename(meta)
                        base = os.path.splitext(filename)[0]
                        return base + ".opus"

                downloaded_file = await loop.run_in_executor(None, download_direct_audio)

                sent_msg = await bot.send_voice(
                    chat_id,
                    voice=types.FSInputFile(downloaded_file),
                    reply_to_message_id=reply_to_message_id
                )
                if sent_msg.voice:
                    id_file_audio[payload] = sent_msg.voice.file_id

                if os.path.exists(downloaded_file):
                    os.remove(downloaded_file)

            elif task_type == "default":
                if payload in id_file_default:
                    await bot.send_document(
                        chat_id,
                        document=id_file_default[payload],
                        reply_to_message_id=reply_to_message_id
                    )
                    idx = default_success_counters[chat_id] % 4
                    default_success_counters[chat_id] += 1
                    await bot.send_message(
                        chat_id,
                        DEFAULT_SUCCESS_MESSAGES[idx],
                        reply_to_message_id=reply_to_message_id
                    )
                    if start_msg:
                        await start_msg.delete()
                    return

                out_tmpl = f"temp_def_{user_id}_{asyncio.get_event_loop().time()}.%(ext)s"
                ydl_opts_dl = {
                    'format': 'bestvideo+bestaudio/best',
                    'outtmpl': out_tmpl,
                    'quiet': True
                }

                def download_default_video():
                    with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
                        meta = ydl.extract_info(payload, download=True)
                        file_path = ydl.prepare_filename(meta)
                        ext = os.path.splitext(file_path)[1]
                        uploader = meta.get('uploader') or meta.get('channel')
                        title = meta.get('title')
                        formatted_name = sanitize_and_format_title(uploader, title) + ext
                        return file_path, formatted_name

                file_path, formatted_filename = await loop.run_in_executor(None, download_default_video)

                input_file = types.FSInputFile(file_path, filename=formatted_filename)
                sent_msg = await bot.send_document(
                    chat_id,
                    document=input_file,
                    reply_to_message_id=reply_to_message_id
                )
                if sent_msg.document:
                    id_file_default[payload] = sent_msg.document.file_id

                idx = default_success_counters[chat_id] % 4
                default_success_counters[chat_id] += 1
                await bot.send_message(
                    chat_id,
                    DEFAULT_SUCCESS_MESSAGES[idx],
                    reply_to_message_id=reply_to_message_id
                )

                if os.path.exists(file_path):
                    os.remove(file_path)

            if start_msg:
                await start_msg.delete()

        except Exception as e:
            logging.error(f"Execution error: {e}")
            if start_msg:
                await start_msg.delete()

            if task_type == "yt":
                await bot.send_message(
                    chat_id,
                    "الرابط غير مدعوم او اليوتيوب مو راضي يتعاون\nشم طيزي يلا",
                    reply_to_message_id=reply_to_message_id
                )
            else:
                await bot.send_message(
                    chat_id,
                    "الرابط غير مدعوم او الموقع مو راضي يتعاون\nشم طيزي يلا",
                    reply_to_message_id=reply_to_message_id
                )

    active_tasks_count -= 1
    if waiting_queue:
        next_task = waiting_queue.popleft()
        await process_media_task(*next_task)

@dp.message(F.text == "ادت")
async def cmd_edit_panel(message: Message):
    if not is_owner(message.from_user.id):
        return

    current_mode = chat_settings[message.chat.id]
    kb = get_edit_keyboard(current_mode)
    await message.reply(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("set_mode_"))
async def handle_mode_change(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("عزيزي\nليس مصرح لك بذلك", show_alert=True)
        return

    chat_id = callback.message.chat.id
    target_mode = callback.data.split("_")[-1]
    current_mode = chat_settings[chat_id]

    if target_mode == "default":
        if current_mode == "default":
            await callback.answer("زر افتراضي مُفعل\nبالفعل", show_alert=True)
            return
        else:
            chat_settings[chat_id] = "default"
    elif target_mode == "audio":
        if current_mode == "audio":
            chat_settings[chat_id] = "default"
        else:
            chat_settings[chat_id] = "audio"

    new_mode = chat_settings[chat_id]
    kb = get_edit_keyboard(new_mode)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.message(F.text.startswith("تعديل") & F.reply_to_message)
async def start_voice_trim_session(message: Message):
    reply_msg = message.reply_to_message
    if not reply_msg.voice:
        return

    file_id = reply_msg.voice.file_id
    if file_id not in id_file_yt.values() and file_id not in id_file_audio.values():
        return

    user_editing_state[message.from_user.id] = file_id
    await message.reply("تستطيع تعديل مدة الصوتيات هكذا\n\n12:45 / 18:36 وللساعات 12.30:48")

@dp.message(F.from_user.id.in_(user_editing_state))
async def process_voice_trim_execution(message: Message):
    file_id = user_editing_state.pop(message.from_user.id)
    text = message.text.strip()

    if "/" not in text:
        return

    try:
        start_str, end_str = text.split("/")
        start_sec = parse_time_to_seconds(start_str)
        end_sec = parse_time_to_seconds(end_str)

        file_info = await bot.get_file(file_id)
        input_path = f"input_{file_id}.ogg"
        output_path = f"output_{file_id}.ogg"

        await bot.download_file(file_info.file_path, input_path)

        def get_duration():
            import subprocess, json
            cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', input_path]
            res = subprocess.run(cmd, capture_output=True, text=True)
            data = json.loads(res.stdout)
            return float(data['format']['duration'])

        loop = asyncio.get_event_loop()
        duration = await loop.run_in_executor(None, get_duration)

        if start_sec >= duration or end_sec > duration or start_sec >= end_sec:
            await message.reply("مدة هذه الصوتيه اصغر من المدة اللتي\n\nارسلتها")
            if os.path.exists(input_path):
                os.remove(input_path)
            return

        trim_duration = end_sec - start_sec

        def trim_audio():
            import subprocess
            cmd = [
                'ffmpeg', '-y', '-ss', str(start_sec), '-t', str(trim_duration),
                '-i', input_path, '-c:a', 'libopus', '-f', 'ogg', output_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        await loop.run_in_executor(None, trim_audio)

        await message.reply_voice(voice=types.FSInputFile(output_path))

        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)

    except Exception as e:
        logging.error(f"Error trimming voice: {e}")
        if os.path.exists(f"input_{file_id}.ogg"):
            os.remove(f"input_{file_id}.ogg")
        if os.path.exists(f"output_{file_id}.ogg"):
            os.remove(f"output_{file_id}.ogg")

@dp.message()
async def main_router(message: Message):
    text = message.text or ""
    chat_type = message.chat.type

    if text.startswith("يوت"):
        query = text[3:].strip()
        if query:
            await process_media_task(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                task_type="yt",
                payload=query,
                reply_to_message_id=message.message_id
            )
        return

    links = URL_REGEX.findall(text)
    if links:
        valid_links = [link for link in links if not TELEGRAM_URL_REGEX.search(link)]
        if not valid_links:
            return

        target_link = valid_links[0]
        mode = chat_settings[message.chat.id]

        await process_media_task(
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            task_type="audio" if mode == "audio" else "default",
            payload=target_link,
            reply_to_message_id=message.message_id
        )
        return

    should_respond = False
    if chat_type == ChatType.PRIVATE:
        should_respond = True
    elif text.strip() == "بوت":
        should_respond = True

    if should_respond:
        user_id = message.from_user.id
        idx = user_reply_counters[user_id] % 4
        user_reply_counters[user_id] += 1
        await message.reply(NON_LINK_RESPONSES[idx])

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
