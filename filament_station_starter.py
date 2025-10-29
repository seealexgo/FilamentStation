#!/usr/bin/env python3

"""
Filament Station v1.1 (Quick-Pair Moves)
----------------------------------------
- Scan a spool QR then a location QR (bin/AMS/dryer) in quick succession to auto-move.
- Also works location -> spool within the same pairing window.
- Configurable location QR strings and pairing window in config.json.

Base features:
- Tkinter touchscreen UI
- USB webcam QR scanning (OpenCV + pyzbar)
- Local SQLite cache of spools and logs
- Opens 3DFilamentProfiles spool pages in a browser on demand
"""

import os
import sys
import json
import time
import queue
import sqlite3
import threading
import webbrowser
from datetime import datetime

# --- UI ---
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# --- Camera / QR ---
try:
    import cv2
    from pyzbar import pyzbar
except Exception:
    cv2 = None
    pyzbar = None

DB_PATH = os.environ.get("FS_DB_PATH", os.path.expanduser("~/.filament_station/filaments.db"))
CONF_PATH = os.environ.get("FS_CONF_PATH", os.path.expanduser("~/.filament_station/config.json"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DEFAULT_CONFIG = {
    "bins": [
        {"name": "PLA Dry Box A", "target_rh": 40},
        {"name": "PETG Dry Box B", "target_rh": 40},
        {"name": "TPU Dry Box C", "target_rh": 35}
    ],
    "locations": [
        {"qr": "fs://loc/pla-a", "name": "PLA Dry Box A"},
        {"qr": "fs://loc/petg-b", "name": "PETG Dry Box B"},
        {"qr": "fs://loc/tpu-c", "name": "TPU Dry Box C"},
        {"qr": "fs://loc/ams-1", "name": "AMS Slot 1"},
        {"qr": "fs://loc/ams-2", "name": "AMS Slot 2"},
        {"qr": "fs://loc/ams-3", "name": "AMS Slot 3"},
        {"qr": "fs://loc/ams-4", "name": "AMS Slot 4"},
        {"qr": "fs://loc/dryer", "name": "Dryer"}
    ],
    "pair_window_seconds": 10,
    "camera_index": 0,
    "scan_interval_ms": 250,
    "kiosk_mode": False,
    "browser": "chromium-browser"
}

def load_config():
    if os.path.exists(CONF_PATH):
        try:
            with open(CONF_PATH, "r") as f:
                cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            for k, v in cfg.items():
                merged[k] = v
            return merged
        except Exception:
            pass
    # write default config if missing
    if not os.path.exists(CONF_PATH):
        os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
        with open(CONF_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return DEFAULT_CONFIG

CFG = load_config()

# --------------------
# Database helpers
# --------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS spools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        name TEXT,
        material TEXT,
        color TEXT,
        location TEXT,
        last_weight_g REAL,
        last_updated TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spool_id INTEGER,
        timestamp TEXT,
        action TEXT,
        weight_g REAL,
        location TEXT,
        note TEXT,
        FOREIGN KEY(spool_id) REFERENCES spools(id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    conn.close()

def upsert_spool(url, name=None, material=None, color=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM spools WHERE url = ?", (url,))
    row = cur.fetchone()
    if row is None:
        cur.execute("""
            INSERT INTO spools (url, name, material, color, location, last_weight_g, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (url, name, material, color, None, None, None))
        conn.commit()
        spool_id = cur.lastrowid
    else:
        spool_id = row["id"]
        if any([name, material, color]):
            cur.execute("""
                UPDATE spools SET name = COALESCE(?, name),
                                  material = COALESCE(?, material),
                                  color = COALESCE(?, color)
                WHERE id = ?
            """, (name, material, color, spool_id))
            conn.commit()
    conn.close()
    return spool_id

def get_spool_by_url(url):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM spools WHERE url = ?", (url,))
    row = cur.fetchone()
    conn.close()
    return row

def update_weight(spool_id, weight_g):
    ts = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE spools SET last_weight_g = ?, last_updated = ? WHERE id = ?",
                (weight_g, ts, spool_id))
    cur.execute("INSERT INTO logs (spool_id, timestamp, action, weight_g) VALUES (?, ?, 'weigh', ?)",
                (spool_id, ts, weight_g))
    conn.commit()
    conn.close()

def update_location(spool_id, location):
    ts = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE spools SET location = ?, last_updated = ? WHERE id = ?",
                (location, ts, spool_id))
    cur.execute("INSERT INTO logs (spool_id, timestamp, action, location) VALUES (?, ?, 'move', ?)",
                (spool_id, ts, location))
    conn.commit()
    conn.close()

# --------------------
# QR helpers
# --------------------
def classify_qr_payload(payload: str):
    """Return ('location', name) if payload matches a configured location QR;
       else ('spool', payload).
    """
    for loc in CFG.get("locations", []):
        if payload == loc.get("qr"):
            return ("location", loc.get("name"))
    return ("spool", payload)

# --------------------
# QR Scanning thread
# --------------------
class QRScanner(threading.Thread):
    def __init__(self, camera_index=0, interval_ms=250, out_queue=None):
        super().__init__(daemon=True)
        self.camera_index = camera_index
        self.interval = interval_ms / 1000.0
        self.q = out_queue or queue.Queue()
        self._stop = threading.Event()
        self.cap = None

    def run(self):
        if cv2 is None or pyzbar is None:
            self.q.put(("error", "Missing OpenCV/pyzbar. Install deps."))
            return
        src = CFG.get("camera_url")
        self.cap = cv2.VideoCapture(src if src else self.camera_index)
        if not self.cap.isOpened():
            self.q.put(("error", f"Cannot open camera index {self.camera_index}"))
            return
        last_val = None
        same_count = 0
        while not self._stop.is_set():
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(self.interval)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            codes = pyzbar.decode(gray)
            if codes:
                data = codes[0].data.decode("utf-8").strip()
                if data == last_val:
                    same_count += 1
                else:
                    last_val = data
                    same_count = 1
                if same_count >= 2:
                    self.q.put(("qr", data))
                    time.sleep(1.0)  # debounce
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()
        try:
            if self.cap:
                self.cap.release()
        except Exception:
            pass

# --------------------
# UI
# --------------------
import shutil

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Filament Station v1.1")
        if CFG.get("kiosk_mode"):
            self.attributes("-fullscreen", True)
        self.geometry("480x320")
        self.configure(bg="#111")

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("TLabel", background="#111", foreground="#eee", font=("DejaVu Sans", 12))
        self.style.configure("Header.TLabel", font=("DejaVu Sans", 14, "bold"))
        self.style.configure("TButton", font=("DejaVu Sans", 12))

        # State
        self.current_url = None
        self.current_spool = None
        self.last_spool_scan_ts = 0.0
        self.last_location_scan = None  # (name, ts)

        # UI
        self.lbl_title = ttk.Label(self, text="📦 Filament Station", style="Header.TLabel")
        self.lbl_title.pack(pady=4)

        info = ttk.Frame(self)
        info.pack(fill="both", expand=True, padx=8, pady=6)

        self.var_name = tk.StringVar(value="--")
        self.var_weight = tk.StringVar(value="Weight: -- g")
        self.var_loc = tk.StringVar(value="Location: --")
        self.var_last = tk.StringVar(value="Updated: --")
        self.var_status = tk.StringVar(value="Ready. Scan a spool.")

        ttk.Label(info, textvariable=self.var_name).pack(anchor="w", pady=2)
        ttk.Label(info, textvariable=self.var_weight).pack(anchor="w", pady=2)
        ttk.Label(info, textvariable=self.var_loc).pack(anchor="w", pady=2)
        ttk.Label(info, textvariable=self.var_last).pack(anchor="w", pady=2)

        btns = ttk.Frame(self)
        btns.pack(pady=6)
        ttk.Button(btns, text="Weigh Now", command=self.on_weigh).grid(row=0, column=0, padx=6, pady=4)
        ttk.Button(btns, text="Move Location", command=self.on_move).grid(row=0, column=1, padx=6, pady=4)
        ttk.Button(btns, text="Open 3DFP", command=self.on_open).grid(row=0, column=2, padx=6, pady=4)

        status = ttk.Label(self, textvariable=self.var_status)
        status.pack(pady=4)

        man = ttk.Frame(self)
        man.pack(pady=4)
        ttk.Button(man, text="Manual URL", command=self.manual_url).grid(row=0, column=0, padx=6)
        ttk.Button(man, text="Show Location QRs", command=self.show_locations).grid(row=0, column=1, padx=6)
        ttk.Button(man, text="Quit", command=self.on_quit).grid(row=0, column=2, padx=6)

        # QR scanner
        self.q = queue.Queue()
        self.scanner = QRScanner(camera_index=CFG["camera_index"],
                                 interval_ms=CFG["scan_interval_ms"],
                                 out_queue=self.q)
        self.after(200, self.poll_q)
        self.scanner.start()

    # --- QR event loop ---
    def poll_q(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "error":
                    messagebox.showerror("QR Error", payload)
                elif kind == "qr":
                    self.on_qr(payload)
        except queue.Empty:
            pass
        self.after(200, self.poll_q)

    def on_qr(self, payload):
        qtype, value = classify_qr_payload(payload)
        now = time.time()
        if qtype == "spool":
            self.handle_spool_scan(value, now)
        else:
            self.handle_location_scan(value, now)

    # --- Handlers ---
    def handle_spool_scan(self, url, now):
        self.current_url = url
        spool = get_spool_by_url(url)
        if spool is None:
            guess = url.rstrip("/").split("/")[-1].replace("-", " ").title()
            sid = upsert_spool(url, name=guess)
            spool = get_spool_by_url(url)
        self.current_spool = spool
        self.last_spool_scan_ts = now
        self.refresh_labels()
        self.log_action("scan", note=f"Scanned spool: {url}")
        self.var_status.set("Spool scanned. (Scan a location to move it.)")

        # If a location was just scanned, pair it
        if self.last_location_scan:
            loc_name, loc_ts = self.last_location_scan
            if (now - loc_ts) <= CFG.get("pair_window_seconds", 10):
                self.apply_location_move(loc_name)

    def handle_location_scan(self, loc_name, now):
        self.last_location_scan = (loc_name, now)
        self.var_status.set(f"Location scanned: {loc_name}. (Scan spool to move.)")

        # If a spool was just scanned, pair it
        if self.current_spool and (now - self.last_spool_scan_ts) <= CFG.get("pair_window_seconds", 10):
            self.apply_location_move(loc_name)

    def apply_location_move(self, loc_name):
        if not self.current_spool:
            return
        update_location(self.current_spool["id"], loc_name)
        self.current_spool = get_spool_by_url(self.current_spool["url"])
        self.refresh_labels()
        self.var_status.set(f"✅ Moved to: {loc_name}")
        self.log_action("move", location=loc_name)
        # clear the last_location_scan to avoid repeated moves
        self.last_location_scan = None

    def log_action(self, action, weight_g=None, location=None, note=None):
        if not self.current_spool:
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO logs (spool_id, timestamp, action, weight_g, location, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (self.current_spool["id"],
              datetime.utcnow().isoformat(),
              action, weight_g, location, note))
        conn.commit()
        conn.close()

    def refresh_labels(self):
        s = self.current_spool
        if not s:
            self.var_name.set("--")
            self.var_weight.set("Weight: -- g")
            self.var_loc.set("Location: --")
            self.var_last.set("Updated: --")
            return
        name = s["name"] or "Unknown Spool"
        self.var_name.set(name)
        w = s["last_weight_g"]
        self.var_weight.set(f"Weight: {w:.0f} g" if w is not None else "Weight: -- g")
        loc = s["location"] or "--"
        self.var_loc.set(f"Location: {loc}")
        last = s["last_updated"] or "--"
        self.var_last.set(f"Updated: {last}")

    # --- Buttons ---
    def on_weigh(self):
        if not self.current_spool:
            messagebox.showinfo("No spool", "Scan a spool QR first.")
            return
        try:
            val = simpledialog.askfloat("Weigh Spool", "Enter current weight (g):", minvalue=0.0, maxvalue=5000.0)
            if val is None:
                return
            update_weight(self.current_spool["id"], float(val))
            self.current_spool = get_spool_by_url(self.current_spool["url"])
            self.refresh_labels()
            self.var_status.set("Weight updated.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_move(self):
        if not self.current_spool:
            messagebox.showinfo("No spool", "Scan a spool QR first.")
            return
        choices = [b["name"] for b in CFG.get("bins", [])] + ["AMS Slot 1", "AMS Slot 2", "AMS Slot 3", "AMS Slot 4", "Dryer", "Other"]
        dlg = ChoiceDialog(self, "Move To Location", "Select a new location:", choices)
        self.wait_window(dlg)
        if dlg.choice:
            update_location(self.current_spool["id"], dlg.choice)
            self.current_spool = get_spool_by_url(self.current_spool["url"])
            self.refresh_labels()
            self.var_status.set(f"Moved to: {dlg.choice}")

    def on_open(self):
        if not self.current_url:
            messagebox.showinfo("No URL", "Scan a spool QR first.")
            return
        url = self.current_url
        browser_bin = CFG.get("browser")
        from shutil import which
        if browser_bin and which(browser_bin):
            try:
                os.system(f"{browser_bin} '{url}' &")
                return
            except Exception:
                pass
        webbrowser.open(url)

    def manual_url(self):
        url = simpledialog.askstring("Manual URL", "Paste a spool URL or short link:")
        if url:
            self.handle_spool_scan(url.strip(), time.time())

    def show_locations(self):
        lines = []
        for loc in CFG.get("locations", []):
            lines.append(f"{loc.get('name')}: {loc.get('qr')}")
        if not lines:
            lines = ["(No locations configured yet)"]
        messagebox.showinfo("Location QRs", "\n".join(lines))

    def on_quit(self):
        if messagebox.askokcancel("Quit", "Exit Filament Station?"):
            try:
                self.scanner.stop()
            except Exception:
                pass
            self.destroy()

class ChoiceDialog(tk.Toplevel):
    def __init__(self, parent, title, prompt, choices):
        super().__init__(parent)
        self.title(title)
        self.choice = None
        ttk.Label(self, text=prompt).pack(padx=8, pady=6)
        self.lb = tk.Listbox(self, height=min(10, len(choices)))
        for c in choices:
            self.lb.insert(tk.END, c)
        self.lb.pack(padx=8, pady=6, fill="both", expand=True)
        btns = ttk.Frame(self)
        btns.pack(pady=6)
        ttk.Button(btns, text="OK", command=self.ok).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Cancel", command=self.cancel).grid(row=0, column=1, padx=6)
        self.lb.bind("<Double-1>", lambda e: self.ok())

    def ok(self):
        idx = self.lb.curselection()
        if idx:
            self.choice = self.lb.get(idx[0])
        self.destroy()

    def cancel(self):
        self.choice = None
        self.destroy()

def main():
    init_db()
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
