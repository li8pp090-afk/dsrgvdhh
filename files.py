from pathlib import Path

from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
)

from cache import get_cache, make_cache_key, set_cache
from downloader import (
    download_with_ytdlp,
    find_media_files,
)
from utils import (
    clean_filename,
    get_entries,
    normalize_latin_case,
    publisher_name,
    title_name,
)

import asyncio
import shutil
import tempfile


async def send_cached_documents(message, file_ids):
    if len(file_ids) == 1:
        await message.answer_document(
            file_ids[0],
            reply_parameters=message.as_reply_parameters(),
        )
        return

    for start in range(
        0,
        len(file_ids),
        10,
    ):
        batch = file_ids[
            start:start + 10
        ]

        media = [
            InputMediaDocument(
                media=file_id
            )
            for file_id in batch
        ]

        await message.answer_media_group(
            media=media,
            reply_parameters=message.as_reply_parameters(),
        )


async def send_default_files(
    message,
    directory,
    info,
):
    files = find_media_files(directory)

    if not files:
        raise RuntimeError(
            "no downloaded files"
        )

    entries = get_entries(info)

    if len(entries) == len(files):
        names = []

        for entry in entries:
            publisher = normalize_latin_case(
                clean_filename(
                    publisher_name(entry)
                )
            )

            title = normalize_latin_case(
                clean_filename(
                    title_name(entry)
                )
            )

            names.append(
                f"{publisher} - {title}"
            )

        renamed = []

        for index, path in enumerate(files):
            extension = path.suffix

            if index < len(names):
                new_name = (
                    names[index]
                    + extension
                )
            else:
                new_name = (
                    normalize_latin_case(
                        clean_filename(
                            path.stem
                        )
                    )
                    + extension
                )

            target = path.with_name(
                new_name
            )

            if target != path:
                if target.exists():
                    if "." in new_name:
                        stem, suffix = (
                            new_name.rsplit(
                                ".",
                                1,
                            )
                        )

                        target = path.with_name(
                            f"{stem}_{index + 1}.{suffix}"
                        )
                    else:
                        target = path.with_name(
                            f"{new_name}_{index + 1}"
                        )

                path.rename(target)

            renamed.append(target)

        files = renamed

    file_ids = []

    if len(files) == 1:
        sent = await message.answer_document(
            FSInputFile(files[0]),
            reply_parameters=message.as_reply_parameters(),
        )

        if not sent.document:
            raise RuntimeError(
                "telegram did not return document"
            )

        file_ids.append(
            sent.document.file_id
        )

        return file_ids

    for start in range(
        0,
        len(files),
        10,
    ):
        batch = files[
            start:start + 10
        ]

        media = [
            InputMediaDocument(
                media=FSInputFile(path)
            )
            for path in batch
        ]

        sent_messages = (
            await message.answer_media_group(
                media=media,
                reply_parameters=message.as_reply_parameters(),
            )
        )

        for sent in sent_messages:
            if sent.document:
                file_ids.append(
                    sent.document.file_id
                )

    if not file_ids:
        raise RuntimeError(
            "telegram did not return document ids"
        )

    return file_ids


async def process_default(
    message,
    source,
    route,
):
    cache_key = make_cache_key(
        source,
        "default",
        route,
    )

    cached = get_cache(cache_key)

    if cached:
        try:
            await send_cached_documents(
                message,
                cached,
            )
            return
        except Exception:
            pass

    directory = tempfile.mkdtemp(
        prefix="media_"
    )

    try:
        info = await asyncio.to_thread(
            download_with_ytdlp,
            source,
            directory,
        )

        file_ids = await send_default_files(
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