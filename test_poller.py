import unittest
import os
import tempfile
from storage import Storage
from fetchers.amazon_scraper import parse_price_str
from bot_notifier import format_telegram_alert

class TestSSDPoller(unittest.TestCase):
    def setUp(self):
        self.tmp_fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(self.tmp_fd)
        self.storage = Storage(db_path=self.tmp_path)

    def tearDown(self):
        if os.path.exists(self.tmp_path):
            os.remove(self.tmp_path)

    def test_parse_price_str(self):
        self.assertEqual(parse_price_str("139,99 €"), 139.99)
        self.assertEqual(parse_price_str(" 89,00 € "), 89.00)
        self.assertEqual(parse_price_str("1.250,50&euro;"), 1250.50)
        self.assertEqual(parse_price_str("99.90"), 99.90)
        self.assertIsNone(parse_price_str(""))
        self.assertIsNone(parse_price_str("Non disponibile"))

    def test_storage_and_cooldown(self):
        asin = "B0B9C4DKKG"
        # Inizialmente nessun prezzo
        self.assertIsNone(self.storage.get_lowest_price(asin))

        # Registriamo un primo prezzo a 160€
        self.storage.record_price(asin, 160.00)
        self.assertEqual(self.storage.get_lowest_price(asin), 160.00)

        # Target 149€: a 160€ non deve inviare alert
        should_alert, _ = self.storage.should_send_alert(asin, 160.00, target_price=149.00, cooldown_hours=6)
        self.assertFalse(should_alert)

        # Calo a 140€ (sotto target e nuovo minimo): deve allertare
        should_alert, reason = self.storage.should_send_alert(asin, 140.00, target_price=149.00, cooldown_hours=6)
        self.assertTrue(should_alert)
        self.assertTrue("minimo" in reason.lower() or "target" in reason.lower())

        # Registriamo l'alert inviato
        self.storage.record_alert(asin, 140.00, reason)
        self.storage.record_price(asin, 140.00)

        # Stesso prezzo 140€ dopo 5 minuti: cooldown attivo, non deve allertare
        should_alert, reason = self.storage.should_send_alert(asin, 140.00, target_price=149.00, cooldown_hours=6)
        self.assertFalse(should_alert)
        self.assertIn("cooldown", reason.lower())

        # Ulteriore calo a 130€: deve superare il cooldown perché è un ulteriore calo!
        should_alert, reason = self.storage.should_send_alert(asin, 130.00, target_price=149.00, cooldown_hours=6)
        self.assertTrue(should_alert)
        self.assertIn("ulteriore calo", reason.lower())

    def test_multi_user_subscriptions(self):
        user_a = 111111
        user_b = 222222
        asin = "B0CK39YR9V"

        self.storage.upsert_product(asin, "Crucial T500 2TB", "2TB", "2280")
        self.storage.subscribe_user(user_a, "alice", asin, target_price=130.00)
        self.storage.subscribe_user(user_b, "bob", asin, target_price=110.00)

        subs_a = self.storage.get_user_subscriptions(user_a)
        subs_b = self.storage.get_user_subscriptions(user_b)
        self.assertEqual(len(subs_a), 1)
        self.assertEqual(len(subs_b), 1)
        self.assertEqual(subs_a[0]["target_price"], 130.00)
        self.assertEqual(subs_b[0]["target_price"], 110.00)

        # Prezzo scende a 125€: Alice (target 130) deve ricevere alert, Bob (target 110) NO
        self.storage.record_price(asin, 125.00)
        alert_a, _ = self.storage.should_send_user_alert(user_a, asin, 125.00, target_price=130.00)
        alert_b, _ = self.storage.should_send_user_alert(user_b, asin, 125.00, target_price=110.00)
        self.assertTrue(alert_a)
        self.assertFalse(alert_b)

        # Alice riceve alert, Bob ancora no
        self.storage.record_user_alert(user_a, asin, 125.00, "Sotto target")

        # Se il prezzo scende a 105€: ORA anche Bob riceve l'alert!
        self.storage.record_price(asin, 105.00)
        alert_b, _ = self.storage.should_send_user_alert(user_b, asin, 105.00, target_price=110.00)
        self.assertTrue(alert_b)

        # Unsubscribe Bob
        removed = self.storage.unsubscribe_user(user_b, asin)
        self.assertTrue(removed)
        self.assertEqual(len(self.storage.get_user_subscriptions(user_b)), 0)
        self.assertEqual(len(self.storage.get_user_subscriptions(user_a)), 1)
        msg = format_telegram_alert(
            product_name="Samsung 990 PRO 2TB",
            form_factor="2280",
            capacity="2TB",
            current_price=139.99,
            target_price=149.00,
            previous_low=145.00,
            discount_pct=15.0,
            deal_badge="Offerta Black Friday",
            asin="B0B9C4DKKG",
            reason="Prezzo sotto target"
        )
        self.assertIn("BLACK FRIDAY", msg)
        self.assertIn("Samsung 990 PRO 2TB", msg)
        self.assertIn("139.99 €", msg)
        self.assertIn("B0B9C4DKKG", msg)

if __name__ == "__main__":
    unittest.main()
