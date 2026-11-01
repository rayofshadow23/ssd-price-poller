#!/usr/bin/env python3
import os
import sys
import re
import time
import yaml
import argparse
from typing import Dict, Any

from storage import Storage
from fetchers.amazon_scraper import fetch_amazon_product
from bot_notifier import format_telegram_alert, send_via_telegram_bot, send_notification

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", SCRIPT_DIR)
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml") if os.path.exists(os.path.join(DATA_DIR, "config.yaml")) else os.path.join(SCRIPT_DIR, "config.yaml")

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        print(f"[Error] File di configurazione non trovato in {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def run_test_alert(config: Dict[str, Any]):
    print("=== Invio messaggio di test Telegram ===")
    sample_text = (
        "🚀 *SSD Price Drop Poller - Test Attivo!*\n\n"
        "Il bot per il tracciamento dei prezzi SSD M.2 2280 per il Black Friday è configurato correttamente.\n"
        "Riceverai notifiche istantanee non appena i modelli monitorati scendono sotto la soglia target o toccano un nuovo minimo storico."
    )
    ok = send_notification(config, sample_text)
    if ok:
        print("✅ Notifica di test inviata con successo!")
    else:
        print("❌ Invio notifica di test fallito. Verifica il token bot in config.yaml.")

def add_product(config: Dict[str, Any], url_or_asin: str, target_price: float, capacity: str = "N/D", name_override: str = ""):
    # Estrai ASIN sia che venga passato un ASIN pulito sia un URL di Amazon
    asin_match = re.search(r"(?:/dp/|/gp/product/|)([B0-9A-Z]{10})", url_or_asin)
    if not asin_match:
        print(f"❌ Impossibile identificare un ASIN Amazon valido da: {url_or_asin}")
        return
    asin = asin_match.group(1)

    # Verifica se è già monitorato
    existing = [p for p in config.get("tracked_products", []) if p["asin"] == asin]
    if existing:
        print(f"⚠️ Il prodotto con ASIN {asin} è già presente nel monitoraggio ({existing[0].get('name')}).")
        # Aggiorna il target se specificato
        if target_price > 0:
            existing[0]["target_price"] = target_price
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, sort_keys=False)
            print(f"✅ Prezzo target aggiornato a {target_price:.2f} € in config.yaml.")
        return

    print(f"🔍 Rilevamento dettagli prodotto su Amazon.it per ASIN {asin}...")
    res = fetch_amazon_product(asin)
    title = name_override or (res["title"] if res.get("title") else f"SSD M.2 ({asin})")
    curr_price = res.get("price", 0.0)

    # Rileva capacità dal titolo se non specificata
    if capacity == "N/D" and title:
        cap_match = re.search(r"\b(500GB|1TB|2TB|4TB|8TB)\b", title, re.IGNORECASE)
        if cap_match:
            capacity = cap_match.group(1).upper()

    new_prod = {
        "asin": asin,
        "name": title,
        "target_price": target_price if target_price > 0 else (curr_price * 0.85 if curr_price > 0 else 100.0),
        "capacity": capacity,
        "form_factor": "2280"
    }

    config.setdefault("tracked_products", []).append(new_prod)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)

    print(f"✅ Prodotto aggiunto con successo a config.yaml!")
    print(f"   Nome: {new_prod['name']}")
    print(f"   ASIN: {asin}")
    print(f"   Prezzo Attuale: {curr_price:.2f} €" if curr_price > 0 else "   Prezzo: N/D")
    print(f"   Prezzo Target: {new_prod['target_price']:.2f} €")
    print(f"   Taglio: {capacity}")

    if curr_price > 0:
        storage = Storage()
        storage.record_price(asin, curr_price, res.get("discount_pct", 0), res.get("deal_badge", ""), res.get("is_available", True))
        print(f"   Prezzo iniziale registrato nel database.")

def list_users(storage: Storage):
    users = storage.get_all_users_stats()
    print(f"\n👥 Utenti registrati nel database ({len(users)} totali):\n")
    print(f"{'Chat ID':<12} | {'Username':<18} | {'SSD Monitorati':<14} | {'Data Iscrizione'}")
    print("-" * 65)
    for u in users:
        uname = f"@{u['username']}" if u['username'] != "N/D" else "N/D"
        print(f"{u['chat_id']:<12} | {uname:<18} | {u['ssd_count']:<14} | {u['joined_at']}")
    print()

def list_products(config: Dict[str, Any], storage: Storage):
    print(f"\n📋 Prodotti configurati in monitoraggio ({len(products)} totali):\n")
    print(f"{'ASIN':<12} | {'Taglio':<6} | {'Target €':<10} | {'Ultimo €':<10} | {'Minimo €':<10} | Nome")
    print("-" * 80)
    for p in products:
        asin = p["asin"]
        name = p.get("name", "N/A")
        capacity = p.get("capacity", "2280")
        target = p.get("target_price", 0.0)
        
        latest = storage.get_latest_price(asin)
        latest_str = f"{latest['price']:.2f} €" if latest else "N/D"
        
        lowest = storage.get_lowest_price(asin)
        lowest_str = f"{lowest:.2f} €" if lowest else "N/D"
        
        print(f"{asin:<12} | {capacity:<6} | {target:<10.2f} | {latest_str:<10} | {lowest_str:<10} | {name[:30]}")
    print()

def check_all_products(config: Dict[str, Any], storage: Storage, dry_run: bool = False):
    """
    Scansiona gli ASIN iscritti nel DATABASE (stessa logica del /check di Telegram).
    I target sono quelli impostati per ogni utente con /target. Gli alert vanno solo
    agli utenti in telegram_bot.allowed_chat_ids, ognuno con il proprio target.
    """
    tg_cfg = config.get("telegram_bot", {}) or {}
    token = (tg_cfg.get("token") or "").strip()
    allowed = {int(x) for x in (tg_cfg.get("allowed_chat_ids") or [])}
    alert_rules = config.get("alert_rules", {})
    cooldown_hours = alert_rules.get("cooldown_hours", 6)
    notify_on_all_time_low = alert_rules.get("notify_on_all_time_low", True)
    delay = config.get("poll_delay_seconds", 4)

    if not allowed:
        print("[SECURITY] telegram_bot.allowed_chat_ids vuoto: nessun alert verrà inviato.")

    asins = storage.get_all_active_asins()
    print(f"\n🔍 Avvio scansione prezzi SSD ({len(asins)} prodotti dal database)... [DryRun={dry_run}]")

    alerts_triggered = 0

    for idx, asin in enumerate(asins, start=1):
        subscribers = [s for s in storage.get_subscribers_for_asin(asin) if int(s["chat_id"]) in allowed]
        if not subscribers:
            print(f"\n[{idx}/{len(asins)}] {asin}: nessun utente autorizzato iscritto, salto.")
            continue

        prod = storage.get_product(asin) or {}
        name = prod.get("name") or asin
        form_factor = prod.get("form_factor", "2280")
        capacity = prod.get("capacity", "N/D")

        print(f"\n[{idx}/{len(asins)}] Controllo {asin}: {name} ({len(subscribers)} utenti)")

        result = fetch_amazon_product(asin)
        if result.get("blocked"):
            print(f"  ⏸️  Amazon in pausa di sicurezza ({result.get('error')}). Scansione interrotta.")
            return
        if not result["success"]:
            print(f"  ⚠️  Errore nel rilevamento: {result.get('error')}")
            time.sleep(delay)
            continue

        curr_price = result["price"]
        discount_pct = result["discount_pct"]
        deal_badge = result["deal_badge"]
        print(f"  💵 Prezzo rilevato: {curr_price:.2f} € (-{discount_pct:.0f}%) | Badge: '{deal_badge or 'Nessuno'}'")

        prev_lowest = storage.get_lowest_price(asin)
        if not dry_run:
            storage.record_price(asin, curr_price, discount_pct, deal_badge, result["is_available"])

        for sub in subscribers:
            chat_id = sub["chat_id"]
            target_price = float(sub["target_price"])

            should_alert, reason = storage.should_send_user_alert(
                chat_id=chat_id,
                asin=asin,
                current_price=curr_price,
                target_price=target_price,
                cooldown_hours=cooldown_hours,
                notify_on_all_time_low=notify_on_all_time_low,
            )
            if not should_alert:
                print(f"  ℹ️  utente {chat_id}: nessun alert ({reason or f'sopra target {target_price:.2f} €'})")
                continue

            alerts_triggered += 1
            alert_msg = format_telegram_alert(
                product_name=name,
                form_factor=form_factor,
                capacity=capacity,
                current_price=curr_price,
                target_price=target_price,
                previous_low=prev_lowest,
                discount_pct=discount_pct,
                deal_badge=deal_badge,
                asin=asin,
                reason=reason,
            )
            if dry_run:
                print(f"  [DryRun] alert per utente {chat_id} (target {target_price:.2f} €): {reason}")
                continue
            if send_via_telegram_bot(token, chat_id, alert_msg):
                storage.record_user_alert(chat_id, asin, curr_price, reason)
                print(f"  🔔 alert inviato a {chat_id}: {reason}")
            else:
                print(f"  ❌ invio a {chat_id} fallito")

        if idx < len(asins):
            time.sleep(delay)

    print(f"\n🏁 Scansione completata. Alert generati: {alerts_triggered}")


def main():
    parser = argparse.ArgumentParser(description="SSD M.2 2280 Black Friday Price Drop Poller")
    parser.add_argument("--check", action="store_true", help="Esegue una sessione di polling completa")
    parser.add_argument("--dry-run", action="store_true", help="Scansiona i prezzi senza salvare o inviare notifiche")
    parser.add_argument("--test-alert", action="store_true", help="Invia un messaggio di prova su Telegram")
    parser.add_argument("--list", action="store_true", help="Mostra i prodotti monitorati e i prezzi correnti")
    parser.add_argument("--users", action="store_true", help="Mostra gli utenti registrati e le loro statistiche")
    parser.add_argument("--add", metavar="ASIN_OR_URL", help="Aggiunge un nuovo SSD tramite ASIN o URL Amazon")
    parser.add_argument("--target", type=float, default=0.0, help="Prezzo target (€) per il nuovo prodotto aggiunto")
    parser.add_argument("--name", default="", help="Nome opzionale per il nuovo prodotto aggiunto")
    args = parser.parse_args()

    config = load_config()
    storage = Storage()

    if args.add:
        add_product(config, args.add, target_price=args.target, name_override=args.name)
    elif args.users:
        list_users(storage)
    elif args.test_alert:
        run_test_alert(config)
    elif args.list:
        list_products(config, storage)
    else:
        # Azione predefinita: scansione (con o senza dry-run)
        check_all_products(config, storage, dry_run=args.dry_run)

if __name__ == "__main__":
    main()
