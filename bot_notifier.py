import subprocess
import requests
from typing import Dict, Any, Optional

def format_telegram_alert(
    product_name: str,
    form_factor: str,
    capacity: str,
    current_price: float,
    target_price: float,
    previous_low: Optional[float],
    discount_pct: float,
    deal_badge: str,
    asin: str,
    reason: str,
    affiliate_tag: str = ""
) -> str:
    """Formatta il messaggio Telegram in Markdown elegante con emoji e dettagli chiari."""
    badge_line = f"🏷️ *Badge*: {deal_badge}\n" if deal_badge else ""
    
    if previous_low and current_price < previous_low:
        low_line = f"📉 *Minimo Storico*: {current_price:.2f} € (🎉 *Nuovo Minimo Assoluto!*)\n"
    elif previous_low:
        low_line = f"📊 *Minimo Storico Precedente*: {previous_low:.2f} €\n"
    else:
        low_line = "📊 *Minimo*: Primo rilevamento nel database\n"

    disc_line = f"🔥 *Sconto Listino*: -{discount_pct:.0f}%\n" if discount_pct > 0 else ""
    savings = target_price - current_price
    savings_line = f"💵 *Risparmio vs Target*: -{savings:.2f} €\n" if savings > 0 else ""

    tag_param = f"?tag={affiliate_tag}" if affiliate_tag else ""
    url = f"https://www.amazon.it/dp/{asin}{tag_param}"
    keepa_url = f"https://keepa.com/#!product/8-{asin}"

    msg = (
        f"🚨 *ALLERTA PREZZO SSD BLACK FRIDAY!* 🚨\n\n"
        f"📦 *{product_name}*\n"
        f"▫️ *Formato*: {form_factor} | *Taglio*: {capacity}\n"
        f"▫️ *Motivo*: {reason}\n\n"
        f"💰 *Prezzo Attuale*: *{current_price:.2f} €*\n"
        f"🎯 *Prezzo Target*: {target_price:.2f} €\n"
        f"{savings_line}"
        f"{disc_line}"
        f"{low_line}"
        f"{badge_line}\n"
        f"🛒 [👉 Apri offerta su Amazon.it]({url})\n"
        f"📈 [📊 Grafico storico prezzi Keepa]({keepa_url})"
    )
    return msg

def send_via_telegram_bot(token: str, chat_id: Any, text: str) -> bool:
    """Invia un messaggio tramite la Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if data.get("ok"):
            return True
        else:
            print(f"[BotNotifier] Errore Bot API: {data.get('description')}")
            return False
    except Exception as e:
        print(f"[BotNotifier] Eccezione di rete: {e}")
        return False

def send_via_tgpipe(text: str, recipient: str = "me") -> bool:
    """Fallback: invia il messaggio usando tgpipe (autenticato come utente personale)."""
    try:
        cmd = ["/home/ray/.local/bin/tgpipe", "messages", "send", recipient, "--text", text]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            return True
        else:
            print(f"[BotNotifier] Errore tgpipe: {res.stderr.strip()}")
            return False
    except Exception as e:
        print(f"[BotNotifier] Impossibile eseguire tgpipe: {e}")
        return False

def send_notification(config: Dict[str, Any], text: str) -> bool:
    """
    Invia la notifica provando prima il bot dedicato configurato in config.yaml.
    Se il bot non è configurato o fallisce, e il fallback è abilitato, prova con tgpipe.
    """
    bot_conf = config.get("telegram_bot", {})
    token = bot_conf.get("token", "").strip()
    chat_id = bot_conf.get("chat_id", 123456789)
    fallback = bot_conf.get("fallback_to_tgpipe", True)

    # Verifica se il token ha un formato verosimile per un bot Telegram (es. 123456:ABC-DEF...)
    is_valid_token = bool(token and ":" in token and not token.startswith("INSERISCI_"))

    if is_valid_token:
        print(f"[BotNotifier] Invio notifica tramite Bot Telegram dedicato (chat {chat_id})...")
        if send_via_telegram_bot(token, chat_id, text):
            return True
        print("[BotNotifier] Invio via Bot fallito.")

    if fallback:
        print("[BotNotifier] Utilizzo fallback tgpipe (Saved Messages)...")
        if send_via_tgpipe(text, recipient="me"):
            return True

    print("[BotNotifier] Nessun canale Telegram riuscito o configurato.")
    return False
