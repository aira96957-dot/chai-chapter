"""Cafe website backend. Uses ONLY Python's standard library (no pip install)."""
import base64, csv, hmac, io, json, os, sqlite3, time, webbrowser
from contextlib import closing
from datetime import date, datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "cafe.db")
PORT = int(os.environ.get("PORT", 8000))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
HITS = {}   # ip -> timestamps, tiny anti-spam limiter

SCHEMA = """
CREATE TABLE IF NOT EXISTS menu(
  id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, cat_order INTEGER NOT NULL,
  sort INTEGER NOT NULL, name TEXT NOT NULL, description TEXT, price INTEGER NOT NULL,
  tag TEXT, tag_class TEXT DEFAULT '', available INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS reservations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL,
  res_date TEXT NOT NULL, res_time TEXT NOT NULL, guests INTEGER NOT NULL CHECK(guests BETWEEN 1 AND 8),
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, text TEXT NOT NULL,
  rating INTEGER NOT NULL DEFAULT 5 CHECK(rating BETWEEN 1 AND 5),
  approved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')));
"""

def connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with closing(connect()) as c:
        c.executescript(SCHEMA)
        if not c.execute("SELECT 1 FROM menu LIMIT 1").fetchone():
            menu = json.load(open(os.path.join(BASE, "menu_seed.json"), encoding="utf-8"))
            for ci, (cat, items) in enumerate(menu.items()):
                for si, it in enumerate(items):
                    c.execute("INSERT INTO menu(category,cat_order,sort,name,description,price,tag,tag_class) VALUES(?,?,?,?,?,?,?,?)",
                              (cat, ci, si, it[0], it[1], it[2], it[3] if len(it) > 3 else None, it[4] if len(it) > 4 else ""))
        if not c.execute("SELECT 1 FROM reviews LIMIT 1").fetchone():
            for r in reversed(json.load(open(os.path.join(BASE, "reviews_seed.json"), encoding="utf-8"))):
                c.execute("INSERT INTO reviews(name,text,rating,approved) VALUES(?,?,5,1)", (r["name"], r["text"]))
        c.commit()

def clean(v, n):
    return " ".join(str(v or "").split())[:n]

class Handler(BaseHTTPRequestHandler):
    server_version = "CafeServer"

    def log_message(self, fmt, *a):
        pass

    # ---------- helpers ----------
    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def out(self, code, obj):
        self.send(code, json.dumps(obj), "application/json")

    def authed(self):
        h = self.headers.get("Authorization", "")
        try:
            pw = base64.b64decode(h[6:]).decode().split(":", 1)[1] if h.startswith("Basic ") else None
        except Exception:
            pw = None
        return pw is not None and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())

    def need_auth(self):
        self.send(401, "Login required", headers={"WWW-Authenticate": 'Basic realm="Cafe admin"'})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10000:
            return None
        return self.rfile.read(n)

    def limited(self):
        ip, now = self.client_address[0], time.time()
        HITS[ip] = [t for t in HITS.get(ip, []) if now - t < 3600] + [now]
        return len(HITS[ip]) > 30

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, open(os.path.join(BASE, "public", "index.html"), "rb").read())
        if path == "/api/menu":
            with closing(connect()) as c:
                rows = c.execute("SELECT * FROM menu WHERE available=1 ORDER BY cat_order,sort").fetchall()
            m = {}
            for r in rows:
                it = [r["name"], r["description"], r["price"]]
                if r["tag"]:
                    it += [r["tag"], r["tag_class"] or ""]
                m.setdefault(r["category"], []).append(it)
            return self.out(200, m)
        if path == "/api/reviews":
            with closing(connect()) as c:
                rows = c.execute("SELECT name,text,rating FROM reviews WHERE approved=1 ORDER BY id DESC LIMIT 9").fetchall()
            return self.out(200, [dict(r) for r in rows])
        if path in ("/admin", "/admin/export.csv"):
            if not self.authed():
                return self.need_auth()
            return self.admin() if path == "/admin" else self.export()
        self.send(404, "Not found")

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        raw = self.body()
        if raw is None:
            return self.out(413, {"error": "Request too large."})
        if path == "/admin/action":
            return self.admin_action(raw)
        if path not in ("/api/reserve", "/api/reviews"):
            return self.send(404, "Not found")
        if self.limited():
            return self.out(429, {"error": "Too many requests. Please try again later."})
        try:
            d = json.loads(raw)
            assert isinstance(d, dict)
        except Exception:
            return self.out(400, {"error": "Invalid data."})
        return self.reserve(d) if path == "/api/reserve" else self.review(d)

    def reserve(self, d):
        name, phone = clean(d.get("name"), 60), clean(d.get("phone"), 20)
        digits = sum(ch.isdigit() for ch in phone)
        try:
            day = date.fromisoformat(str(d.get("date")))
            hh, mm = map(int, str(d.get("time")).split(":"))
            mins = hh * 60 + mm
            guests = d.get("guests")
            assert type(guests) is int and 1 <= guests <= 8
        except Exception:
            return self.out(400, {"error": "Please check the date, time and number of guests."})
        if not name:
            return self.out(400, {"error": "Please enter your name."})
        if not (7 <= digits <= 15) or not all(ch.isdigit() or ch in "+- ()" for ch in phone):
            return self.out(400, {"error": "Please enter a valid phone number."})
        if not (date.today() <= day <= date.today() + timedelta(days=90)):
            return self.out(400, {"error": "Pick a date within the next 90 days."})
        if not (480 <= mins <= 1380):
            return self.out(400, {"error": "We take bookings between 8:00 am and 11:00 pm."})
        t = f"{hh:02d}:{mm:02d}"
        with closing(connect()) as c:
            cur = c.execute("INSERT INTO reservations(name,phone,res_date,res_time,guests) VALUES(?,?,?,?,?)",
                            (name, phone, day.isoformat(), t, guests))
            c.commit()
        self.out(201, {"id": cur.lastrowid, "date": day.isoformat(), "time": t})

    def review(self, d):
        name, text = clean(d.get("name"), 60), clean(d.get("text"), 300)
        rating = d.get("rating")
        if not name or len(text) < 10 or type(rating) is not int or not 1 <= rating <= 5:
            return self.out(400, {"error": "Add your name, a rating and at least 10 characters."})
        with closing(connect()) as c:
            c.execute("INSERT INTO reviews(name,text,rating) VALUES(?,?,?)", (name, text, rating))
            c.commit()
        self.out(201, {"ok": True})

    # ---------- admin ----------
    def admin(self):
        with closing(connect()) as c:
            res = c.execute("SELECT * FROM reservations ORDER BY res_date DESC,res_time DESC LIMIT 60").fetchall()
            rev = c.execute("SELECT * FROM reviews WHERE approved=0 ORDER BY id DESC").fetchall()
            nm = c.execute("SELECT COUNT(*) FROM menu").fetchone()[0]
        def btn(kind, i, act, label):
            return (f'<form method="post" action="/admin/action"><input type="hidden" name="kind" value="{kind}">'
                    f'<input type="hidden" name="id" value="{i}"><button name="action" value="{act}">{label}</button></form>')
        rrows = "".join(
            f"<tr><td>{r['id']}</td><td>{escape(r['res_date'])} {escape(r['res_time'])}</td><td>{escape(r['name'])}</td>"
            f"<td>{escape(r['phone'])}</td><td>{r['guests']}</td><td><b>{escape(r['status'])}</b></td>"
            f"<td class=a>{btn('res', r['id'], 'confirmed', 'Confirm')}{btn('res', r['id'], 'cancelled', 'Cancel')}{btn('res', r['id'], 'delete', 'Delete')}</td></tr>"
            for r in res) or "<tr><td colspan=7>No bookings yet</td></tr>"
        vrows = "".join(
            f"<tr><td>{escape(v['name'])} ({v['rating']}/5)</td><td>{escape(v['text'])}</td>"
            f"<td class=a>{btn('rev', v['id'], 'approve', 'Approve')}{btn('rev', v['id'], 'delete', 'Delete')}</td></tr>"
            for v in rev) or "<tr><td colspan=3>No reviews waiting</td></tr>"
        self.send(200, f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Cafe admin</title><style>body{{font:16px system-ui;max-width:1000px;margin:24px auto;padding:0 14px;color:#2b1d16}}
table{{border-collapse:collapse;width:100%;margin-bottom:30px}}td,th{{border:1px solid #d8c9b0;padding:8px;text-align:left;vertical-align:top}}
th{{background:#f3e6d3}}.a form{{display:inline}}button{{cursor:pointer;margin:1px}}</style>
<h1>Chai &amp; Chapter admin</h1><p>{nm} menu items &middot; <a href="/admin/export.csv">Download bookings (CSV)</a> &middot; <a href="/">View website</a></p>
<h2>Table bookings</h2><div style="overflow-x:auto"><table><tr><th>#</th><th>When</th><th>Name</th><th>Phone</th><th>Guests</th><th>Status</th><th>Action</th></tr>{rrows}</table></div>
<h2>Reviews waiting for approval</h2><table><tr><th>From</th><th>Review</th><th>Action</th></tr>{vrows}</table>""")

    def admin_action(self, raw):
        if not self.authed():
            return self.need_auth()
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send(403, "Blocked")
        f = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "ignore")).items()}
        kind, act = f.get("kind"), f.get("action")
        try:
            i = int(f.get("id", ""))
        except ValueError:
            return self.send(400, "Bad id")
        with closing(connect()) as c:
            if kind == "res" and act in ("confirmed", "cancelled"):
                c.execute("UPDATE reservations SET status=? WHERE id=?", (act, i))
            elif kind == "res" and act == "delete":
                c.execute("DELETE FROM reservations WHERE id=?", (i,))
            elif kind == "rev" and act == "approve":
                c.execute("UPDATE reviews SET approved=1 WHERE id=?", (i,))
            elif kind == "rev" and act == "delete":
                c.execute("DELETE FROM reviews WHERE id=?", (i,))
            else:
                return self.send(400, "Bad action")
            c.commit()
        self.send(303, "", headers={"Location": "/admin"})

    def export(self):
        with closing(connect()) as c:
            rows = c.execute("SELECT id,name,phone,res_date,res_time,guests,status,created_at FROM reservations ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "name", "phone", "date", "time", "guests", "status", "created_at"])
        for r in rows:
            w.writerow([("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v for v in tuple(r)])
        self.send(200, buf.getvalue(), "text/csv", {"Content-Disposition": "attachment; filename=bookings.csv"})

if __name__ == "__main__":
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\nCafe website running at {url}\nAdmin page: {url}/admin  (password: {ADMIN_PASSWORD})\nPress Ctrl+C to stop.\n")
    if not os.environ.get("NO_BROWSER"):
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
