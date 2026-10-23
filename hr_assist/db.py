from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS roles(
 id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,is_sales INTEGER NOT NULL,
 current_version TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jd_versions(
 id TEXT PRIMARY KEY,role_id TEXT NOT NULL REFERENCES roles(id),text TEXT NOT NULL,
 fields TEXT NOT NULL,criteria TEXT NOT NULL,approved INTEGER NOT NULL,missing TEXT NOT NULL,
 source_file_id TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
 id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,
 relative_path TEXT NOT NULL UNIQUE,size INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS file_hash ON files(sha256,kind);
CREATE TABLE IF NOT EXISTS documents(
 file_id TEXT PRIMARY KEY REFERENCES files(id),status TEXT NOT NULL,spans TEXT NOT NULL,
 error TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications(
 id TEXT PRIMARY KEY,file_id TEXT NOT NULL REFERENCES files(id),role_id TEXT REFERENCES roles(id),
 name TEXT NOT NULL,original_name TEXT NOT NULL,status TEXT NOT NULL,assessment_id TEXT,
 recommended_at TEXT,created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS application_role ON applications(file_id,ifnull(role_id,''));
CREATE TABLE IF NOT EXISTS assessments(
 id TEXT PRIMARY KEY,application_id TEXT NOT NULL REFERENCES applications(id),jd_version_id TEXT NOT NULL,
 cache_key TEXT NOT NULL UNIQUE,status TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS followups(
 application_id TEXT PRIMARY KEY REFERENCES applications(id),cells TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workbook_exports(
 id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,file_id TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback_imports(
 id TEXT PRIMARY KEY,export_id TEXT NOT NULL,file_id TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mail_events(
 id TEXT PRIMARY KEY,mailbox TEXT NOT NULL,uid_validity TEXT NOT NULL,mail_uid TEXT NOT NULL,
 message_id TEXT,received_at TEXT NOT NULL,status TEXT NOT NULL,
 UNIQUE(mailbox,uid_validity,mail_uid));
CREATE TABLE IF NOT EXISTS receipts(
 id TEXT PRIMARY KEY,mail_event_id TEXT REFERENCES mail_events(id),sha256 TEXT NOT NULL,
 file_id TEXT,application_id TEXT,status TEXT NOT NULL,UNIQUE(mail_event_id,sha256));
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY,trigger TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,
 window_start TEXT,window_end TEXT,stats TEXT NOT NULL,error TEXT,
 started_at TEXT NOT NULL,finished_at TEXT);
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY,run_id TEXT,application_id TEXT,level TEXT NOT NULL,code TEXT NOT NULL,
 message TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
 id TEXT PRIMARY KEY,run_id TEXT,severity TEXT NOT NULL,message TEXT NOT NULL,
 read INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
"""


class Database:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "hr-assist.db"
        with self.connect() as con:
            con.executescript(SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")

    @contextmanager
    def connect(self, write=False):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def all(self, sql, args=()):
        with self.connect() as con:
            return [dict(row) for row in con.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect(True) as con:
            con.execute(sql, args)

    def get(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def put(self, key, value):
        self.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dump(value)))

    def event(self, code, message, *, run_id=None, application_id=None, level="info"):
        record = (uid(), run_id, application_id, level, code, message, now())
        self.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", record)
        directory = self.path.parent / "logs"
        directory.mkdir(exist_ok=True)
        with (directory / f"{record[-1][:10]}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(dump(dict(zip(("id","run_id","application_id","level","code","message","created_at"),record))) + "\n")

    def notify(self, message, severity="info", run_id=None):
        self.execute("INSERT INTO notifications VALUES (?,?,?,?,0,?)", (uid(), run_id, severity, message, now()))
