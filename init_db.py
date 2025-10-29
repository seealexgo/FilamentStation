#!/usr/bin/env python3
import os, sqlite3
DB_PATH = os.environ.get("FS_DB_PATH", os.path.expanduser("~/.filament_station/filaments.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS spools (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, name TEXT, material TEXT, color TEXT, location TEXT, last_weight_g REAL, last_updated TEXT);")
cur.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, spool_id INTEGER, timestamp TEXT, action TEXT, weight_g REAL, location TEXT, note TEXT, FOREIGN KEY(spool_id) REFERENCES spools(id) ON DELETE CASCADE);")
conn.commit(); conn.close()
print(f'Initialized DB at: {DB_PATH}')
