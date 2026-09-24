# services/thumbnails.py

import os
import shutil
import subprocess
from pathlib import Path


# ============================================================
# DASH VIDEO THUMBNAILS
# ============================================================


FFMPEG_BIN = os.getenv(
    "FFMPEG_BIN",
    "ffmpeg"
)


FFPROBE_BIN = os.getenv(
    "FFPROBE_BIN",
    "ffprobe"
)


THUMBNAIL_WIDTH = int(
    os.getenv(
        "THUMBNAIL_WIDTH",
        "640"
    )
)


THUMBNAIL_HEIGHT = int(
    os.getenv(
        "THUMBNAIL_HEIGHT",
        "360"
    )
)


THUMBNAIL_QUALITY = int(
    os.getenv(
        "THUMBNAIL_QUALITY",
        "4"
    )
)


def has_ffmpeg():
    return shutil.which(
        FFMPEG_BIN
    ) is not None


def has_ffprobe():
    return shutil.which(
        FFPROBE_BIN
    ) is not None


def get_video_duration(
    video_path: str
):

    if not has_ffprobe():
        return None

    try:

        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            return None

        value = result.stdout.strip()

        if not value:
            return None

        return float(value)

    except (
        subprocess.SubprocessError,
        ValueError,
        OSError
    ):
        return None


def create_video_thumbnail(
    video_path: str,
    output_dir: str,
    filename: str = "thumbnail.jpg",
    timestamp: float | None = None,
):

    if not os.path.exists(
        video_path
    ):
        raise FileNotFoundError(
            f"Vidéo introuvable : {video_path}"
        )

    if not has_ffmpeg():
        raise RuntimeError(
            "FFmpeg n'est pas installé."
        )

    output_directory = Path(
        output_dir
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    output_path = (
        output_directory
        / filename
    )

    duration = get_video_duration(
        video_path
    )

    if timestamp is None:

        if duration:

            # Prendre une image vers 20 %
            # de la vidéo.
            timestamp = max(
                0.5,
                duration * 0.20
            )

            # Éviter de dépasser la vidéo.
            timestamp = min(
                timestamp,
                max(
                    0.5,
                    duration - 0.5
                )
            )

        else:
            timestamp = 1.0

    command = [
        FFMPEG_BIN,
        "-y",
        "-ss",
        str(timestamp),
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        (
            f"scale={THUMBNAIL_WIDTH}:"
            f"{THUMBNAIL_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={THUMBNAIL_WIDTH}:"
            f"{THUMBNAIL_HEIGHT}:"
            "(ow-iw)/2:(oh-ih)/2"
        ),
        "-q:v",
        str(THUMBNAIL_QUALITY),
        str(output_path),
    ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )

    except subprocess.TimeoutExpired:

        raise RuntimeError(
            "La création de la miniature "
            "a dépassé le temps maximum."
        )

    if result.returncode != 0:

        error = (
            result.stderr.strip()
            or "Erreur FFmpeg inconnue."
        )

        raise RuntimeError(
            f"Impossible de créer la miniature : "
            f"{error[-1000:]}"
        )

    if not output_path.exists():

        raise RuntimeError(
            "FFmpeg n'a pas généré la miniature."
        )

    return str(
        output_path
    )


def create_thumbnail_from_video(
    video_path: str,
    output_dir: str,
):

    return create_video_thumbnail(
        video_path=video_path,
        output_dir=output_dir,
        filename="thumbnail.jpg",
    )


def validate_thumbnail(
    thumbnail_path: str
):

    path = Path(
        thumbnail_path
    )

    if not path.exists():
        return False

    if not path.is_file():
        return False

    if path.stat().st_size <= 0:
        return False

    return True
