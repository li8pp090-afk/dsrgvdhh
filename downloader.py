from pathlib import Path

import yt_dlp


def download_with_ytdlp(url, directory):
    output_template = str(
        Path(directory)
        / "%(uploader)s - %(title)s.%(ext)s"
    )

    options = {
        "outtmpl": output_template,
        "format": "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

    return info


def download_audio_with_ytdlp(url, directory):
    output_template = str(
        Path(directory)
        / "%(uploader)s - %(title)s.%(ext)s"
    )

    options = {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

    return info


def find_media_files(directory):
    files = []

    for path in Path(directory).iterdir():
        if (
            path.is_file()
            and not path.name.endswith(".part")
        ):
            files.append(path)

    return sorted(files)