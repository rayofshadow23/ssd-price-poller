#!/bin/sh
set -e

# Se non esiste la cartella data, creala
mkdir -p /app/data

# Se il file config.yaml non esiste ancora nel volume persistente, copia il template
if [ ! -f /app/data/config.yaml ]; then
    echo "[Entrypoint] Inizializzazione prima configurazione in /app/data/config.yaml..."
    if [ -f /app/config.yaml ]; then
        cp /app/config.yaml /app/data/config.yaml
    else
        cp /app/config.example.yaml /app/data/config.yaml
    fi
fi

# Se è stato specificato un token via variabile d'ambiente, sincronizzalo
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
    echo "[Entrypoint] Aggiornamento token bot da variabile d'ambiente..."
    sed -i "s/token: .*/token: \"$TELEGRAM_BOT_TOKEN\"/" /app/data/config.yaml
fi

exec "$@"
