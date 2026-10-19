"""Run with:  python -m unittest discover -s tests -v
Uses a temporary database and a local mock web server (the SSRF guard is relaxed ONLY inside this test)."""
import json, os, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BACKLINK_DB"] = tempfile.mktemp(suffix=".db")
os.environ["BACKLINK_CONFIG"] = tempfile.mktemp(suffix=".json")
from modules import checker, crawler, db, exporter, finder, jobs, outreach, quality, safety, settings, submitter

SUBMITS = []
PAGES = {
    "/robots.txt": "User-agent: *\nDisallow: /private/\n",
    "/dir": '<html><title>Python tools directory</title><body><h1>Python developer tools</h1><p>' + "tools " * 150 +
            '</p><a href="/submit">Submit your site</a><a href="https://other.org/x" rel="nofollow">x</a></body></html>',
    "/submit": '<form action="/add" method="post"><input type=hidden name=csrf value=tok><input name=url type=url required>'
               '<input name=title required><textarea name=description></textarea><input name=hp_field style="display:none"></form>',
    "/captcha": '<div class="g-recaptcha"></div><form action="/add"><input name=url type=url><input name=title></form>',
    "/listing_live": '<a href="https://mysite.com/page" rel="nofollow">My Site</a>',
    "/listing_clean": '<a href="https://www.mysite.com/">Great tools</a>',
    "/listing_ugc": '<a href="https://mysite.com/" rel="ugc nofollow">c</a>',
    "/listing_none": '<p>' + "nothing " * 80 + '</p>',
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="text/html", extra=None):
        self.send_response(code); self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body.encode())
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/redir": return self._send(302, "", extra={"Location": "/listing_clean"})
        if p == "/gone": return self._send(404, "nope")
        if p == "/flaky":
            H.flaky = getattr(H, "flaky", 0) + 1
            return self._send(503 if H.flaky < 2 else 200, '<a href="https://mysite.com/">ok</a>')
        if p in PAGES: return self._send(200, PAGES[p], "text/plain" if p == "/robots.txt" else "text/html")
        self._send(404, "nf")
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); data = parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        SUBMITS.append(data); self._send(200, "<p>Thank you! Your site has been submitted and is awaiting review.</p>")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), H); cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls._orig = (safety.is_ip_blocked, safety.ALLOWED_PORTS)
        safety.is_ip_blocked = lambda ip: False
        safety.ALLOWED_PORTS = safety.ALLOWED_PORTS + (cls.port,)
        db.init_db()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.cfg = dict(settings.load(), per_domain_delay=0, retry_delay=0, retry_limit=2)
        db.execute("INSERT OR IGNORE INTO targets (url,domain,name,description,category,keywords,contact_email,created_at,updated_at) "
                             "VALUES ('https://mysite.com/','mysite.com','My Site','Python developer tools','software',"
                             "'python, tools','me@mysite.com',?,?)", (db.now(), db.now()))
        cls.tid = db.query("SELECT id FROM targets WHERE url='https://mysite.com/'", one=True)["id"]
        cls.target = db.query("SELECT * FROM targets WHERE id=?", (cls.tid,), one=True)
    @classmethod
    def tearDownClass(cls):
        safety.is_ip_blocked, safety.ALLOWED_PORTS = cls._orig; cls.srv.shutdown()
    def fetcher(self): return crawler.Fetcher(cfg=self.cfg)


class TestSafety(unittest.TestCase):
    def test_blocked(self):
        for u in ["http://localhost/", "http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.0.1/", "http://169.254.169.254/",
                  "http://[::1]/", "http://metadata.google.internal/", "ftp://example.com/", "https://a:b@example.com/",
                  "https://example.com:22/", "javascript:alert(1)", "http://foo.local/", "http://2130706433/"]:
            with self.assertRaises(safety.UnsafeURL, msg=u): safety.validate_url(u, resolve=False)
    def test_ok_and_normalize(self):
        self.assertEqual(safety.validate_url("https://example.com", resolve=False), "https://example.com/")
        self.assertEqual(safety.normalize_url("HTTPS://WWW.Ex.com:443/a/?utm_source=x&b=1#f"), "https://www.ex.com/a?b=1")
        self.assertEqual(safety.get_domain("https://www.Ex.com/x"), "ex.com")
        self.assertTrue(safety.valid_email("a@b.co")); self.assertFalse(safety.valid_email("nope"))


class TestDatabase(Base):
    def test_idempotent_init_and_dupes(self):
        db.init_db(); db.init_db()
        a = finder.add_opportunity(self.tid, "https://dir.org/a/?utm_x=1", "directory")
        b = finder.add_opportunity(self.tid, "https://dir.org/a", "directory")
        self.assertTrue(a[1]); self.assertFalse(b[1]); self.assertEqual(a[0], b[0])
        with self.assertRaises(safety.UnsafeURL): finder.add_opportunity(self.tid, "https://mysite.com/x", "directory")
        with self.assertRaises(Exception): db.execute("UPDATE opportunities SET status='bogus' WHERE id=?", (a[0],))
    def test_import(self):
        st = finder.import_urls(self.tid, "x.csv", b"url,category\nhttps://imp.org/1,guest_post\nhttps://imp.org/1,guest_post\nhttp://localhost/x,\n", "directory")
        self.assertEqual((st["added"], st["duplicates"], len(st["invalid"])), (1, 1, 1))
    def test_job_unique(self):
        ts = db.now()
        db.execute("INSERT INTO jobs (target_id,kind,status,created_at,updated_at) VALUES (?, 'verify','paused',?,?)", (self.tid, ts, ts))
        with self.assertRaises(jobs.JobConflict): jobs.start("verify", self.tid)
        db.execute("UPDATE jobs SET status='cancelled' WHERE target_id=? AND kind='verify'", (self.tid,))


class TestPipeline(Base):
    def test_quality_and_ssrf_fetch(self):
        opp_id, _ = finder.add_opportunity(self.tid, self.base + "/dir", "directory")
        opp = db.query("SELECT * FROM opportunities WHERE id=?", (opp_id,), one=True)
        r = quality.evaluate(opp, self.target, self.fetcher(), self.cfg)
        quality.save_assessment(opp, r)
        self.assertEqual(r["classification"], "eligible"); self.assertTrue(r["submit_url"].endswith("/submit"))
        self.assertTrue(any(b["kind"] == "estimate" for b in r["breakdown"]))
        self.assertNotIn("domain authority", json.dumps(r).lower().replace("not domain authority", ""))
    def test_robots_blocks(self):
        res = self.fetcher().fetch(self.base + "/private/x"); self.assertEqual(res.error_kind, "robots")
    def test_submit_flow(self):
        oid, _ = finder.add_opportunity(self.tid, self.base + "/submit", "directory")
        db.execute("UPDATE opportunities SET status='eligible', approved=0 WHERE id=?", (oid,))
        prep = submitter.prepare(oid, self.fetcher(), self.cfg); self.assertFalse(prep["can_auto"])   # needs approval
        r = submitter.submit(oid, self.fetcher(), self.cfg); self.assertEqual(r["status"], "skipped"); self.assertEqual(SUBMITS, [])
        db.execute("UPDATE opportunities SET approved=1 WHERE id=?", (oid,))
        r = submitter.submit(oid, self.fetcher(), self.cfg)
        self.assertEqual(r["status"], "awaiting_approval")
        self.assertEqual(SUBMITS[-1]["url"], ["https://mysite.com/"]); self.assertEqual(SUBMITS[-1]["hp_field"], [""])
        bl = db.query("SELECT * FROM backlinks WHERE opportunity_id=?", (oid,), one=True)
        self.assertEqual(bl["state"], "pending")                       # NOT live
        again = submitter.submit(oid, self.fetcher(), self.cfg); self.assertEqual(again["status"], "skipped")
        self.assertEqual(len(SUBMITS), 1)
    def test_captcha_not_bypassed(self):
        oid, _ = finder.add_opportunity(self.tid, self.base + "/captcha", "directory")
        db.execute("UPDATE opportunities SET status='eligible', approved=1 WHERE id=?", (oid,))
        n = len(SUBMITS); r = submitter.submit(oid, self.fetcher(), self.cfg)
        self.assertEqual(r["status"], "captcha_required"); self.assertEqual(len(SUBMITS), n)
    def test_checker_states(self):
        f = self.fetcher(); t = "https://mysite.com/"
        cases = {"/listing_live": "live_nofollow", "/listing_clean": "live", "/listing_ugc": "live_ugc", "/listing_none": "not_found",
                 "/gone": "not_found", "/redir": "live", "/flaky": "live"}
        for path, want in cases.items():
            self.assertEqual(checker.check_backlink(f, self.base + path, t)["state"], want, path)
        r = checker.check_backlink(f, self.base + "/listing_live", t); self.assertEqual((r["anchor_text"], r["rel"]), ("My Site", "nofollow"))
    def test_verification_history_and_disappearance(self):
        bid, _ = checker.add_backlink(self.tid, self.base + "/listing_clean")
        checker.run_check(bid, self.fetcher(), self.cfg)
        row = db.query("SELECT * FROM backlinks WHERE id=?", (bid,), one=True)
        self.assertEqual(row["state"], "live"); self.assertTrue(row["first_seen"])
        db.execute("UPDATE backlinks SET source_url=? WHERE id=?", (self.base + "/listing_none", bid))
        checker.run_check(bid, self.fetcher(), self.cfg)
        row = db.query("SELECT * FROM backlinks WHERE id=?", (bid,), one=True)
        self.assertEqual((row["state"], row["disappeared"]), ("not_found", 1))
        self.assertEqual(db.query("SELECT COUNT(*) c FROM verification_history WHERE backlink_id=?", (bid,), one=True)["c"], 2)
    def test_outreach_and_export(self):
        oid, _ = finder.add_opportunity(self.tid, "https://blog.example.org/write", "guest_post", title="Write for us")
        d = outreach.create_draft(oid); self.assertIn("mysite.com", d["body"]); self.assertEqual(d["id"], outreach.create_draft(oid)["id"])
        for kind in exporter.KINDS:
            for fmt in ("csv", "json"):
                name, mime, data = exporter.export(kind, fmt, self.tid); self.assertTrue(data)
        db.execute("UPDATE opportunities SET title='=HYPERLINK(\"x\")' WHERE id=?", (oid,))
        self.assertEqual(exporter._csv_safe("=1+1"), "'=1+1")


class TestApp(Base):
    def test_routes_and_guards(self):
        import app as A
        c = A.create_app(start_background=False).test_client()
        H1 = {"Host": "127.0.0.1:8080"}; H2 = dict(H1, **{"X-Requested-With": "BacklinkBuilder"})
        for p in ("/", "/opportunities", "/backlinks", "/history", "/settings"): self.assertEqual(c.get(p, headers=H1).status_code, 200, p)
        self.assertEqual(c.get("/", headers={"Host": "evil.com"}).status_code, 403)
        self.assertEqual(c.post("/api/targets", json={"url": "x.com"}, headers=H1).status_code, 403)
        self.assertEqual(c.post("/api/targets", json={"url": "http://localhost"}, headers=H2).status_code, 400)
        self.assertEqual(c.post("/api/settings", json={"bogus": 1}, headers=H2).status_code, 400)
        self.assertEqual(c.get(f"/api/state?target_id={self.tid}", headers=H1).status_code, 200)
        self.assertEqual(c.get(f"/export/backlinks.csv?target_id={self.tid}", headers=H1).status_code, 200)
        self.assertEqual(c.get("/export/evil.csv?target_id=%d" % self.tid, headers=H1).status_code, 404)


if __name__ == "__main__":
    unittest.main()
