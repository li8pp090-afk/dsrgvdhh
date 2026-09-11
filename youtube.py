import asyncio
from difflib import SequenceMatcher

import yt_dlp


def youtube_search(query):
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"ytsearch3:{query}",
            download=False,
        )

    return info.get("entries") or []


def select_youtube_result(query, results):
    normalized_query = query.casefold().strip()

    return max(
        results,
        key=lambda item: SequenceMatcher(
            None,
            normalized_query,
            (
                item.get("title") or ""
            ).casefold().strip(),
        ).ratio(),
    )


async def resolve_youtube_query(query):
    results = await asyncio.to_thread(
        youtube_search,
        query,
    )

    if not results:
        raise RuntimeError(
            "youtube search failed"
        )

    selected = select_youtube_result(
        query,
        results,
    )

    url = (
        selected.get("webpage_url")
        or selected.get("url")
    )

    if not url:
        raise RuntimeError(
            "youtube result has no url"
        )

    return url