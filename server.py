#!/usr/bin/env python3
"""Artemis NYC — Resy Reservation Sniper & Monitor"""

import http.server
import json
import os
import sqlite3
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

PORT = int(os.environ.get('PORT', 3001))
DIR = os.path.dirname(os.path.abspath(__file__))

if os.path.isdir("/data"):
    DB_PATH = "/data/resy.db"
else:
    DB_PATH = os.path.join(DIR, "resy.db")

RESY_API = "https://api.resy.com"

# ─── Database ───────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venue_id TEXT NOT NULL,
            venue_name TEXT DEFAULT '',
            party_size INTEGER DEFAULT 2,
            date_start TEXT NOT NULL,
            date_end TEXT,
            time_earliest TEXT DEFAULT '17:00',
            time_latest TEXT DEFAULT '22:00',
            snipe_mode INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            last_checked TEXT
        );
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id INTEGER,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            details TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS found_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id INTEGER,
            venue_name TEXT,
            date TEXT,
            time TEXT,
            party_size INTEGER,
            slot_token TEXT,
            config_token TEXT,
            seen_at TEXT DEFAULT (datetime('now')),
            booked INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()

# ─── Resy API Helpers ───────────────────────────────────────────────

def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}

def resy_headers(settings=None):
    if settings is None:
        settings = get_settings()
    h = {
        "Authorization": 'ResyAPI api_key="%s"' % settings.get('api_key', ''),
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Origin": "https://resy.com",
        "Referer": "https://resy.com/",
    }
    token = settings.get('auth_token', '')
    if token:
        h["X-Resy-Auth-Token"] = token
    return h

def resy_find(venue_id, day, party_size, settings=None):
    """Find available slots for a venue on a given day."""
    params = urllib.parse.urlencode({
        "venue_id": venue_id,
        "day": day,
        "party_size": party_size,
        "lat": "40.7128",
        "long": "-74.0060",
    })
    url = f"{RESY_API}/4/find?{params}"
    req = urllib.request.Request(url, headers=resy_headers(settings))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def resy_venue(venue_id, settings=None):
    """Get venue details."""
    params = urllib.parse.urlencode({"id": venue_id})
    url = f"{RESY_API}/4/venue?{params}"
    req = urllib.request.Request(url, headers=resy_headers(settings))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def resy_search(query, settings=None):
    """Search for venues."""
    params = urllib.parse.urlencode({
        "query": query,
        "geo": '{"latitude":40.7128,"longitude":-74.0060}',
        "types": '["venue"]',
        "per_page": 10,
    })
    url = f"{RESY_API}/3/venuesearch/search?{params}"
    req = urllib.request.Request(url, headers=resy_headers(settings))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def resy_get_details(config_id, day, party_size, settings=None):
    """Get booking details/token for a specific slot."""
    params = urllib.parse.urlencode({
        "config_id": config_id,
        "day": day,
        "party_size": party_size,
    })
    url = f"{RESY_API}/3/details?{params}"
    req = urllib.request.Request(url, headers=resy_headers(settings))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def resy_book(book_token, settings=None):
    """Book a reservation."""
    s = settings or get_settings()
    data = urllib.parse.urlencode({
        "book_token": book_token,
        "struct_payment_method": json.dumps({"id": int(s.get("payment_method_id", 0))}),
    }).encode()
    url = f"{RESY_API}/3/book"
    req = urllib.request.Request(url, data=data, headers=resy_headers(s), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

# ─── Monitor Thread ─────────────────────────────────────────────────

_monitor_running = False
_monitor_thread = None

def start_monitor():
    global _monitor_running, _monitor_thread
    if not _monitor_running:
        _monitor_running = True
        _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        _monitor_thread.start()
        log_activity(None, "system", "🟢 Monitor started")

def stop_monitor():
    global _monitor_running
    _monitor_running = False
    log_activity(None, "system", "🔴 Monitor stopped")

def monitor_loop():
    global _monitor_running
    while _monitor_running:
        try:
            check_all_watches()
        except Exception as e:
            log_activity(None, "error", f"Monitor error: {e}")
        time.sleep(30)  # Check every 30 seconds

def check_all_watches():
    conn = get_db()
    watches = conn.execute("SELECT * FROM watches WHERE active=1").fetchall()
    settings = get_settings()
    
    if not settings.get('api_key'):
        conn.close()
        return
    
    for w in watches:
        try:
            check_watch(dict(w), settings, conn)
        except Exception as e:
            log_activity(w['id'], "error", f"Error checking {w['venue_name']}: {e}", conn=conn)
    conn.close()

def check_watch(watch, settings, conn):
    from datetime import date as dt_date
    
    date_start = watch['date_start']
    date_end = watch.get('date_end') or date_start
    
    # Generate all dates in range
    start = dt_date.fromisoformat(date_start)
    end = dt_date.fromisoformat(date_end)
    current = start
    
    while current <= end:
        day_str = current.isoformat()
        result = resy_find(watch['venue_id'], day_str, watch['party_size'], settings)
        
        if "error" in result:
            current = dt_date.fromordinal(current.toordinal() + 1)
            continue
        
        # Parse slots
        venues = result.get("results", {}).get("venues", [])
        for venue in venues:
            slots = venue.get("slots", [])
            for slot in slots:
                slot_date = slot.get("date", {})
                start_time = slot_date.get("start", "")
                
                # Check if time is in range
                if start_time:
                    time_part = start_time.split(" ")[-1] if " " in start_time else start_time
                    # Simple time check
                    if watch['time_earliest'] and watch['time_latest']:
                        t = time_part[:5]
                        if t < watch['time_earliest'] or t > watch['time_latest']:
                            continue
                
                config_token = slot.get("config", {}).get("token", "")
                
                # Check if we already found this slot
                existing = conn.execute(
                    "SELECT id FROM found_slots WHERE watch_id=? AND date=? AND time=? AND booked=0",
                    (watch['id'], day_str, start_time)
                ).fetchone()
                
                if not existing:
                    conn.execute(
                        "INSERT INTO found_slots (watch_id, venue_name, date, time, party_size, config_token) VALUES (?,?,?,?,?,?)",
                        (watch['id'], watch['venue_name'], day_str, start_time, watch['party_size'], config_token)
                    )
                    log_activity(watch['id'], "found",
                        f"🎯 Slot found! {watch['venue_name']} - {day_str} at {start_time} for {watch['party_size']}",
                        conn=conn)
                    
                    # Auto-book if snipe mode
                    if watch['snipe_mode'] and config_token and settings.get('auth_token'):
                        try_snipe(watch, config_token, day_str, start_time, settings, conn)
        
        current = dt_date.fromordinal(current.toordinal() + 1)
        time.sleep(1)  # Rate limit between days
    
    conn.execute("UPDATE watches SET last_checked=datetime('now') WHERE id=?", (watch['id'],))
    conn.commit()

def try_snipe(watch, config_token, day, time_str, settings, conn):
    """Attempt to auto-book a found slot."""
    log_activity(watch['id'], "snipe", f"⚡ Attempting to snipe {watch['venue_name']} {day} {time_str}...", conn=conn)
    
    # Step 1: Get booking details
    details = resy_get_details(config_token, day, watch['party_size'], settings)
    if "error" in details:
        log_activity(watch['id'], "error", f"Failed to get details: {details['error']}", conn=conn)
        return
    
    book_token = details.get("book_token", {}).get("value", "")
    if not book_token:
        log_activity(watch['id'], "error", "No book token received", conn=conn)
        return
    
    # Step 2: Book it
    result = resy_book(book_token, settings)
    if "error" in result:
        log_activity(watch['id'], "error", f"Booking failed: {result['error']}", conn=conn)
    else:
        log_activity(watch['id'], "booked", f"✅ BOOKED! {watch['venue_name']} {day} at {time_str}", conn=conn)
        conn.execute("UPDATE found_slots SET booked=1 WHERE watch_id=? AND date=? AND time=?",
            (watch['id'], day, time_str))
        conn.commit()

def log_activity(watch_id, type_, message, details=None, conn=None):
    close = False
    if conn is None:
        conn = get_db()
        close = True
    conn.execute("INSERT INTO activity (watch_id, type, message, details) VALUES (?,?,?,?)",
        (watch_id, type_, message, details))
    conn.commit()
    if close:
        conn.close()

# ─── HTTP Handler ───────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Quiet logs

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            fpath = os.path.join(DIR, "index.html")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            with open(fpath, "rb") as f:
                self.wfile.write(f.read())

        elif path == "/api/watches":
            conn = get_db()
            rows = conn.execute("SELECT * FROM watches ORDER BY created_at DESC").fetchall()
            conn.close()
            self._json([dict(r) for r in rows])

        elif path == "/api/activity":
            conn = get_db()
            rows = conn.execute("SELECT * FROM activity ORDER BY created_at DESC LIMIT 100").fetchall()
            conn.close()
            self._json([dict(r) for r in rows])

        elif path == "/api/found":
            conn = get_db()
            rows = conn.execute("SELECT * FROM found_slots ORDER BY seen_at DESC LIMIT 50").fetchall()
            conn.close()
            self._json([dict(r) for r in rows])

        elif path == "/api/settings":
            s = get_settings()
            # Mask sensitive values
            masked = {}
            for k, v in s.items():
                if k in ('api_key', 'auth_token') and v:
                    masked[k] = v[:8] + "..." + v[-4:] if len(v) > 12 else "***"
                else:
                    masked[k] = v
            self._json(masked)

        elif path == "/api/monitor/status":
            self._json({"running": _monitor_running})

        elif path.startswith("/api/search"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            query = params.get("q", [""])[0]
            if query:
                result = resy_search(query)
                self._json(result)
            else:
                self._json({"error": "Missing query"}, 400)

        elif path.startswith("/api/venue/"):
            venue_id = path.split("/")[-1]
            result = resy_venue(venue_id)
            self._json(result)

        elif path == "/api/check":
            # Manual check trigger
            threading.Thread(target=check_all_watches, daemon=True).start()
            self._json({"ok": True, "message": "Check triggered"})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/watches":
            body = self._read_body()
            conn = get_db()
            conn.execute(
                """INSERT INTO watches (venue_id, venue_name, party_size, date_start, date_end,
                   time_earliest, time_latest, snipe_mode)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (body.get('venue_id'), body.get('venue_name', ''),
                 body.get('party_size', 2), body.get('date_start'),
                 body.get('date_end'), body.get('time_earliest', '17:00'),
                 body.get('time_latest', '22:00'), body.get('snipe_mode', 0))
            )
            conn.commit()
            watch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.close()
            log_activity(watch_id, "watch", f"Added watch: {body.get('venue_name', body.get('venue_id'))}")
            self._json({"ok": True, "id": watch_id})

        elif path == "/api/settings":
            body = self._read_body()
            conn = get_db()
            for k, v in body.items():
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (k, v))
            conn.commit()
            conn.close()
            self._json({"ok": True})

        elif path == "/api/monitor/start":
            start_monitor()
            self._json({"running": True})

        elif path == "/api/monitor/stop":
            stop_monitor()
            self._json({"running": False})

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        path = self.path.split("?")[0]

        if path.startswith("/api/watches/"):
            watch_id = path.split("/")[-1]
            conn = get_db()
            conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            conn.commit()
            conn.close()
            self._json({"ok": True})

        elif path == "/api/activity":
            conn = get_db()
            conn.execute("DELETE FROM activity")
            conn.commit()
            conn.close()
            self._json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        path = self.path.split("?")[0]

        if path.startswith("/api/watches/"):
            watch_id = path.split("/")[-1]
            body = self._read_body()
            conn = get_db()
            fields = []
            values = []
            for k in ('active', 'snipe_mode', 'party_size', 'date_start', 'date_end',
                       'time_earliest', 'time_latest'):
                if k in body:
                    fields.append(f"{k}=?")
                    values.append(body[k])
            if fields:
                values.append(watch_id)
                conn.execute(f"UPDATE watches SET {','.join(fields)} WHERE id=?", values)
                conn.commit()
            conn.close()
            self._json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

# ─── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"🏹 Artemis NYC running on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _monitor_running = False
        server.shutdown()
