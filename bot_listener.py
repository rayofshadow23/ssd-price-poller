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
        tg_cfg = self.config.get("telegram_bot", {}) or {}
        self.token = (tg_cfg.get("token") or "").strip()

        # --- Access control (all values come from the private config.yaml) ---
        # Owner: can use /users. Read from owner_chat_id, or from the old "chat_id" key.
        owner_raw = tg_cfg.get("owner_chat_id") or tg_cfg.get("chat_id")
        self.owner_id: Optional[int] = int(owner_raw) if owner_raw else None
        self.owner_username = (tg_cfg.get("owner_username") or "").strip()

        # Allowlist: ONLY these chat IDs can use the bot. Empty list = nobody (fail closed).
        self.allowed_ids = {int(x) for x in (tg_cfg.get("allowed_chat_ids") or [])}
        if not self.allowed_ids:
            print("[SECURITY] 'telegram_bot.allowed_chat_ids' è vuoto: il bot ignorerà TUTTI i messaggi.")

        # --- Abuse limits ---
        limits = self.config.get("limits", {}) or {}
        self.max_asins_per_user = int(limits.get("max_asins_per_user", 15))
        self.check_cooldown_s = float(limits.get("check_cooldown_minutes", 10)) * 60
        self._last_check: Dict[int, float] = {}

        self.affiliate_tag = (self.config.get("amazon_affiliate", {}) or {}).get("tag", "").strip()
        self.last_update_id = 0
        self.last_poll_time = 0
        self._seed_initial_products()
        self._register_bot_commands()

    def is_allowed(self, chat_id: int) -> bool:
        try:
            return int(chat_id) in self.allowed_ids
        except (TypeError, ValueError):
            return False

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
        if self.owner_id is None or not self.is_allowed(self.owner_id):
            return
        subs = self.storage.get_user_subscriptions(self.owner_id)
        if not subs:
            for p in self.config.get("tracked_products", []):
                asin = p["asin"]
                name = p.get("name", asin)
                capacity = p.get("capacity", "2TB")
                form_factor = p.get("form_factor", "2280")
                target = float(p.get("target_price", 100.0))

                self.storage.upsert_product(asin, name, capacity, form_factor)
                self.storage.subscribe_user(self.owner_id, self.owner_username, asin, target)
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
            keepa_url = f"https://keepa.com/#!product/8-{asin}"

            lines.append(
                f"▫️ *{name[:38]}* ({capacity})\n"
                f"   💵 Attuale: `{latest_str}` | 📉 Minimo: `{lowest_str}`\n"
                f"   🎯 Tuo Target: `{target:.2f} €`\n"
                f"   🛒 [Amazon](https://www.amazon.it/dp/{asin}{tag_param}) | 📈 [Grafico Keepa]({keepa_url})\n"
            )
        self.send_to_user(chat_id, "\n".join(lines))

    def handle_report(self, chat_id: int):
        print("[DEBUG] handle_report called")
        subs = self.storage.get_user_subscriptions(chat_id)
        if not subs:
            self.send_to_user(chat_id, "📋 Nessun SSD in monitoraggio. Aggiungine uno con `/add <link> <target>`.")
            return

        lines = [f"📊 *Report SSD* ({len(subs)} totali)\n"]
        for p in subs:
            asin = p["asin"]
            name = p.get("name", asin)
            target = p.get("target_price", 0.0)
            capacity = p.get("capacity", "")
            res = fetch_amazon_product(asin)
            current = res.get("price") if res.get("success") else None
            status = ""
            if current is not None and target and current <= target:
                status = " ✅ TARGET RAGGIUNTO"
            elif current is not None and target:
                status = f" (gap: {current-target:+.2f} €)"
            cur_str = f"{current:.2f} €" if current is not None else "N/D"
            tag_param = f"?tag={self.affiliate_tag}" if self.affiliate_tag else ""
            keepa_url = f"https://keepa.com/#!product/8-{asin}"
            lines.append(
                f"▫️ *{name[:36]}* ({capacity})\n"
                f"   💵 Attuale: `{cur_str}` | 🎯 Target: `{target:.2f} €`{status}\n"
                f"   🛒 [Amazon](https://www.amazon.it/dp/{asin}{tag_param}) | 📈 [Keepa]({keepa_url})\n"
            )
        self.send_to_user(chat_id, "\n".join(lines))

    def handle_add(self, chat_id: int, username: str, text: str):
        asin_match = re.search(r"(?:/dp/|/gp/product/|)([B0-9A-Z]{10})", text)
        if not asin_match:
            self.send_to_user(chat_id, "❌ Link o ASIN non valido. Invia un link Amazon.it o un codice ASIN (es. `B0B9C4DKKG`).")
            return

        asin = asin_match.group(1)

        # Per-user cap: protects the home IP from unlimited requests
        subs = self.storage.get_user_subscriptions(chat_id)
        already_tracked = any(s["asin"] == asin for s in subs)
        if not already_tracked and len(subs) >= self.max_asins_per_user:
            self.send_to_user(
                chat_id,
                f"⛔ Hai raggiunto il limite di {self.max_asins_per_user} SSD monitorati. "
                "Rimuovine uno con `/remove <ASIN>` prima di aggiungerne un altro."
            )
            return

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

        if res.get("blocked"):
            self.send_to_user(chat_id, f"⏸️ Amazon è in pausa di sicurezza ({res.get('error')}). Riprova più tardi.")
            return

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

        keepa_url = f"https://keepa.com/#!product/8-{asin}"
        self.send_to_user(
            chat_id,
            f"🎉 *SSD aggiunto al tuo monitoraggio personale!*\n\n"
            f"📦 *{title[:50]}*\n"
            f"▫️ Taglio: *{capacity}* | Formato: *2280*\n"
            f"💰 Prezzo Attuale: *{curr_price:.2f} €*\n"
            f"🎯 Tuo Prezzo Target: *{target_price:.2f} €*\n\n"
            f"📈 [Visualizza storico prezzi su Keepa]({keepa_url})\n"
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

    def handle_users(self, chat_id: int):
        if self.owner_id is None or chat_id != self.owner_id:
            self.send_to_user(chat_id, "⛔ Comando riservato all'amministratore.")
            return

        users = self.storage.get_all_users_stats()
        lines = [f"👥 *Utenti Registrati nel Database* ({len(users)} totali):\n"]
        for u in users:
            uname = f"@{u['username']}" if u['username'] != "N/D" else "N/D"
            allowed_mark = "✅" if self.is_allowed(u['chat_id']) else "🚫"
            lines.append(
                f"▫️ *Utente*: `{uname}` (ID: `{u['chat_id']}`) {allowed_mark}\n"
                f"   📦 SSD Monitorati: *{u['ssd_count']}*\n"
                f"   📅 Iscritto il: `{u['joined_at'][:16]}`\n"
            )
        self.send_to_user(chat_id, "\n".join(lines))

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
            if res.get("blocked"):
                self.send_to_user(chat_id, f"⏸️ Amazon è in pausa di sicurezza ({res.get('error')}). Riprova più tardi.")
                return
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
        """Scansiona gli ASIN monitorati e notifica i rispettivi utenti iscritti (solo utenti in allowlist)."""
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
            # Only subscribers in the allowlist count. Old subscriptions from users who are
            # not allowed are ignored: no Amazon request is made for ASINs only they follow.
            subscribers = [
                s for s in self.storage.get_subscribers_for_asin(asin)
                if self.is_allowed(s["chat_id"])
            ]
            if not subscribers:
                continue

            res = fetch_amazon_product(asin)

            if res.get("blocked"):
                # Amazon showed a CAPTCHA / block: stop the whole cycle, do not try other ASINs.
                print(f"[Daemon] Amazon in pausa ({res.get('error')}). Scansione interrotta.")
                if for_user_chat_id:
                    self.send_to_user(for_user_chat_id, f"⏸️ Amazon è in pausa di sicurezza ({res.get('error')}). Scansione interrotta.")
                return

            if not res["success"]:
                time.sleep(delay)
                continue

            curr_price = res["price"]
            discount_pct = res["discount_pct"]
            deal_badge = res["deal_badge"]
            prev_lowest = self.storage.get_lowest_price(asin)

            self.storage.record_price(asin, curr_price, discount_pct, deal_badge, res["is_available"])

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

    def handle_check(self, chat_id: int):
        """Manual scan with a per-user cooldown."""
        now = time.time()
        wait = self.check_cooldown_s - (now - self._last_check.get(chat_id, 0.0))
        if wait > 0:
            self.send_to_user(chat_id, f"⏳ Controllo manuale disponibile tra circa {int(wait // 60) + 1} min.")
            return
        self._last_check[chat_id] = now
        self.run_check_cycle(for_user_chat_id=chat_id)

    def process_incoming_message(self, message: Dict[str, Any]):
        chat = message.get("chat", {}) or {}
        chat_id = chat.get("id")
        if not chat_id:
            return

        # Access control: private chats only, and only chat IDs in the allowlist.
        # Everyone else is ignored silently (no reply, message text is not logged).
        if chat.get("type") != "private" or not self.is_allowed(chat_id):
            print(f"[Security] Messaggio ignorato da chat non autorizzata: {chat_id}")
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
        elif cmd in ["/report", "/resoconto"]:
            self.handle_report(chat_id)
        elif cmd in ["/users", "/utenti", "/stats"]:
            self.handle_users(chat_id)
        elif cmd in ["/check", "/scansiona"]:
            self.handle_check(chat_id)
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
        print(f"🚀 Multi-User Bot Listener avviato. Accesso limitato a {len(self.allowed_ids)} chat in allowlist.")
        from fetchers.amazon_scraper import get_public_ip
        print(f"🌐 IP pubblico in uscita: {get_public_ip()}")
        interval_min = self.config.get("check_interval_minutes", 30)
        print(f"[Daemon] Intervallo scansione automatica impostato a {interval_min} minuti.")

        self.last_poll_time = time.time()

        while True:
            try:
                # Ricarica l'intervallo se viene modificato in config
                CHECK_INTERVAL_SECONDS = self.config.get("check_interval_minutes", 30) * 60

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
