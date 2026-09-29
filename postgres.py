"""PostgreSQL DB-API bridge for the application's bounded SQL dialect.

Read statements use autocommit. Every write transaction obtains the same database
advisory lock BEFORE reads, matching SQLite BEGIN IMMEDIATE across worker processes.
Sequences are deliberately nontransactional, so failed operations never reuse codes.
"""
import os
from pathlib import Path
import re
import sqlite3

LOCK_ID = 734901820


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [c.name for c in cursor.description]
    return lambda values: Row(zip(names, values))


class Cursor:
    def __init__(self, cursor, inserted=False):
        self.cursor = cursor
        self.lastrowid = None
        if inserted:
            row = cursor.fetchone()
            self.lastrowid = row[0] if row else None

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)

    @property
    def rowcount(self):
        return self.cursor.rowcount


def translate(sql):
    sql = sql.strip().rstrip(';')
    # Leave SQL literals unchanged, including literal '?' and '%'.
    parts = re.split("('(?:''|[^'])*')", sql)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r'\bIS\s+\?', 'IS NOT DISTINCT FROM ?', parts[i], flags=re.I)
        parts[i] = parts[i].replace('?', '%s')
        parts[i] = re.sub(r'\bLIKE\b', 'ILIKE', parts[i], flags=re.I)
    sql = ''.join(parts)
    sql = re.sub(r'([\w.]+)\s+COLLATE\s+NOCASE', r'lower(\1)', sql, flags=re.I)
    sql = re.sub(r'GROUP_CONCAT\(([^,]+),\s*(\x27[^\x27]*\x27)\)', r'STRING_AGG(CAST(\1 AS TEXT), \2)', sql, flags=re.I)
    sql = re.sub(r'HAVING stock\b', 'HAVING COALESCE(SUM(l.quantity),0)', sql, flags=re.I)
    if re.match(r'INSERT OR REPLACE INTO password_resets', sql, re.I):
        sql = re.sub('INSERT OR REPLACE', 'INSERT', sql, flags=re.I)
        sql += ' ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at,requested_at=excluded.requested_at,attempts=excluded.attempts,organization_id=excluded.organization_id'
    if re.match('INSERT OR IGNORE', sql, re.I):
        sql = re.sub('INSERT OR IGNORE', 'INSERT', sql, flags=re.I) + ' ON CONFLICT DO NOTHING'
    return sql


class Connection:
    dialect = 'postgresql'

    def __init__(self, url):
        import psycopg
        self.raw = psycopg.connect(url, autocommit=True, row_factory=row_factory, connect_timeout=10)
        self.raw.execute("SET statement_timeout = '30s'")
        self.raw.execute("SET lock_timeout = '15s'")
        self.id_tables = {r[0] for r in self.raw.execute("SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='id' AND is_identity='YES'")}

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.raw.info.transaction_status != TransactionStatus.IDLE

    def begin(self):
        if not self.in_transaction:
            self.raw.execute('BEGIN')
            self.raw.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))

    def execute(self, sql, params=()):
        import psycopg
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            self.begin()
            return Cursor(self.raw.execute('SELECT 1'))
        if re.match(r'\s*(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)', sql, re.I):
            self.begin()
        statement = translate(sql)
        match = re.match(r'INSERT INTO\s+(\w+)', statement, re.I)
        inserted = bool(match and match[1] in self.id_tables and 'RETURNING' not in statement.upper())
        if inserted:
            statement += ' RETURNING id'
        try:
            return Cursor(self.raw.execute(statement, tuple(int(x) if isinstance(x, bool) else x for x in params) or None), inserted)
        except psycopg.IntegrityError:
            # Existing API handlers already map this category to HTTP 409.
            raise sqlite3.IntegrityError('Integrity constraint violated') from None

    def next_number(self, sequence):
        if sequence not in ('lot_code_sequence', 'order_number_sequence'):
            raise ValueError('Invalid sequence')
        return self.raw.execute('SELECT nextval(%s)', (sequence,)).fetchone()[0]

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def connect():
    return Connection(os.environ['DATABASE_URL'])


def initialize():
    import psycopg
    from datetime import datetime, timezone
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as db:
        db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
        if not db.execute('SELECT 1 FROM schema_migrations WHERE version=1').fetchone():
            if db.execute("SELECT to_regclass('public.users')").fetchone()[0]:
                raise RuntimeError('Banco existente sem migração reconhecida. Migração manual necessária.')
            db.execute((Path(__file__).parent / 'migrations' / '001_postgresql.sql').read_text(encoding='utf-8'))
            db.execute("INSERT INTO organizations(id,name,slug,active,created_at) VALUES(1,'Organização padrão','organizacao-padrao',1,%s)", (datetime.now(timezone.utc).isoformat(timespec='seconds'),))
            db.execute("SELECT setval(pg_get_serial_sequence('organizations','id'),1,true)")
            db.execute('INSERT INTO schema_migrations VALUES(1)')
