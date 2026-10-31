# 🚀 SSD Price Drop Poller & Telegram Bot (Black Friday Edition)

Bot Telegram autonomo e leggero per il monitoraggio prezzi degli SSD M.2 (PCIe 4.0 / SATA 2280) su Amazon.it in vista del Black Friday.
Progettato per girare 24/7 su **Raspberry Pi**, server Linux o qualsiasi host Docker con consumo minimo di risorse (< 30 MB RAM).

---

## 🌟 Caratteristiche

- **Controllo Accessi & Allowlist**: Accesso privato protetto (`allowed_chat_ids` in `config.yaml`): solo gli utenti autorizzati possono inviare comandi al bot. Tutti gli altri messaggi vengono ignorati silenziosamente.
- **Scraper Sicuro Anti-Blocco**: Impersonificazione TLS Chrome (tramite `curl_cffi`), rate limiting globale con jitter casuale, cache 10 min e *circuit breaker* automatico in caso di CAPTCHA/429.
- **Protezione Anti-Abuso**: Limite configurabile di SSD per utente (default 15) e cooldown sulla scansione manuale `/check` (default 10 min) per non sovraccaricare l'IP di casa.
- **Aggiunta istantanea da Chat Telegram**: Incolla qualsiasi link Amazon o ASIN direttamente in chat con il tuo prezzo target desiderato (es. `https://amazon.it/... 130`).
- **Database SQLite embedded**: Zero configurazione, salva automaticamente storico prezzi, all-time lows e previene lo spam (cooldown 6 ore).
- **Controllo automatico periodico**: Esegue la scansione dei prezzi ogni 30 minuti in background e notifica all'istante quando un SSD scende sotto la soglia.
- **Docker & Raspberry Pi Ready**: `docker-compose.yml` preconfigurato con volumi persistenti e riavvio automatico al boot (`restart: unless-stopped`).

---

## ⚡ Avvio Rapido su Raspberry Pi (con Docker Compose)

### 1. Copia la cartella sul Raspberry Pi
```bash
# Esempio via rsync o scp
scp -r ssd-price-poller pi@raspberrypi.local:~/
```

### 2. Configura Token e Utenti Autorizzati
Copia `config.example.yaml` in `config.yaml` e inserisci il token Telegram e gli ID autorizzati:
```yaml
telegram_bot:
  token: "IL_TUO_TOKEN_BOTFATHER"
  owner_chat_id: 12345678       # Tuo ID Telegram (abilita il comando admin /users)
  owner_username: "tuo_username"
  allowed_chat_ids:             # IMPORTANTE: Solo questi ID possono usare il bot
    - 12345678
    - 87654321

limits:
  max_asins_per_user: 15        # Max SSD per utente
  check_cooldown_minutes: 10    # Cooldown tra comandi /check consecutivi
```
*(Nota: per trovare il tuo ID numerico di Telegram puoi scrivere al bot `@userinfobot`).*

### 3. Avvia il container in background
```bash
cd ssd-price-poller
docker compose up -d --build
```

Il bot sarà immediatamente attivo e in ascolto 24/7!

---

## 🔨 Come Compilare e Costruire l'Immagine (Build Instructions)

Puoi compilare l'immagine Docker in diversi modi in base al tuo ambiente:

### Metodo A: Compilazione con Docker Compose (Consigliato sul Raspberry Pi)
Se ti trovi direttamente sul Raspberry Pi (o su un server Linux):
```bash
# Costruisce l'immagine localmente
docker compose build

# Oppure compila e avvia contemporaneamente in un solo comando:
docker compose up -d --build
```

### Metodo B: Compilazione manuale con `docker build`
Se preferisci gestire l'immagine Docker manualmente:
```bash
# Compilazione dell'immagine con tag locale
docker build -t ssd-price-bot:latest .

# Esecuzione manuale con mount del volume per il database
docker run -d \
  --name ssd-price-bot \
  --restart unless-stopped \
  -v $(pwd)/data:/app/data \
  -e TZ=Europe/Rome \
  ssd-price-bot:latest
```

### Metodo C: Cross-compilazione da PC (x86_64) per Raspberry Pi (ARM64 / ARMv7)
Se compili dal tuo computer fisso/portatile per poi trasferire l'immagine sul Raspberry Pi senza doverla compilare sull'hardware limitato del Pi, usa `docker buildx`:
```bash
# 1. Abilita il builder multi-architettura
docker buildx create --name multiarch --use 2>/dev/null || docker buildx use multiarch

# 2. Compila per Raspberry Pi OS 64-bit (aarch64) o 32-bit (armv7)
docker buildx build --platform linux/arm64,linux/arm/v7 -t ssd-price-bot:latest --load .

# 3. (Opzionale) Esporta l'immagine compilata come archivio .tar per caricarla sul Pi:
docker save ssd-price-bot:latest | gzip > ssd-price-bot.tar.gz
scp ssd-price-bot.tar.gz pi@raspberrypi.local:~/

# Sul Raspberry Pi basterà importarla con:
docker load < ssd-price-bot.tar.gz
docker compose up -d
```

### Metodo D: Esecuzione Nativa in Python (Senza Docker)
Se non vuoi usare Docker e preferisci un ambiente virtuale Python standard:
```bash
# 1. Crea e attiva il virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Installa le dipendenze
pip install -r requirements.txt

# 3. Avvia il bot listener
python3 bot_listener.py
```

---

## 🛠️ Comandi Utili Docker

- **Visualizzare i log in tempo reale**:
  ```bash
  docker compose logs -f
  ```
- **Riavviare il bot**:
  ```bash
  docker compose restart
  ```
- **Fermare il container**:
  ```bash
  docker compose down
  ```
- **Ricompilare dopo aver modificato il codice**:
  ```bash
  docker compose up -d --build
  ```

---

## 📱 Comandi Telegram per gli Utenti

| Comando | Descrizione |
| :--- | :--- |
| `<link Amazon> <prezzo>` | Incolla un link con il prezzo target (es. `https://amazon.it/dp/B0CK39YR9V 130`) per iniziare a monitorarlo. |
| `/add <ASIN> <prezzo>` | Aggiunge un SSD indicando solo l'ASIN e il prezzo target. |
| `/list` | Mostra i tuoi SSD monitorati con prezzo attuale, target e minimo storico. |
| `/target <ASIN> <prezzo>` | Modifica/aggiorna la tua soglia target per un SSD già monitorato. |
| `/reset <ASIN>` | Reimposta la soglia al valore automatico di sconto (-15% sul prezzo attuale). |
| `/check` | Forza un controllo istantaneo dei tuoi SSD (soggetto a cooldown di 10 min). |
| `/remove <ASIN>` | Rimuove un SSD dalla tua lista personale. |
| `/users` | Mostra le statistiche degli utenti registrati (riservato all'amministratore/owner). |
| `/help` | Mostra la guida dei comandi. |

---

## 🌐 Requisiti di Rete e Firewall (per Raspberry Pi / Reti Chiuse)

Se installi il bot su una rete protetta o isolata (es. VLAN dedicata):

- **Porte in Ingresso (Inbound): NESSUNA.** Il bot opera come client via *long-polling* (`getUpdates`); non serve aprire alcuna porta né fare port forwarding sul router.
- **Porte in Uscita (Outbound):**
  - **TCP `443` (HTTPS):** verso `api.telegram.org` e `www.amazon.it`.
  - **UDP/TCP `53` (DNS):** per la risoluzione dei domini.

---

## 📂 Struttura Cartella e Dati Persistenti

```
ssd-price-poller/
├── data/                  # Cartella montata nel container (dati persistenti)
│   ├── config.yaml        # Configurazione principale e token
│   └── ssd_prices.db      # Database SQLite (storico prezzi e utenti)
├── docker-compose.yml     # Configurazione Docker Compose
├── Dockerfile             # Immagine Python 3.12-slim ARM64/AMD64
├── entrypoint.sh          # Script di avvio e sincronizzazione volume
├── bot_listener.py        # Demone principale multi-utente
├── storage.py             # Gestione database SQLite multi-tenant
├── fetchers/              # Scraper Amazon.it ottimizzato
└── requirements.txt       # Dipendenze Python
```
