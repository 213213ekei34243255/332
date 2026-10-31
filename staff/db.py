"""Tiny database layer: Postgres when DATABASE_URL is set (Render), SQLite otherwise.

Queries are written once with `?` placeholders; they are translated to `%s`
for Postgres. JSON is stored as TEXT so the same schema works on both.
"""
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

# Every table is created as staff_<name> so this system can safely share a Postgres database (DATABASE_URL)
# with the existing app. Code below uses the short logical names; `_sql` adds the prefix.
TABLES = ("positions", "applications", "enrollments", "emails", "interviews", "turns", "recordings", "audit_log")
_TABLE_RE = re.compile(r"\b(FROM|JOIN|INTO|UPDATE|TABLE(?: IF NOT EXISTS)?|ON)\s+(" + "|".join(TABLES) + r")\b")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS positions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        department TEXT,
        location TEXT,
        employment_type TEXT,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        config TEXT NOT NULL DEFAULT '{}',
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS applications (
        id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL,
        full_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT,
        profile TEXT NOT NULL DEFAULT '{}',
        resume_file TEXT,
        resume_mime TEXT,
        resume_text TEXT,
        eligibility TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'submitted',
        decision TEXT,
        admin_notes TEXT,
        ip TEXT,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS staff_idx_app_position ON applications(position_id)",
    "CREATE INDEX IF NOT EXISTS staff_idx_app_email ON applications(email)",
    """CREATE TABLE IF NOT EXISTS enrollments (
        id TEXT PRIMARY KEY,
        application_id TEXT NOT NULL,
        code TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'issued',
        expires_at BIGINT NOT NULL,
        created_at BIGINT NOT NULL,
        created_by TEXT,
        opened_at BIGINT,
        face_verified_at BIGINT,
        used_at BIGINT,
        face_attempts INTEGER NOT NULL DEFAULT 0,
        meta TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS staff_idx_enr_app ON enrollments(application_id)",
    """CREATE TABLE IF NOT EXISTS emails (
        id TEXT PRIMARY KEY,
        application_id TEXT,
        enrollment_id TEXT,
        kind TEXT NOT NULL,
        to_addr TEXT NOT NULL,
        subject TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at BIGINT NOT NULL,
        sent_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS interviews (
        id TEXT PRIMARY KEY,
        enrollment_id TEXT NOT NULL,
        application_id TEXT NOT NULL,
        position_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'in_progress',
        state TEXT NOT NULL DEFAULT '{}',
        face TEXT NOT NULL DEFAULT '{}',
        events TEXT NOT NULL DEFAULT '[]',
        evaluation TEXT,
        overall_score INTEGER,
        recommendation TEXT,
        resume_count INTEGER NOT NULL DEFAULT 0,
        started_at BIGINT NOT NULL,
        ended_at BIGINT,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS staff_idx_iv_app ON interviews(application_id)",
    """CREATE TABLE IF NOT EXISTS turns (
        id TEXT PRIMARY KEY,
        interview_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        role TEXT NOT NULL,
        body TEXT NOT NULL,
        meta TEXT NOT NULL DEFAULT '{}',
        created_at BIGINT NOT NULL,
        UNIQUE (interview_id, idx)
    )""",
    """CREATE TABLE IF NOT EXISTS recordings (
        id TEXT PRIMARY KEY,
        interview_id TEXT NOT NULL,
        segment INTEGER NOT NULL DEFAULT 0,
        filename TEXT NOT NULL,
        mime TEXT NOT NULL,
        size BIGINT NOT NULL DEFAULT 0,
        duration_ms BIGINT NOT NULL DEFAULT 0,
        chunks INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'uploading',
        created_at BIGINT NOT NULL,
        finalized_at BIGINT,
        UNIQUE (interview_id, segment)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        ts BIGINT NOT NULL,
        actor TEXT,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT,
        ip TEXT
    )""",
]

# columns that hold JSON text, per table -> parsed automatically on read
JSON_COLUMNS = {
    "positions": ("config",),
    "applications": ("profile", "eligibility"),
    "enrollments": ("meta",),
    "interviews": ("state", "face", "events", "evaluation"),
    "turns": ("meta",),
}


def now_ms():
    return int(time.time() * 1000)


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def jdump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def jload(value, default=None):
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.is_pg = bool(settings.database_url)
        self._pool = None
        self._lock = threading.Lock()
        if self.is_pg:
            import psycopg2.pool  # imported lazily: SQLite-only dev needs no driver
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                1, 8, settings.database_url, sslmode=settings.pg_sslmode,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
            )
        else:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # -- connections ----------------------------------------------------
    @contextmanager
    def _connect(self):
        if self.is_pg:
            conn = self._pool.getconn()
            broken = False
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    broken = True
                raise
            finally:
                self._pool.putconn(conn, close=broken or bool(conn.closed))
        else:
            conn = sqlite3.connect(str(self.settings.sqlite_path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _sql(self, sql):
        sql = _TABLE_RE.sub(lambda m: f"{m.group(1)} staff_{m.group(2)}", sql)
        return sql.replace("?", "%s") if self.is_pg else sql

    def _cursor(self, conn):
        if self.is_pg:
            import psycopg2.extras
            return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return conn.cursor()

    # -- public API -----------------------------------------------------
    def init_schema(self):
        with self._lock, self._connect() as conn:
            cur = self._cursor(conn)
            for stmt in SCHEMA:
                cur.execute(_TABLE_RE.sub(lambda m: f"{m.group(1)} staff_{m.group(2)}", stmt))

    def _with_conn(self, fn):
        """Run fn(conn); on a dropped Postgres connection (idle timeout, failover) retry once on a fresh one."""
        for attempt in (0, 1):
            try:
                with self._connect() as conn:
                    return fn(conn)
            except Exception as exc:
                if self.is_pg and attempt == 0 and type(exc).__name__ in ("OperationalError", "InterfaceError"):
                    continue
                raise

    def run(self, sql, params=()):
        def go(conn):
            cur = self._cursor(conn)
            cur.execute(self._sql(sql), tuple(params))
            return cur.rowcount
        return self._with_conn(go)

    def all(self, sql, params=(), table=None):
        def go(conn):
            cur = self._cursor(conn)
            cur.execute(self._sql(sql), tuple(params))
            return [dict(r) for r in cur.fetchall()]
        return [self._decode(r, table) for r in self._with_conn(go)]

    def one(self, sql, params=(), table=None):
        rows = self.all(sql, params, table)
        return rows[0] if rows else None

    def insert(self, table, values):
        values = dict(values)
        for col in JSON_COLUMNS.get(table, ()):
            if col in values and not isinstance(values[col], (str, type(None))):
                values[col] = jdump(values[col])
        cols = ", ".join(values.keys())
        marks = ", ".join("?" for _ in values)
        self.run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))

    def update(self, table, row_id, values):
        values = dict(values)
        for col in JSON_COLUMNS.get(table, ()):
            if col in values and not isinstance(values[col], (str, type(None))):
                values[col] = jdump(values[col])
        sets = ", ".join(f"{k} = ?" for k in values)
        self.run(f"UPDATE {table} SET {sets} WHERE id = ?", list(values.values()) + [row_id])

    def get(self, table, row_id):
        return self.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,), table)

    def delete(self, table, row_id):
        return self.run(f"DELETE FROM {table} WHERE id = ?", (row_id,))

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _decode(row, table):
        for col in JSON_COLUMNS.get(table, ()):
            if col in row:
                default = [] if col == "events" else {}
                row[col] = jload(row[col], None if col == "evaluation" else default)
        return row

    def audit(self, actor, action, target="", detail="", ip=""):
        try:
            self.insert("audit_log", {
                "id": new_id("aud"), "ts": now_ms(), "actor": actor, "action": action,
                "target": target, "detail": detail[:500], "ip": ip,
            })
        except Exception:  # auditing must never break the request
            pass
