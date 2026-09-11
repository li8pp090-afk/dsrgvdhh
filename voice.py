import asyncio
import shutil
import subprocess
from pathlib import Path
import tempfile

from aiogram.types import FSInputFile

from cache import get_cache, make_cache_key, set_cache
from downloader import (
    download_audio_with_ytdlp,
    find_media_files,
)
from utils import (
    clean_filename,
    get_entries,
    normalize_latin_case,
    publisher_name,
)


def convert_to_voice(
    source,
    target,
):
    process = subprocess.run(
        [
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
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if process.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed"
        )


async def send_cached_voices(
    message,
    file_ids,
):
    for file_id in file_ids:
        await message.answer_voice(
            file_id,
            reply_parameters=message.as_reply_parameters(),
        )


async def send_voice_files(
    message,
    directory,
    info,
):
    files = find_media_files(directory)

    if not files:
        raise RuntimeError(
            "no downloaded audio files"
        )

    entries = get_entries(info)

    file_ids = []

    for index, source in enumerate(files):
        if index < len(entries):
            publisher = normalize_latin_case(
                clean_filename(
                    publisher_name(
                        entries[index]
                    )
                )
            )
        else:
            publisher = normalize_latin_case(
                clean_filename(
                    source.stem
                )
            )

        target = (
            Path(directory)
            / f"{publisher}.ogg"
        )

        if target.exists():
            target.unlink()

        convert_to_voice(
            source,
            target,
        )

        sent = await message.answer_voice(
            FSInputFile(target),
            reply_parameters=message.as_reply_parameters(),
        )

        if not sent.voice:
            raise RuntimeError(
                "telegram did not return voice id"
            )

        file_ids.append(
            sent.voice.file_id
        )

    return file_ids


async def process_voice(
    message,
    source,
    route,
):
    cache_key = make_cache_key(
        source,
        "voice",
        route,
    )

    cached = get_cache(cache_key)

    if cached:
        try:
            await send_cached_voices(
                message,
                cached,
            )
            return
        except Exception:
            pass

    directory = tempfile.mkdtemp(
        prefix="voice_"
    )

    try:
        info = await asyncio.to_thread(
            download_audio_with_ytdlp,
            source,
            directory,
        )

        file_ids = await send_voice_files(
            message,
            directory,
            info,
        )

        set_cache(
            cache_key,
            file_ids,
        )

    finally:
        shutil.rmtree(
            directory,
            ignore_errors=True,
        )