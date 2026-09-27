# Dash Renamer — Local Bot API

Version prête pour Render avec le **Telegram Local Bot API Server** intégré.

Cette version reste un renamer : pas de compression, pas de découpage et pas de conversion.

## Pourquoi le Local Bot API ?

L’API Bot officielle limite le téléchargement des fichiers à 20 Mo. Le Local Bot API en mode `--local` permet de télécharger les fichiers sans cette limite et d’envoyer des fichiers jusqu’à 2000 Mo.

## 1. Variables Render

Ajoute ces variables dans Render :

```text
BOT_TOKEN=TON_NOUVEAU_TOKEN
ADMIN_IDS=TON_ID_TELEGRAM
NEXA_CHANNEL=@Nexa_CG
NEXA_CHANNEL_URL=https://t.me/Nexa_CG

TELEGRAM_API_ID=TON_API_ID
TELEGRAM_API_HASH=TON_API_HASH
TELEGRAM_LOCAL_API=true
TELEGRAM_API_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_API_FILE_BASE_URL=http://127.0.0.1:8081/file/bot

B2_BUCKET=...
B2_ENDPOINT=...
B2_KEY_ID=...
B2_APPLICATION_KEY=...
B2_PREFIX=dash-renamer
B2_RETENTION=false

DB_PATH=/app/data/dash.sqlite3
WEBHOOK_PATH=telegram
PORT=10000
```

`TELEGRAM_API_ID` et `TELEGRAM_API_HASH` viennent de `my.telegram.org`. Le serveur Local Bot API les utilise pour se connecter à Telegram.

## 2. Important : déconnecter le bot de l’API officielle

Telegram indique qu’avant de déplacer un bot vers un serveur Local Bot API, il faut appeler la méthode `logOut` sur l’API officielle. Fais-le **une seule fois**, avec ton nouveau token :

```text
https://api.telegram.org/botTON_NOUVEAU_TOKEN/logOut
```

Tu dois obtenir une réponse JSON avec `"ok":true`. Ne publie jamais ton token dans un dépôt GitHub ou dans une conversation.

## 3. Déploiement Render

1. Mets tous les fichiers du ZIP dans ton dépôt GitHub `Dashrename`.
2. Sur Render, crée/redéploie le service Docker.
3. Ajoute les variables ci-dessus.
4. Lance un nouveau deploy.
5. Dans les logs, tu dois voir :

```text
[Dash] Démarrage du Telegram Local Bot API...
[Dash] Local Bot API prêt.
[Dash] Démarrage du bot Dash Renamer...
```

## 4. Test

Envoie d’abord un petit fichier. Puis teste un fichier de 264 Mo.

Pour un fichier de 264 Mo, le téléchargement doit maintenant passer par :

```text
Telegram
   ↓
Local Bot API :8081
   ↓
Dash Renamer
   ↓
Renommage
   ↓
Retour Telegram
```

Le serveur Local Bot API est lancé dans le même conteneur que Dash, donc `127.0.0.1:8081` est volontairement utilisé.

## 5. Stockage

Le Local Bot API utilise `/app/telegram-data` et `/app/telegram-tmp`. Pour des fichiers très volumineux ou une utilisation importante, prévois un stockage persistant suffisamment grand sur ton hébergement.

## Local Bot API pour les gros fichiers

Cette image utilise `aiogram/telegram-bot-api` comme image de base. Cette image contient le binaire officiel Telegram Bot API à `/usr/local/bin/telegram-bot-api`; le projet Dash réinitialise explicitement l'entrypoint hérité afin de lancer le serveur local puis le bot Python.

Le mode `--local` permet le téléchargement des fichiers sans la limite de 20 Mo de l'API officielle et l'upload jusqu'à 2000 Mo, selon la documentation Telegram. Le serveur local nécessite `TELEGRAM_API_ID` et `TELEGRAM_API_HASH` obtenus sur `my.telegram.org`.

Variables Render minimales :

- `BOT_TOKEN`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_LOCAL_API=true`

Le `start.sh` vérifie que le binaire existe, prépare les dossiers, démarre le Local Bot API, attend son `getMe`, puis démarre Dash.
