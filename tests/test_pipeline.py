"""Run: python -m unittest discover -s tests -v   (stdlib only; uses a throwaway database)."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import lib  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", "").lstrip("0123456789_"), ROOT / "pipeline" / name)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class Pure(unittest.TestCase):
    def test_offer_rule(self):
        self.assertTrue(lib.is_free_offer("Book your free consultation today"))
        self.assertTrue(lib.is_free_offer("First class is complimentary"))
        self.assertTrue(lib.is_free_offer("Free 1-week trial"))
        self.assertFalse(lib.is_free_offer("Free shipping on all orders"))
        self.assertFalse(lib.is_free_offer("Free parking and delivery"))
        self.assertFalse(lib.is_free_offer("20% off your first class"))
        self.assertFalse(lib.is_free_offer(None))

    def test_mailbox_choice(self):
        seed = lib.load_yaml("suppression_seed.yaml")
        e, why, person = lib.choose_mailbox(["info@x.ca", "careers@x.ca", "jane@x.ca"], "x.ca", seed, "Jane")
        self.assertEqual((e, person), ("jane@x.ca", True))
        e, _, person = lib.choose_mailbox(["careers@x.ca", "hr@x.ca"], "x.ca", seed)
        self.assertIsNone(e)
        e, _, person = lib.choose_mailbox(["info@x.ca", "bob.smith@gmail.com"], "x.ca", seed)
        self.assertEqual(e, "bob.smith@gmail.com")  # named free-mail beats generic info@
        e, _, person = lib.choose_mailbox(["info@x.ca"], "x.ca", seed)
        self.assertEqual((e, person), ("info@x.ca", False))

    def test_normalisers(self):
        self.assertEqual(lib.norm_domain("https://www.Foo.ca/path"), "foo.ca")
        self.assertEqual(lib.norm_phone("+1 (604) 555-1212"), "6045551212")
        self.assertEqual(lib.norm_name("The Gallery at Evergreen Arts, Inc."), "gallery at evergreen arts")
        self.assertEqual(lib.first_postal_v("Burnaby BC V5H 2A1"), "V5H 2A1")
        self.assertIsNone(lib.first_postal_v("Toronto M5V 2T6"))
        self.assertTrue(lib.quote_in_text("Free  trial class", "Come for a free trial   class today"))
        self.assertFalse(lib.quote_in_text("free trial class", "nothing here"))

    def test_classify_replies(self):
        r = load("10_reply.py")
        self.assertEqual(r.classify("Please unsubscribe me", False), "stop")
        self.assertEqual(r.classify("Not interested, thanks", False), "not_interested")
        self.assertEqual(r.classify("Yes, send me the details", False), "yes")
        self.assertEqual(r.classify("Is it really free?", False), "free")
        self.assertEqual(r.classify("How does it work?", False), "how_it_works")
        self.assertEqual(r.classify("Thanks, we signed up but can't find the listing", False), "signed_up_cannot_find")
        self.assertEqual(r.classify("Out of office until Monday", True), "auto")
        self.assertEqual(r.classify("Who is this? My lawyer will call you", False), "other")

    def test_load_helpers(self):
        l = load("08_load.py")
        self.assertEqual(l.clean_name("Acme &amp; Sons Ltd."), "Acme & Sons")
        self.assertEqual(l.clean_name("Yoga Place (Burnaby) Inc"), "Yoga Place")
        self.assertEqual(l.offer_phrase("Book a free trial class today"), "free trial class")

    def test_tiers(self):
        s = load("04_score.py")
        t = lib.load_yaml("scoring.yaml")["tiers"]
        self.assertEqual([s.tier_for(x, t) for x in (95, 70, 69, 45, 44)], ["A", "A", "B", "B", "X"])


class Flow(unittest.TestCase):
    """Synthetic business through resolve -> extraction -> score -> check -> load -> match."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = lib.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def make(self, name="Calm Counselling", domain="calm.ca", city="Burnaby"):
        c = self.conn
        res = load("02_resolve.py")
        bid = res.resolve_row(c, 0, {"name": name, "domain": domain, "website": f"https://{domain}", "city": city, "phone": "6045551212"}, "test")
        text = ("Calm Counselling offers individual therapy in Burnaby BC V5H 2A1. Jane Doe, registered counsellor and owner. "
                "Book a free consultation to see if we are a fit. Email jane@calm.ca")
        c.execute("INSERT INTO pages(business_id, url, kind, http_status, text) VALUES (?,?,?,?,?)", (bid, f"https://{domain}", "home", 200, text))
        c.execute("INSERT INTO contacts(business_id, email, source_url) VALUES (?,?,?)", (bid, "jane@calm.ca", "u"))
        c.execute("INSERT INTO contacts(business_id, email, source_url) VALUES (?,?,?)", (bid, "careers@calm.ca", "u"))
        c.commit()
        return bid

    def extract(self, bid, **over):
        data = {"segment": "counselling_therapy", "segment_quote": "individual therapy in Burnaby",
                "offer": {"sentence": "Book a free consultation to see if we are a fit", "url": "https://calm.ca", "type": "consultation"},
                "owner": {"first_name": "Jane", "role": "owner", "quote": "Jane Doe, registered counsellor and owner", "url": "https://calm.ca"},
                "accepting_clients": "true", "location_count": 1, "franchise_wording": False,
                "listing_title": "Free consultation", "listing_description": "Individual therapy in Burnaby."}
        data.update(over)
        return load("03_enrich.py").apply_extraction(self.conn, bid, data)

    def test_resolve_dedupes_on_domain(self):
        a = self.make()
        res = load("02_resolve.py")
        b = res.resolve_row(self.conn, 1, {"name": "Calm Counselling Inc", "domain": "calm.ca", "city": "Burnaby"}, "test")
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM merges").fetchone()[0], 1)

    def test_fabricated_quotes_are_dropped(self):
        bid = self.make()
        self.extract(bid, offer={"sentence": "Free consultation for everyone forever", "url": "x", "type": "consultation"},
                     owner={"first_name": "Zed", "role": "owner", "quote": "Zed is the owner", "url": "x"})
        fields = {r["field"] for r in self.conn.execute("SELECT field FROM evidence WHERE business_id=?", (bid,))}
        self.assertNotIn("offer_sentence", fields)
        self.assertNotIn("owner_first_name", fields)
        self.assertIn("segment", fields)

    def test_full_path_to_load_and_match(self):
        c = self.conn
        bid = self.make()
        out = self.extract(bid)
        self.assertEqual(out["email"], "jane@calm.ca")
        sc = load("04_score.py")
        cfg, segs = lib.load_yaml("scoring.yaml"), lib.segment_catalog()
        b = c.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
        score, sig = sc.compute(c, b, cfg, segs, lib.city_set())
        self.assertEqual(score, 25 + 20 + 15 + 10 + 10 + 10)  # no booking link, no licence
        c.execute("INSERT INTO scores VALUES (?,?,?,?,?,?)", (bid, score, "A", json.dumps(sig), "t", lib.now()))
        c.execute("UPDATE contacts SET verification='valid', verified_at=? WHERE business_id=? AND is_chosen=1", (lib.now(), bid))
        lib.set_status(c, bid, "verified")
        c.execute("INSERT INTO llm_tasks(kind, business_id, prompt, prompt_hash, response, status) VALUES ('segment_check',?,?,?,?, 'done')",
                  (bid, "p", "h", '{"segment": "counselling_therapy"}'))
        chk = load("07_check.py")
        fails = chk.evaluate(c, c.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone(), lib.city_set(), 14, None, True)
        self.assertEqual(fails, [])
        c.execute("INSERT INTO checks(business_id, passed, failures) VALUES (?,1,'[]')", (bid,))
        c.commit()
        ld = load("08_load.py")
        rows = ld.select(c, "counselling_therapy", "A", 10)
        v = ld.lead_vars(c, rows[0], lib.load_yaml("peers.yaml"), segs)
        self.assertEqual(v["email"], "jane@calm.ca")
        self.assertIn("?r=", v["custom_variables"]["Signup_Link"])
        self.assertEqual(v["custom_variables"]["First_Name"], "Jane")
        # simulate a send, then a live-list match inside the 21-day window
        c.execute("INSERT INTO batches(name, segment, tier, n_loaded) VALUES ('b','counselling','A',1)")
        c.execute("INSERT INTO batch_leads(batch_id, business_id, email, pushed) VALUES (1,?,?,1)", (bid, "jane@calm.ca"))
        c.execute("INSERT INTO sends(business_id, batch_id, email, step, sent_at) VALUES (?,1,'jane@calm.ca',1,'2026-11-01 10:00:00')", (bid,))
        c.commit()
        m = load("11_match.py")
        m.store(c, [{"name": "Calm Counselling Ltd", "email": "jane@calm.ca", "website": "", "phone": "", "date": "2026-11-05", "city": ""},
                    {"name": "Early Bird", "email": "e@other.ca", "website": "", "phone": "", "date": "", "city": ""}])
        stats = m.match_all(c)
        self.assertEqual(stats["in_window"], 1)
        self.assertEqual(stats["other_source"], 1)
        self.assertEqual(c.execute("SELECT status FROM businesses WHERE id=?", (bid,)).fetchone()[0], "onboarded")

    def test_window_rejects_signup_before_email_one(self):
        bid = self.make()
        c = self.conn
        c.execute("INSERT INTO sends(business_id, email, step, sent_at) VALUES (?,?,1,'2026-11-10 10:00:00')", (bid, "jane@calm.ca"))
        m = load("11_match.py")
        self.assertEqual(m.window(c, bid, "2026-11-01")[0], 0)   # already on Tryuu
        self.assertEqual(m.window(c, bid, "2026-12-15")[0], 0)   # outside 21 days
        self.assertEqual(m.window(c, bid, "2026-11-20")[0], 1)
        self.assertIsNone(m.window(c, bid, None)[0])

    def test_placeholder_signup_email_skips_email_rules(self):
        self.make()
        m = load("11_match.py")
        m.store(self.conn, [{"name": "Totally Different", "email": "testcompany+5@tryuu.com", "website": "", "phone": "", "date": "", "city": ""}])
        row = self.conn.execute("SELECT * FROM onboarded").fetchone()
        self.assertEqual(row["is_test_email"], 1)
        self.assertEqual(m.find_business(self.conn, row), (None, None))

    def test_suppression(self):
        bid = self.make()
        c = self.conn
        self.assertEqual(lib.suppression_reasons(c, bid), [])
        c.execute("INSERT INTO suppression(kind, value, reason) VALUES ('domain','calm.ca','unsubscribed')")
        self.assertTrue(lib.suppression_reasons(c, bid))
        c.execute("DELETE FROM suppression")
        c.execute("INSERT INTO sends(business_id, email, step, sent_at) VALUES (?,?,1,?)", (bid, "jane@calm.ca", lib.now()))
        self.assertIn("emailed within 60 days", lib.suppression_reasons(c, bid))
        c.execute("UPDATE businesses SET name='Cypress Aesthetics' WHERE id=?", (bid,))
        self.assertTrue(any("cypress" in r for r in lib.suppression_reasons(c, bid)))

    def test_precision_gate_blocks_load(self):
        c = self.conn
        bid = self.make()
        self.extract(bid)
        c.execute("INSERT INTO scores VALUES (?,90,'A','{}','t',?)", (bid, lib.now()))
        c.execute("INSERT INTO checks(business_id, passed, failures) VALUES (?,0,'[]')", (bid,))
        ld = load("08_load.py")
        p, n = ld.precision(c, "counselling_therapy", "A")
        self.assertEqual((p, n), (0.0, 1))


if __name__ == "__main__":
    unittest.main()
