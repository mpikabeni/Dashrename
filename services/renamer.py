# services/renamer.py

import re
from pathlib import Path


# ============================================================
# DASH RENAMER
# ============================================================


def safe_filename(
    name: str,
    fallback: str = "file",
    max_length: int = 180
) -> str:

    if not name:
        name = fallback

    name = Path(name).name

    # Supprimer les caractères de contrôle
    name = re.sub(
        r"[\x00-\x1f\x7f]",
        "",
        name
    )

    # Remplacer les caractères dangereux
    name = name.replace(
        "/",
        "_"
    )

    name = name.replace(
        "\\",
        "_"
    )

    # Éviter les noms problématiques
    name = name.strip(
        " ."
    )

    if not name:
        name = fallback

    return name[:max_length]


def preserve_extension(
    original_name: str,
    new_name: str
) -> str:

    original = Path(
        original_name
    )

    new = Path(
        safe_filename(new_name)
    )

    # Si l'utilisateur a déjà indiqué
    # une extension, on la conserve.
    if new.suffix:
        return new.name

    # Sinon on reprend celle du fichier original.
    if original.suffix:
        return (
            new.name
            + original.suffix
        )

    return new.name


def rename_file(
    source_path: str,
    new_name: str,
    output_directory: str | None = None
) -> str:

    source = Path(
        source_path
    )

    if not source.exists():
        raise FileNotFoundError(
            f"Fichier introuvable : {source}"
        )

    if output_directory:
        destination_dir = Path(
            output_directory
        )
    else:
        destination_dir = source.parent

    destination_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    final_name = preserve_extension(
        source.name,
        new_name
    )

    final_name = safe_filename(
        final_name
    )

    destination = (
        destination_dir
        / final_name
    )

    # Éviter d'écraser accidentellement
    # un fichier existant.
    if destination.exists() and destination != source:

        stem = destination.stem
        suffix = destination.suffix

        counter = 1

        while destination.exists():

            destination = (
                destination_dir
                / f"{stem}_{counter}{suffix}"
            )

            counter += 1

    source.rename(
        destination
    )

    return str(
        destination
    )


def get_extension(
    filename: str
) -> str:

    return Path(
        filename
    ).suffix.lower()


def get_filename_without_extension(
    filename: str
) -> str:

    return Path(
        filename
    ).stem


def change_extension(
    filename: str,
    new_extension: str
) -> str:

    path = Path(
        filename
    )

    new_extension = (
        new_extension
        .strip()
        .lstrip(".")
        .lower()
    )

    if not new_extension:
        return path.name

    return (
        path.stem
        + "."
        + new_extension
    )
