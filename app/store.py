"""SQLite persistence. Message/task/credential payloads are encrypted at rest.

IDs, scheduling timestamps and status indexes remain plaintext. The local key
is separate from the database; protecting the machine remains essential.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        key_path = self.directory / "vault.key"
        if not key_path.exists():
            if (self.directory / "relay.sqlite3").exists():
                raise RuntimeError("Database exists but vault.key is missing. Restore the original key.")
            with key_path.open("xb") as stream:
                stream.write(Fernet.generate_key())
            os.chmod(key_path, 0o600)
        self.cipher = Fernet(key_path.read_bytes().strip())
        self.path = self.directory / "relay.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(source, external_id));
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, message_id TEXT, start TEXT NOT NULL,
                    end TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_task_per_message ON tasks(message_id)
                    WHERE message_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY, task_id TEXT, due TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    reminder_id TEXT, channel TEXT, attempts INTEGER DEFAULT 0,
                    delivered INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
                    PRIMARY KEY(reminder_id, channel));
                CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS busy_blocks (
                    id TEXT PRIMARY KEY, external_key TEXT UNIQUE NOT NULL,
                    start TEXT NOT NULL, end TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            if "item_id" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN item_id TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_task_per_item ON tasks(item_id) WHERE item_id IS NOT NULL")

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def encrypt(self, obj):
        return self.cipher.encrypt(json.dumps(obj, ensure_ascii=False).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return self.decrypt(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, self.encrypt(value)))

    def delete(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM kv WHERE key=?", (key,))

    def messages(self):
        with self.connect() as db:
            return [self.decrypt(r[0]) for r in db.execute(
                "SELECT payload FROM messages ORDER BY created_at DESC LIMIT 1000")]

    def tasks(self):
        with self.connect() as db:
            return [dict(self.decrypt(r["payload"]), done=bool(r["done"])) for r in db.execute(
                "SELECT * FROM tasks ORDER BY start")]

    def items(self):
        with self.connect() as db:
            return [self.decrypt(row[0]) for row in db.execute("SELECT payload FROM items ORDER BY updated_at DESC LIMIT 2000")]

    def blocks(self):
        with self.connect() as db:
            return [self.decrypt(row[0]) for row in db.execute("SELECT payload FROM busy_blocks ORDER BY start LIMIT 5000")]
