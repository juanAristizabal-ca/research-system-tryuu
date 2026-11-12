"""Agent-in-session mode: LLM worksheets, connector-page ingestion, verification levels, backup/restore."""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import lib  # noqa: E402


def load(name, path=None):
    spec = importlib.util.spec_from_file_location(name.replace(".py", "").lstrip("0123456789_"), path or ROOT / "pipeline" / name)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class AgentMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = lib.connect(self.dir / "t.db")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def biz(self, name="Calm Counselling", domain="calm.ca", email="jane@calm.ca", city="Burnaby"):
        c = self.conn
        bid = c.execute("INSERT INTO businesses(key, domain, name, city, website) VALUES (?,?,?,?,?)",
                        (f"d:{domain}", domain, name, city, f"https://{domain}")).lastrowid
        c.execute("INSERT INTO contacts(business_id, email, is_chosen) VALUES (?,?,1)", (bid, email))
        c.commit()
        return bid

    def test_dedupe_pages_drops_repeated_navigation(self):
        nav = "Home About Contact Services Pricing Book Now Facebook Instagram Privacy Policy Terms"
        a, b = lib.dedupe_pages([f"Welcome to Calm. {nav}", f"We offer free consultation calls. {nav}"])
        self.assertIn("Welcome", a)
        self.assertIn("free consultation", b)
        self.assertNotIn("Privacy Policy", b)

    def test_worksheet_roundtrip(self):
        llm = load("llm.py")
        bid = self.biz()
        lib.enqueue_llm(self.conn, "extract", bid, "Business: Calm\n[[URL: u]]\ntext")
        self.conn.commit()
        n = llm.export(self.conn, self.dir / "w.md", "extract", None)
        self.assertEqual(n, 1)
        sheet = (self.dir / "w.md").read_text()
        self.assertIn("### TASK id=1", sheet)
        self.assertIn("verbatim quote", sheet)  # instructions present once
        ans = self.dir / "a.jsonl"
        ans.write_text(json.dumps({"id": 1, "response": {"segment": "counselling_therapy"}}) + "\n" + json.dumps({"id": 2, "response": "not json"}) + "\n")
        self.conn.execute("INSERT INTO llm_tasks(kind, business_id, prompt, prompt_hash) VALUES ('extract', ?, 'x', 'h2')", (bid,))
        ok, bad = llm.import_answers(self.conn, ans)
        self.assertEqual((ok, bad), (1, 1))
        row = self.conn.execute("SELECT status, prompt FROM llm_tasks WHERE id=1").fetchone()
        self.assertEqual((row["status"], row["prompt"]), ("done", ""))  # prompt blanked to keep the db small
        self.assertEqual(lib.get_llm_response(self.conn, "extract", bid), {"segment": "counselling_therapy"})

    def test_extract_screen_skips_unloadable_leads(self):
        enrich = load("03_enrich.py")
        seed = lib.load_yaml("suppression_seed.yaml")
        bid = self.biz(email="careers@calm.ca")
        b = self.conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
        self.assertEqual(enrich.skip_reason(self.conn, b, seed, lib.city_set()), "no usable mailbox on site")
        bid2 = self.biz(name="Toronto Co", domain="to.ca", email="a@to.ca", city="Toronto")
        b2 = self.conn.execute("SELECT * FROM businesses WHERE id=?", (bid2,)).fetchone()
        self.assertEqual(enrich.skip_reason(self.conn, b2, seed, lib.city_set()), "outside launch cities")

    def test_verification_levels(self):
        v = load("05_verify.py")
        bid = self.biz()
        c = self.conn
        c.execute("UPDATE businesses SET status='scored' WHERE id=?", (bid,))
        c.execute("INSERT INTO scores VALUES (?,90,'A','{}','t',?)", (bid, lib.now()))
        c.execute("UPDATE contacts SET verification='mx_only' WHERE business_id=?", (bid,))
        c.commit()
        real = lib.load_yaml
        def strict(name):
            d = real(name)
            if name == "sources.yaml":
                d["verify"]["min_level"] = "valid"
            return d
        lib.load_yaml = strict
        try:
            self.assertEqual(v.settle(c), (0, 0))   # strict level: a DNS check alone does not load
        finally:
            lib.load_yaml = real
        self.assertEqual(c.execute("SELECT status FROM businesses").fetchone()[0], "scored")
        self.assertEqual(v.settle(c), (1, 0))        # configured level (mx_only, owner decision) loads it
        c.execute("UPDATE businesses SET status='scored'")
        c.commit()
        res = self.dir / "r.json"
        res.write_text(json.dumps([{"email": "jane@calm.ca", "verification_status": "verified", "catch_all": True}]))
        class A: file = str(res)
        v.cmd_ingest(c, A)
        self.assertEqual(c.execute("SELECT status FROM businesses").fetchone()[0], "verified")
        self.assertEqual(c.execute("SELECT tier FROM scores").fetchone()[0], "B")  # catch-all caps at B

    def test_sync_ingests_connector_pages(self):
        sync = load("09_sync.py")
        bid = self.biz()
        c = self.conn
        c.execute("UPDATE businesses SET status='loaded' WHERE id=?", (bid,))
        c.execute("INSERT INTO batches(name, instantly_campaign_id, n_loaded) VALUES ('b','camp1',1)")
        c.commit()
        emails = [  # shapes copied from real list_emails output
            {"id": "e1", "ue_type": 1, "lead": "jane@calm.ca", "step": "0_0_0", "campaign_id": "camp1", "timestamp_email": "2026-11-11T19:08:33.000Z"},
            {"id": "e2", "ue_type": 2, "from_address_email": "jane@calm.ca", "subject": "Re: x", "body": {"text": "Is it really free?"},
             "eaccount": "a@b.com", "campaign_id": "camp1", "timestamp_email": "2026-11-12T10:00:00.000Z"},
            {"id": "e3", "ue_type": 2, "from_address_email": "jane@calm.ca", "subject": "Automatic reply: out of office", "body": {"text": "I am away"}},
        ]
        self.assertEqual(sync.ingest_emails(c, emails), (1, 2))
        self.assertEqual(sync.ingest_emails(c, emails), (0, 0))  # idempotent
        self.assertEqual(c.execute("SELECT is_auto FROM replies ORDER BY id").fetchall()[1][0], 1)
        sync.update_statuses(c)
        self.assertEqual(c.execute("SELECT status FROM businesses").fetchone()[0], "replied")
        self.assertEqual(sync.ingest_leads(c, [{"email": "x@y.ca", "status": -1, "campaign": "camp1"}, {"email": "z@w.ca", "status": -2}]), 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM suppression WHERE value='z@w.ca'").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT step FROM sends WHERE instantly_email_id='e1'").fetchone()[0], 1)

    def test_backup_restore_roundtrip_without_pages(self):
        run = load("run.py", ROOT / "run.py")
        bid = self.biz()
        self.conn.execute("INSERT INTO pages(business_id, url, kind, http_status, text) VALUES (?,?,?,?,?)", (bid, "u", "home", 200, "big text"))
        self.conn.commit()
        run.lib.DB_PATH = self.dir / "t.db"
        run.DUMP = self.dir / "dump" / "tryuu.sql"
        orig = lib.connect
        lib.connect = lambda path=None: orig(self.dir / "t.db")
        try:
            run.backup()
            self.assertNotIn("big text", run.DUMP.read_text())
            run.lib.DB_PATH = self.dir / "restored.db"
            run.restore()
        finally:
            lib.connect = orig
        r = sqlite3.connect(self.dir / "restored.db")
        self.assertEqual(r.execute("SELECT name FROM businesses").fetchone()[0], "Calm Counselling")
        self.assertEqual(r.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 0)

    def test_reply_queue_only_with_approved_templates(self):
        reply = load("10_reply.py")
        self.assertIsNone(reply.load_template("yes"))  # shipped as approved: false


if __name__ == "__main__":
    unittest.main()
