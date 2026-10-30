#!/usr/bin/env python3
import os
import sys
import re
import time
import yaml
import requests
from typing import Dict, Any, Optional

from storage import Storage
from fetchers.amazon_scraper import fetch_amazon_product
from bot_notifier import format_telegram_alert, send_via_telegram_bot

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", SCRIPT_DIR)
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml") if os.path.exists(os.path.join(DATA_DIR, "config.yaml")) else os.path.join(SCRIPT_DIR, "config.yaml")

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

class MultiUserBotListener:
    def __init__(self):
        self.config = load_config()
        self.storage = Storage()
        self.token = self.config.get("telegram_bot", {}).get("token", "").strip()
        self.default_owner_id = int(self.config.get("telegram_bot", {}).get("chat_id", 123456789))
        self.affiliate_tag = self.config.get("amazon_affiliate", {}).get("tag", "").strip()
        self.last_update_id = 0
        self.last_poll_time = 0
        self._seed_initial_products()
        self._register_bot_commands()

    def _register_bot_commands(self):
        """Registra il menu dei comandi nativo di Telegram visibile digitando '/'."""
        commands = [
            {"command": "list", "description": "📋 Mostra i tuoi SSD monitorati e prezzi"},
            {"command": "check", "description": "🔍 Controlla subito i prezzi attuali"},
            {"command": "add", "description": "➕ Aggiungi un SSD (link o ASIN con prezzo)"},
            {"command": "target", "description": "🎯 Modifica prezzo target (/target ASIN prezzo)"},
            {"command": "reset", "description": "🔄 Reimposta target automatico (-15%)"},
            {"command": "remove", "description": "🗑️ Rimuovi un SSD dal monitoraggio"},
            {"command": "help", "description": "❓ Guida e comandi"}
        ]
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/setMyCommands", json={"commands": commands}, timeout=10)
        except Exception as e:
            print(f"[Bot] Errore registrazione comandi: {e}")

    def _seed_initial_products(self):
        """Se il database non ha ancora sottoscrizioni, migra i prodotti di config.yaml per l'owner."""
        subs = self.storage.get_user_subscriptions(self.default_owner_id)
        if not subs:
            for p in self.config.get("tracked_products", []):
                asin = p["asin"]
                name = p.get("name", asin)
                capacity = p.get("capacity", "2TB")
                form_factor = p.get("form_factor", "2280")
                target = float(p.get("target_price", 100.0))
                self.storage.upsert_product(asin, name, capacity, form_factor)
                self.storage.subscribe_user(self.default_owner_id, "example_user", asin, target)
            print(f"[Seed] Migrati {len(self.config.get('tracked_products', []))} SSD predefiniti per l'account owner.")

    def send_to_user(self, chat_id: int, text: str) -> bool:
        return send_via_telegram_bot(self.token, chat_id, text)

    def handle_help(self, chat_id: int, first_name: str = "Utente"):
        msg = (
            f"👋 *Ciao {first_name}! Benvenuto su SSD Price Tracker Bot* 🚀\n\n"
            "Questo bot monitora per te i prezzi degli SSD per il Black Friday e ti invia una **notifica privata istantanea** solo quando i prezzi scendono sotto la tua soglia desiderata.\n\n"
            "📌 *Come monitorare un SSD*:\n"
            "Invia semplicemente il link Amazon seguito dal tuo prezzo target desiderato, ad esempio:\n"
            "`https://www.amazon.it/dp/B0CK39YR9V 130`\n"
            "oppure con il solo ASIN:\n"
            "`/add B0CK39YR9V 130`\n\n"
            "📋 *Altri Comandi*:\n"
            "• `/list` : Mostra i tuoi SSD monitorati e i prezzi attuali.\n"
            "• `/check` : Esegue subito un controllo prezzi sui tuoi SSD.\n"
            "• `/target <ASIN> <prezzo>` : Modifica la tua soglia target per un SSD.\n"
            "• `/reset <ASIN>` : Reimposta la soglia al valore automatico (-15% sul prezzo attuale).\n"
            "• `/remove <ASIN>` : Rimuove un SSD dalla tua lista.\n"
            "• `/help` : Mostra questo messaggio di aiuto.\n\n"
            "🔒 *Privacy*: Le notifiche arriveranno solo ed esclusivamente a te in questa chat."
        )
        self.send_to_user(chat_id, msg)

    def handle_list(self, chat_id: int):
        subs = self.storage.get_user_subscriptions(chat_id)
        if not subs:
            self.send_to_user(chat_id, "📋 Non hai ancora nessun SSD nel tuo monitoraggio personale.\n\nIncolla un link Amazon con un prezzo target (es. `https://amazon.it/... 130`) per iniziare!")
            return

        lines = [f"📋 *I tuoi SSD in Monitoraggio* ({len(subs)} totali):\n"]
        for p in subs:
            asin = p["asin"]
            name = p.get("name", asin)
            target = p.get("target_price", 0.0)
            capacity = p.get("capacity", "")
            
            latest_str = f"{p['latest_price']:.2f} €" if p.get("latest_price") else "N/D"
            lowest_str = f"{p['min_price']:.2f} €" if p.get("min_price") else "N/D"

            tag_param = f"?tag={self.affiliate_tag}" if self.affiliate_tag else ""
            lines.append(
                f"▫️ *{name[:38]}* ({capacity})\n"
                f"   💵 Attuale: `{latest_str}` | 📉 Minimo: `{lowest_str}`\n"
                f"   🎯 Tuo Target: `{target:.2f} €` | [Amazon](https://www.amazon.it/dp/{asin}{tag_param})\n"
            )

        self.send_to_user(chat_id, "\n".join(lines))

    def handle_add(self, chat_id: int, username: str, text: str):
        asin_match = re.search(r"(?:/dp/|/gp/product/|)([B0-9A-Z]{10})", text)
        if not asin_match:
            self.send_to_user(chat_id, "❌ Link o ASIN non valido. Invia un link Amazon.it o un codice ASIN (es. `B0B9C4DKKG`).")
            return

        asin = asin_match.group(1)

        # Rimuovi l'URL intero, il comando /add e l'ASIN per isolare solo il prezzo target dell'utente
        clean_text = re.sub(r"https?://\S+", "", text)
        clean_text = re.sub(r"^/add\b", "", clean_text, flags=re.IGNORECASE)
        clean_text = clean_text.replace(asin, "").strip()

        target_price = 0.0
        m_target = re.search(r"([0-9]+(?:[.,][0-9]{1,2})?)", clean_text)
        if m_target:
            try:
                target_price = float(m_target.group(1).replace(",", "."))
            except ValueError:
                pass

        self.send_to_user(chat_id, f"🔍 Recupero informazioni su Amazon.it per `{asin}`...")

        res = fetch_amazon_product(asin)
        if not res["success"]:
            self.send_to_user(chat_id, f"⚠️ Errore nel rilevamento del prodotto su Amazon: {res.get('error')}")
            return

        title = res.get("title") or f"SSD M.2 ({asin})"
        curr_price = res.get("price", 0.0)

        if target_price <= 0:
            target_price = round(curr_price * 0.85, 2) if curr_price > 0 else 100.0

        capacity = "N/D"
        cap_match = re.search(r"\b(500GB|1TB|2TB|4TB|8TB)\b", title, re.IGNORECASE)
        if cap_match:
            capacity = cap_match.group(1).upper()

        # Salva prodotto globale
        self.storage.upsert_product(asin, title, capacity, "2280")
        # Iscrivi questo specifico utente
        self.storage.subscribe_user(chat_id, username, asin, target_price)

        if curr_price > 0:
            self.storage.record_price(asin, curr_price, res.get("discount_pct", 0), res.get("deal_badge", ""), res.get("is_available", True))

        self.send_to_user(
            chat_id,
            f"🎉 *SSD aggiunto al tuo monitoraggio personale!*\n\n"
            f"📦 *{title[:50]}*\n"
            f"▫️ Taglio: *{capacity}* | Formato: *2280*\n"
            f"💰 Prezzo Attuale: *{curr_price:.2f} €*\n"
            f"🎯 Tuo Prezzo Target: *{target_price:.2f} €*\n\n"
            f"🔔 Ti invieremo un messaggio privato appena il prezzo scende sotto `{target_price:.2f} €`!"
        )

    def handle_remove(self, chat_id: int, text: str):
        asin_match = re.search(r"([B0-9A-Z]{10})", text)
        if not asin_match:
            self.send_to_user(chat_id, "❌ Specifica l'ASIN da rimuovere (es. `/remove B0B9C4DKKG`).")
            return

        asin = asin_match.group(1)
        removed = self.storage.unsubscribe_user(chat_id, asin)
        if removed:
            self.send_to_user(chat_id, f"🗑️ Prodotto con ASIN `{asin}` rimosso dalla tua lista personale.")
        else:
            self.send_to_user(chat_id, f"ℹ️ Il prodotto `{asin}` non era presente nella tua lista.")

    def handle_set_target(self, chat_id: int, text: str):
        asin_match = re.search(r"([B0-9A-Z]{10})", text)
        if not asin_match:
            self.send_to_user(chat_id, "❌ Specifica l'ASIN e il nuovo target (es. `/target B0B9C3ZVHR 180`).")
            return

        asin = asin_match.group(1)
        clean = re.sub(r"^/(?:target|soglia|set)\b", "", text, flags=re.IGNORECASE)
        clean = re.sub(r"https?://\S+", "", clean)
        clean = clean.replace(asin, "").strip()

        m_num = re.search(r"([0-9]+(?:[.,][0-9]{1,2})?)", clean)
        if not m_num:
            self.send_to_user(chat_id, "❌ Specifica il nuovo prezzo target (es. `/target B0B9C3ZVHR 180`).")
            return

        new_target = float(m_num.group(1).replace(",", "."))
        prod = self.storage.get_product(asin)
        name = prod.get("name", asin) if prod else asin
        self.storage.subscribe_user(chat_id, "", asin, new_target)

        latest = self.storage.get_latest_price(asin)
        latest_str = f"{latest['price']:.2f} €" if latest else "N/D"

        self.send_to_user(
            chat_id,
            f"🎯 *Prezzo target aggiornato!*\n\n"
            f"📦 *{name[:45]}*\n"
            f"💰 Prezzo attuale: `{latest_str}`\n"
            f"🎯 Nuovo target: *{new_target:.2f} €*"
        )

    def handle_reset_target(self, chat_id: int, text: str):
        asin_match = re.search(r"([B0-9A-Z]{10})", text)
        if not asin_match:
            self.send_to_user(chat_id, "❌ Specifica l'ASIN da resettare (es. `/reset B0B9C3ZVHR`).")
            return

        asin = asin_match.group(1)
        prod = self.storage.get_product(asin)
        name = prod.get("name", asin) if prod else asin

        latest = self.storage.get_latest_price(asin)
        curr_price = latest["price"] if latest else 0.0

        if curr_price <= 0:
            res = fetch_amazon_product(asin)
            curr_price = res.get("price", 0.0)

        new_target = round(curr_price * 0.85, 2) if curr_price > 0 else 100.0
        self.storage.subscribe_user(chat_id, "", asin, new_target)

        self.send_to_user(
            chat_id,
            f"🔄 *Prezzo target ricalcolato (-15% dal mercato attuale)!*\n\n"
            f"📦 *{name[:45]}*\n"
            f"💰 Prezzo attuale rilevato: `{curr_price:.2f} €`\n"
            f"🎯 Nuovo target reimpostato: *{new_target:.2f} €*"
        )

    def run_check_cycle(self, for_user_chat_id: Optional[int] = None):
        """Scansiona tutti gli ASIN monitorati e notifica i rispettivi utenti iscritti."""
        if for_user_chat_id:
            # Scansione manuale richiesta da un utente: controlla solo i suoi ASIN
            user_subs = self.storage.get_user_subscriptions(for_user_chat_id)
            asins_to_check = [s["asin"] for s in user_subs]
            self.send_to_user(for_user_chat_id, f"🔍 Scansione in corso per i tuoi {len(asins_to_check)} SSD...")
        else:
            # Scansione automatica programmata di background: controlla tutti gli ASIN attivi
            asins_to_check = self.storage.get_all_active_asins()

        alert_rules = self.config.get("alert_rules", {})
        cooldown_hours = alert_rules.get("cooldown_hours", 6)
        notify_on_all_time_low = alert_rules.get("notify_on_all_time_low", True)
        delay = self.config.get("poll_delay_seconds", 4)

        for idx, asin in enumerate(asins_to_check, start=1):
            res = fetch_amazon_product(asin)
            if not res["success"]:
                time.sleep(delay)
                continue

            curr_price = res["price"]
            discount_pct = res["discount_pct"]
            deal_badge = res["deal_badge"]

            prev_lowest = self.storage.get_lowest_price(asin)
            self.storage.record_price(asin, curr_price, discount_pct, deal_badge, res["is_available"])

            # Recupera tutti gli utenti che stanno monitorando questo ASIN
            subscribers = self.storage.get_subscribers_for_asin(asin)
            prod_info = self.storage.get_product(asin) or {}
            prod_name = prod_info.get("name") or res.get("title") or asin
            capacity = prod_info.get("capacity", "2TB")
            form_factor = prod_info.get("form_factor", "2280")

            for sub in subscribers:
                user_id = sub["chat_id"]
                user_target = sub["target_price"]

                should_alert, reason = self.storage.should_send_user_alert(
                    chat_id=user_id,
                    asin=asin,
                    current_price=curr_price,
                    target_price=user_target,
                    cooldown_hours=cooldown_hours,
                    notify_on_all_time_low=notify_on_all_time_low
                )

                if should_alert:
                    alert_msg = format_telegram_alert(
                        product_name=prod_name,
                        form_factor=form_factor,
                        capacity=capacity,
                        current_price=curr_price,
                        target_price=user_target,
                        previous_low=prev_lowest,
                        discount_pct=discount_pct,
                        deal_badge=deal_badge,
                        asin=asin,
                        reason=reason,
                        affiliate_tag=self.affiliate_tag
                    )
                    # Invia la notifica ESCLUSIVAMENTE a questo utente!
                    self.send_to_user(user_id, alert_msg)
                    self.storage.record_user_alert(user_id, asin, curr_price, reason)

            time.sleep(delay)

        if for_user_chat_id:
            self.send_to_user(for_user_chat_id, "🏁 Scansione completata! Tutti i prezzi sono stati aggiornati.")

    def process_incoming_message(self, message: Dict[str, Any]):
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return

        username = message.get("from", {}).get("username", "")
        first_name = message.get("from", {}).get("first_name", "Utente")
        text = message.get("text", "").strip()
        if not text:
            return

        print(f"[Bot] Messaggio da {first_name} (@{username}, chat {chat_id}): {text}")

        cmd = text.split()[0].lower() if text.startswith("/") else ""

        if cmd in ["/start", "/help"]:
            self.handle_help(chat_id, first_name)
        elif cmd in ["/list", "/elenco"]:
            self.handle_list(chat_id)
        elif cmd in ["/check", "/scansiona"]:
            self.run_check_cycle(for_user_chat_id=chat_id)
        elif cmd in ["/remove", "/del", "/delete"]:
            self.handle_remove(chat_id, text)
        elif cmd in ["/target", "/soglia", "/set"]:
            self.handle_set_target(chat_id, text)
        elif cmd in ["/reset"]:
            self.handle_reset_target(chat_id, text)
        elif cmd in ["/add"] or "amazon.it" in text or re.search(r"B0[0-9A-Z]{8}", text):
            self.handle_add(chat_id, username, text)
        else:
            self.send_to_user(chat_id, "❓ Comando non riconosciuto. Invia un link Amazon con prezzo target, oppure `/list`, `/check`, `/help`.")

    def run(self):
        print("🚀 Multi-User Bot Listener avviato. Aperto a qualsiasi utente Telegram...")
        CHECK_INTERVAL_SECONDS = 1800
        self.last_poll_time = time.time()

        while True:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                params = {
                    "offset": self.last_update_id + 1,
                    "timeout": 20
                }
                resp = requests.get(url, params=params, timeout=25)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        self.last_update_id = update["update_id"]
                        if "message" in update:
                            self.process_incoming_message(update["message"])

                now = time.time()
                if now - self.last_poll_time >= CHECK_INTERVAL_SECONDS:
                    print("[Daemon] Esecuzione scansione automatica programmata di background...")
                    self.run_check_cycle()
                    self.last_poll_time = now

            except requests.exceptions.Timeout:
                pass
            except Exception as e:
                print(f"[Error] Loop exception: {e}")
                time.sleep(5)

if __name__ == "__main__":
    listener = MultiUserBotListener()
    listener.run()
