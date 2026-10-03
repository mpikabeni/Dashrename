# Dash Renamer — V1

This is a clean rebuild. It does not reuse the old Dash code.

## Current rules

- Video: accepted
- Document/file: accepted
- Photo: saved as the user's permanent thumbnail record
- Audio: ignored
- Voice: ignored
- Animation/GIF: ignored
- Maximum video/document size: 2 GiB
- Video can be sent as Video or Document
- A video document can be sent as Video or Document
- No transcoding in V1

## Important

A Telegram Local Bot API server is required for the 2 GiB target. The official Local Bot API server documents unlimited downloads and uploads up to 2000 MB in local mode.

The current V1 code focuses on the clean media/rename workflow first. Upload progress (speed/ETA), cancel during an active transfer, and permanent thumbnail attachment to every outgoing video/document are the next implementation step; they should not be faked as complete.

## Environment

BOT_TOKEN
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_LOCAL_API=true

For Render, RENDER_EXTERNAL_URL and PORT are provided by the platform.
