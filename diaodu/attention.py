"""Aggregate attention, with salted identifiers and no raw IP storage."""
import sqlite3, secrets, hashlib, time
from contextlib import contextmanager
from datetime import datetime, timezone
class Attention:
 def __init__(self,root):
  self.path=root/'attention.sqlite3'
  with self.db() as db:
   db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT)')
   db.execute("INSERT OR IGNORE INTO settings VALUES ('secret',?)",(secrets.token_hex(32),))
   db.execute("INSERT OR IGNORE INTO settings VALUES ('started',?)",(datetime.now(timezone.utc).isoformat(),))
   self.secret=db.execute("SELECT value FROM settings WHERE key='secret'").fetchone()[0]
   self.started=db.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0]
   db.execute('CREATE TABLE IF NOT EXISTS visitors (task TEXT,channel TEXT,visitor TEXT,hits INTEGER,last REAL,PRIMARY KEY(task,channel,visitor))')
 @contextmanager
 def db(self):
  db=sqlite3.connect(self.path,timeout=10)
  try:
   with db:yield db
  finally:db.close()
 def record(self,task,channel,visitor,at=None):
  at=time.time() if at is None else at
  key=hashlib.sha256((self.secret+channel+visitor).encode()).hexdigest()
  with self.db() as db:
   db.execute('INSERT INTO visitors VALUES (?,?,?,1,?) ON CONFLICT(task,channel,visitor) DO UPDATE SET hits=hits+1,last=excluded.last WHERE excluded.last-visitors.last>=?',(task,channel,key,at,1800 if channel=='browser' else 0))
 def stats(self,task):
  with self.db() as db:rows=db.execute('SELECT channel,SUM(hits),COUNT(*) FROM visitors WHERE task=? GROUP BY channel',(task,)).fetchall()
  values={c:(h,u) for c,h,u in rows}
  return dict(page_views=values.get('browser',(0,0))[0],unique_browsers=values.get('browser',(0,0))[1],api_reads=values.get('api',(0,0))[0],unique_api_sources=values.get('api',(0,0))[1],started_at=self.started)
