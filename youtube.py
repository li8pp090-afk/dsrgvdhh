import asyncio
from difflib import SequenceMatcher

import yt_dlp


def youtube_search(query):
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
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
            (item.get("title") or "").casefold().strip(),
        ).ratio(),
    )


def get_youtube_url(result):
    webpage_url = result.get("webpage_url")

    if webpage_url and webpage_url.startswith(
        "http"
    ):
        return webpage_url

    video_id = result.get("id")

    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"

    url = result.get("url")

    if url and url.startswith("http"):
        return url

    return None


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

    url = get_youtube_url(
        selected
    )

    if not url:
        raise RuntimeError(
            "youtube result has no url"
        )

    return url