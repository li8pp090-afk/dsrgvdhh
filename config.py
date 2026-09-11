import os

TOKEN = os.getenv("BOT_TOKEN")
DATABASE = "settings.db"

DOWNLOAD_LIMIT = 3
WAITING_LIMIT = 3

BOOT_MESSAGE_IDS = [
    int(user_id.strip())
    for user_id in os.getenv(
        "BOOT_MESSAGE_IDS",
        "",
    ).split("/")
    if user_id.strip()
]