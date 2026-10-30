import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict, Any

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "ssd_prices.db")

class Storage:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Prodotti conosciuti
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    asin TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    capacity TEXT DEFAULT 'N/D',
                    form_factor TEXT DEFAULT '2280'
                )
            """)

            # Sottoscrizioni per singolo utente (multi-tenant)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    asin TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, asin)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_subs_chat ON user_subscriptions(chat_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_subs_asin ON user_subscriptions(asin)
            """)

            # Storico prezzi globale
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    price REAL NOT NULL,
                    discount_pct REAL DEFAULT 0,
                    deal_badge TEXT,
                    is_available BOOLEAN DEFAULT 1
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_price_history_asin ON price_history(asin, timestamp)
            """)

            # Storico alert inviati al singolo utente (per cooldown indipendente per ciascun utente)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    asin TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    price REAL NOT NULL,
                    alert_reason TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_alerts ON user_alerts(chat_id, asin, timestamp)
            """)

            conn.commit()

    def upsert_product(self, asin: str, name: str, capacity: str = "N/D", form_factor: str = "2280"):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO products (asin, name, capacity, form_factor)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    name = excluded.name,
                    capacity = CASE WHEN excluded.capacity != 'N/D' THEN excluded.capacity ELSE products.capacity END,
                    form_factor = excluded.form_factor
            """, (asin, name, capacity, form_factor))
            conn.commit()

    def get_product(self, asin: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products WHERE asin = ?", (asin,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def subscribe_user(self, chat_id: int, username: str, asin: str, target_price: float):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_subscriptions (chat_id, username, asin, target_price)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, asin) DO UPDATE SET
                    target_price = excluded.target_price,
                    username = excluded.username
            """, (chat_id, username or "", asin, target_price))
            conn.commit()

    def unsubscribe_user(self, chat_id: int, asin: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM user_subscriptions
                WHERE chat_id = ? AND asin = ?
            """, (chat_id, asin))
            conn.commit()
            return cursor.rowcount > 0

    def get_user_subscriptions(self, chat_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    us.asin,
                    us.target_price,
                    COALESCE(p.name, us.asin) as name,
                    COALESCE(p.capacity, '2TB') as capacity,
                    COALESCE(p.form_factor, '2280') as form_factor,
                    (SELECT price FROM price_history ph WHERE ph.asin = us.asin ORDER BY timestamp DESC LIMIT 1) as latest_price,
                    (SELECT MIN(price) FROM price_history ph2 WHERE ph2.asin = us.asin AND ph2.price > 0 AND ph2.is_available = 1) as min_price
                FROM user_subscriptions us
                LEFT JOIN products p ON us.asin = p.asin
                WHERE us.chat_id = ?
                ORDER BY us.created_at ASC
            """, (chat_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_all_active_asins(self) -> List[str]:
        """Restituisce tutti gli ASIN attualmente monitorati da almeno un utente."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT asin FROM user_subscriptions")
            return [row["asin"] for row in cursor.fetchall()]

    def get_subscribers_for_asin(self, asin: str) -> List[Dict[str, Any]]:
        """Restituisce tutti gli utenti iscritti a un determinato ASIN."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT chat_id, username, target_price
                FROM user_subscriptions
                WHERE asin = ?
            """, (asin,))
            return [dict(row) for row in cursor.fetchall()]

    def record_price(self, asin: str, price: float, discount_pct: float = 0.0, deal_badge: str = "", is_available: bool = True) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO price_history (asin, price, discount_pct, deal_badge, is_available)
                VALUES (?, ?, ?, ?, ?)
            """, (asin, price, discount_pct, deal_badge, 1 if is_available else 0))
            conn.commit()
            return cursor.lastrowid

    def get_lowest_price(self, asin: str) -> Optional[float]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT MIN(price) as min_price
                FROM price_history
                WHERE asin = ? AND price > 0 AND is_available = 1
            """, (asin,))
            row = cursor.fetchone()
            if row and row["min_price"] is not None:
                return float(row["min_price"])
            return None

    def get_latest_price(self, asin: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT price, discount_pct, deal_badge, timestamp
                FROM price_history
                WHERE asin = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (asin,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def should_send_user_alert(self, chat_id: int, asin: str, current_price: float, target_price: float, cooldown_hours: int = 6, notify_on_all_time_low: bool = True) -> Tuple[bool, str]:
        """
        Determina se inviare un alert A QUESTO SPECIFICO UTENTE:
        - Sotto il prezzo target impostato dall'utente
        - Oppure nuovo minimo storico assoluto
        - Rispetta il cooldown personale dell'utente per questo ASIN
        """
        if current_price <= 0:
            return False, ""

        lowest = self.get_lowest_price(asin)
        is_all_time_low = (lowest is not None and current_price < lowest)
        is_below_target = current_price <= target_price

        if not (is_below_target or (notify_on_all_time_low and is_all_time_low)):
            return False, ""

        if is_all_time_low and lowest is not None:
            diff_low = lowest - current_price
            reason = f"Nuovo Minimo Storico ({current_price:.2f} € vs precedente {lowest:.2f} €, -{diff_low:.2f} €)"
        elif is_below_target:
            diff = target_price - current_price
            reason = f"Prezzo sotto il tuo target ({current_price:.2f} € <= {target_price:.2f} €, -{diff:.2f} €)"
        else:
            reason = f"Offerta rilevata: {current_price:.2f} €"

        # Controlla l'ultimo alert inviato a questo specifico utente
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT price, timestamp
                FROM user_alerts
                WHERE chat_id = ? AND asin = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (chat_id, asin))
            last_alert = cursor.fetchone()

            if last_alert:
                last_time = datetime.fromisoformat(last_alert["timestamp"])
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                hours_passed = (datetime.now(timezone.utc) - last_time).total_seconds() / 3600.0
                last_price = float(last_alert["price"])

                # Se c'è stato un ulteriore calo rispetto all'ultima notifica a questo utente, allerta subito!
                if current_price < (last_price - 1.0):
                    return True, f"{reason} (Ulteriore calo rispetto all'ultimo avviso)"

                if hours_passed < cooldown_hours:
                    return False, f"In cooldown per questo utente ({hours_passed:.1f}h fa a {last_price:.2f} €)"

        return True, reason

    def should_send_alert(self, asin: str, current_price: float, target_price: float, cooldown_hours: int = 6, notify_on_all_time_low: bool = True) -> Tuple[bool, str]:
        """Metodo di compatibilità per uso standalone (chat_id = 0)."""
        return self.should_send_user_alert(0, asin, current_price, target_price, cooldown_hours, notify_on_all_time_low)

    def record_alert(self, asin: str, price: float, alert_reason: str):
        """Metodo di compatibilità per uso standalone (chat_id = 0)."""
        self.record_user_alert(0, asin, price, alert_reason)

    def record_user_alert(self, chat_id: int, asin: str, price: float, alert_reason: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_alerts (chat_id, asin, price, alert_reason)
                VALUES (?, ?, ?, ?)
            """, (chat_id, asin, price, alert_reason))
            conn.commit()
