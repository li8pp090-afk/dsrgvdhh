import re

from aiogram.enums import ChatType


def clean_filename(name):
    name = re.sub(
        r"[^\w\s.]",
        "",
        name,
        flags=re.UNICODE,
    )

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    name = name[:200]

    return name or "media"


def normalize_latin_case(name):
    chars = set(
        "ATFNMJULGatfnmjulg"
    )

    return "".join(
        char.upper() if char in chars else char
        for char in name
    )


def publisher_name(info):
    return (
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or info.get("artist")
        or "media"
    )


def title_name(info):
    return info.get("title") or "media"


def get_entries(info):
    entries = info.get("entries")

    if not entries:
        return [info]

    return [
        entry
        for entry in entries
        if entry
    ]


def is_telegram_url(text):
    lowered = text.lower()

    return (
        "t.me/" in lowered
        or "telegram.me/" in lowered
        or "telegram.dog/" in lowered
        or lowered.startswith("tg://")
    )


def is_url(text):
    return bool(
        re.match(
            r"^(https?://|www\.)",
            text.strip(),
            re.IGNORECASE,
        )
    )


def is_youtube_command(text):
    return text.casefold().startswith("يوت")


def youtube_query(text):
    return text[3:].strip()


def is_group_like(message):
    return message.chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
        ChatType.CHANNEL,
    }


def chat_key(message):
    if message.message_thread_id:
        return (
            f"{message.chat.id}:"
            f"{message.message_thread_id}"
        )

    return str(message.chat.id)