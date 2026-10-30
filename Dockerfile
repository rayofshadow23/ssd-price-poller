# Immagine Python leggera multi-architettura (supporta Raspberry Pi ARM64/ARMv7 e x86_64)
FROM python:3.12-slim

# Evita buffer per i log stdout e disabilita cache pip
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

WORKDIR /app

# Installa pacchetti minimi di sistema e certificati SSL
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Installa dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il codice dell'applicazione
COPY . .

# Rendi eseguibile l'entrypoint
RUN chmod +x entrypoint.sh

# Directory per il volume persistente (database SQLite + config)
VOLUME ["/app/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "bot_listener.py"]
