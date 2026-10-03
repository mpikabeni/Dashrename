# Dash Renamer V2 — Local Bot API

This version uses ONE Render web service containing:
1. Telegram Local Bot API
2. Dash Renamer

This is intentional. Telegram's local Bot API returns local absolute file paths
in local mode. Keeping both processes in the same container means the bot can
access those paths directly.

## Target

- Videos: up to 2 GiB
- Documents/files: up to 2 GiB
- Video -> Document
- Video -> Video
- Video document -> Document
- Video document -> Video
- Photo -> stored as permanent thumbnail record
- Audio/voice/animation -> ignored
- No transcoding in this validation version

## Render

Use a paid web service with a persistent disk. A persistent disk is required
for large temporary files and the local Bot API working directory.

Environment variables:

BOT_TOKEN
TELEGRAM_API_ID
TELEGRAM_API_HASH

PORT is supplied by Render.

## Critical Telegram step

When moving a bot from the official Bot API to a local Bot API server, Telegram
requires logging the bot out from the official server first. Then the bot can
use the local server.

Do not paste your token, api_hash or api_id into chat.

## What is deliberately not marked complete yet

The code validates the 2 GiB local transfer path first. Real upload progress
(speed/ETA), cancel-during-transfer, and attaching the stored user thumbnail
to each outgoing upload are the next implementation layer. They must be added
without pretending a Telegram file_id is itself a reusable thumbnail upload.
