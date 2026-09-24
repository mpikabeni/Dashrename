# services/broadcast.py

import asyncio

try:
    from database import (
        get_users,
        save_broadcast,
    )
except ImportError:
    from ..database import (
        get_users,
        save_broadcast,
    )


# ============================================================
# DASH BROADCAST SERVICE
# CREATED BY NEXA
# ============================================================


BROADCAST_DELAY = 0.05


async def broadcast_message(
    bot,
    admin_id,
    source_message,
    delay=BROADCAST_DELAY,
):
    """
    Diffuse un message Telegram vers tous les utilisateurs
    enregistrés dans la base de données.

    Compatible avec :
    - texte
    - photo
    - vidéo
    - document
    - audio
    - animation/GIF
    - sticker
    - vocal
    """

    users = get_users()

    sent = 0
    failed = 0

    for user_id in users:

        try:

            await source_message.copy(
                chat_id=user_id
            )

            sent += 1

        except Exception as error:

            failed += 1

            print(
                "DASH BROADCAST ERROR:",
                user_id,
                repr(error)
            )

        if delay > 0:
            await asyncio.sleep(
                delay
            )

    try:

        save_broadcast(
            admin_id=admin_id,
            sent=sent,
            failed=failed,
        )

    except Exception as error:

        print(
            "BROADCAST DATABASE ERROR:",
            repr(error)
        )

    return {
        "total": len(users),
        "sent": sent,
        "failed": failed,
    }


async def send_broadcast_preview(
    bot,
    admin_id,
    source_message,
):
    """
    Envoie une copie du message à l'administrateur
    afin de permettre une prévisualisation avant diffusion.
    """

    try:

        message = await source_message.copy(
            chat_id=admin_id
        )

        return message

    except Exception as error:

        print(
            "BROADCAST PREVIEW ERROR:",
            repr(error)
        )

        return None


async def broadcast_to_users(
    bot,
    admin_id,
    source_message,
):
    """
    Alias pratique pour la fonction principale.
    """

    return await broadcast_message(
        bot=bot,
        admin_id=admin_id,
        source_message=source_message,
    )


def format_broadcast_result(
    result,
):

    if not result:
        return (
            "❌ <b>La diffusion a échoué.</b>"
        )

    total = result.get(
        "total",
        0
    )

    sent = result.get(
        "sent",
        0
    )

    failed = result.get(
        "failed",
        0
    )

    return (
        "📢 <b>DIFFUSION TERMINÉE</b>\n\n"
        f"👥 Destinataires : <b>{total}</b>\n"
        f"✅ Envoyés : <b>{sent}</b>\n"
        f"❌ Échecs : <b>{failed}</b>"
    )
