# Dash Renamer

Bot Telegram de NEXA dédié au renommage de fichiers.

## Fonctions

- Renommer
- Informations du fichier
- Historique
- Vérification du canal NEXA
- Administration
- Diffusion
- Stockage temporaire Backblaze B2
- `dash.png` affiché au `/start`

## Fonctions volontairement absentes

- Pas de compression
- Pas de conversion
- Pas de découpage
- Pas d'extraction audio
- Pas de limite de taille ajoutée par le code Dash

## Installation Render

Variables minimales :

```env
BOT_TOKEN=...
ADMIN_IDS=...
NEXA_CHANNEL=@Nexa_CG
NEXA_CHANNEL_URL=https://t.me/Nexa_CG
TELEGRAM_LOCAL_API=false
DB_PATH=/app/data/dash.sqlite3
```

Pour B2, remplir les quatre variables :

```env
B2_BUCKET=...
B2_ENDPOINT=...
B2_KEY_ID=...
B2_APPLICATION_KEY=...
```

Ne jamais mettre un token Telegram ou une clé B2 dans GitHub.

## Important pour les gros fichiers

Le code Dash n'impose aucune limite applicative de 20 Mo. Les limites éventuelles de téléchargement/envoi restent celles de l'API Telegram utilisée. Backblaze B2 sert de stockage et ne transforme pas à lui seul l'API Telegram officielle en Local Bot API.
