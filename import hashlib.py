import hashlib
import base64
import binascii
import struct
import zlib
import hmac
import json
import os
import secrets
import sqlite3
import smtplib
import ssl
import sys
import threading
import functools
import logging
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from calendar import monthrange
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlencode


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor_python"))
sys.path.insert(0, str(ROOT))
import settings
PUBLIC = ROOT / "public"
DATA = settings.data_directory()
DATABASE = settings.sqlite_path()
BACKUPS = DATA / "backups"
DATABASE_LOCK = threading.RLock()
SESSION_DAYS = 8
COOKIE_NAME = "farmacia_session"
ROLES = {"Administrador", "Administrador da Organização", "Enfermeiro", "Funcionário da Farmácia", "Consulta"}
DEFAULT_ORGANIZATION = "Organização padrão"
TENANT_TABLES = ("requisitions", "audit_log", "quick_exit_requests", "units", "users", "user_units",
                 "sessions", "password_resets", "medicines", "lots", "movements", "inventory_counts",
                 "transfers", "transfer_events", "generated_pdf_files", "organization_settings")
TRANSFER_PENDING = "Pendente de Recebimento"
TRANSFER_RECEIVED = "Recebida"
TRANSFER_DIVERGENCE = "Divergência"
TRANSFER_REFUSED = "Recusada"
TRANSFER_CANCELLED = "Cancelada"
TRANSFER_REVERSED = "Estornada"


import catalog_orders
import notifications
from catalog_orders import ApiError


def connect_db():
    if DATABASE is None:
        from postgres import connect
        return connect()
    DATA.mkdir(parents=True, exist_ok=True)
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, encoded):
    try:
        salt_hex, expected = encoded.split(":", 1)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate_organization_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS organizations (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)""")
    db.execute("INSERT OR IGNORE INTO organizations(id,name,slug,created_at) VALUES(1,?,?,?)",
               (DEFAULT_ORGANIZATION, "organizacao-padrao", now_iso()))
    db.execute("""CREATE TABLE IF NOT EXISTS organization_settings (
        organization_id INTEGER NOT NULL REFERENCES organizations(id), setting_key TEXT NOT NULL,
        setting_value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY(organization_id, setting_key))""")
    organization_id = db.execute("SELECT id FROM organizations WHERE slug='organizacao-padrao'").fetchone()[0]
    user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "is_superadmin" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN is_superadmin INTEGER NOT NULL DEFAULT 0")
    for table in TENANT_TABLES:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "organization_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN organization_id INTEGER REFERENCES organizations(id)")
        if table == "users":
            db.execute("UPDATE users SET organization_id=? WHERE organization_id IS NULL AND is_superadmin=0", (organization_id,))
            db.execute("UPDATE users SET organization_id=NULL WHERE is_superadmin=1")
        else:
            db.execute(f"UPDATE {table} SET organization_id=? WHERE organization_id IS NULL", (organization_id,))
    db.execute("""CREATE TABLE IF NOT EXISTS generated_pdf_files(
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, unit_id INTEGER NOT NULL,
        expires_at TEXT NOT NULL, content BLOB NOT NULL, organization_id INTEGER REFERENCES organizations(id))""")
    legacy_global_unit_name = False
    for index in db.execute("PRAGMA index_list(units)"):
        if index[2]:
            columns = [column[2] for column in db.execute(f'PRAGMA index_info("{index[1]}")')]
            if columns == ["name"]:
                legacy_global_unit_name = True
                break
    if legacy_global_unit_name:
        db.commit()
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE units_rebuilt(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                organization_id INTEGER REFERENCES organizations(id))""")
            db.execute("INSERT INTO units_rebuilt SELECT id,name,created_at,organization_id FROM units")
            db.execute("DROP TABLE units")
            db.execute("ALTER TABLE units_rebuilt RENAME TO units")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_units_organization_name ON units(organization_id,name)")
    for table in ("units", "users", "medicines", "lots", "movements", "sessions"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_organization ON {table}(organization_id)")


def initialize_db():
    if DATABASE is None:
        from postgres import initialize
        initialize()
        db = connect_db()
        try:
            bootstrap_accounts(db)
            db.commit()
        finally:
            db.close()
        return
    db = connect_db()
    try:
        _initialize_sqlite(db)
    finally:
        db.close()


def _initialize_sqlite(db):
    existing_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "organizations" not in existing_tables and "units" in existing_tables and db.execute("SELECT 1 FROM units LIMIT 1").fetchone():
        backup_name = backup_database()
        print(f"Backup pré-migração criado: {backup_name}", file=sys.stderr)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS organization_settings (
            organization_id INTEGER NOT NULL REFERENCES organizations(id), setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
            PRIMARY KEY(organization_id, setting_key)
        );
        CREATE TABLE IF NOT EXISTS requisitions (
            id INTEGER PRIMARY KEY, requester_unit_id INTEGER NOT NULL REFERENCES units(id),
            supplier_unit_id INTEGER NOT NULL REFERENCES units(id), medicine_id INTEGER NOT NULL REFERENCES medicines(id),
            quantity INTEGER NOT NULL CHECK(quantity>0), reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pendente',
            created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
            decided_by INTEGER REFERENCES users(id), decided_at TEXT, decision_reason TEXT NOT NULL DEFAULT '',
            transfer_id INTEGER UNIQUE REFERENCES transfers(id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY, actor TEXT NOT NULL, entity TEXT NOT NULL,
            entity_id INTEGER NOT NULL, changes TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quick_exit_requests (
            request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, unit_id INTEGER NOT NULL,
            payload TEXT NOT NULL, result TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('Administrador','Enfermeiro','Funcionário da Farmácia','Consulta')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_units (
            user_id INTEGER NOT NULL REFERENCES users(id),
            unit_id INTEGER NOT NULL REFERENCES units(id),
            PRIMARY KEY(user_id, unit_id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            csrf_token TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_security (
            username TEXT PRIMARY KEY,
            failures INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS password_resets (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS medicines (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            active_ingredient TEXT NOT NULL DEFAULT '',
            concentration TEXT NOT NULL DEFAULT '',
            dosage_form TEXT NOT NULL DEFAULT '',
            presentation TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL DEFAULT 'unidade',
            package_quantity REAL,
            package_unit TEXT NOT NULL DEFAULT '',
            stock_unit TEXT NOT NULL DEFAULT 'Unidade',
            administration_route TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Geral',
            code TEXT NOT NULL UNIQUE,
            manufacturer_barcode TEXT NOT NULL DEFAULT '',
            minimum_stock INTEGER NOT NULL DEFAULT 0 CHECK(minimum_stock >= 0),
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lots (
            id INTEGER PRIMARY KEY,
            unit_id INTEGER NOT NULL REFERENCES units(id),
            medicine_id INTEGER NOT NULL REFERENCES medicines(id),
            lot_number TEXT NOT NULL,
            internal_code TEXT NOT NULL UNIQUE,
            manufacture_date TEXT,
            expiration_date TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
            created_at TEXT NOT NULL,
            UNIQUE(unit_id, medicine_id, lot_number),
            UNIQUE(id, unit_id)
        );
        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY,
            unit_id INTEGER NOT NULL REFERENCES units(id),
            medicine_id INTEGER NOT NULL REFERENCES medicines(id),
            lot_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL CHECK(kind IN ('Entrada','Saída','Ajuste')),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            stock_before INTEGER NOT NULL,
            stock_after INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            destination TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            document TEXT NOT NULL DEFAULT '',
            responsible TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(lot_id, unit_id) REFERENCES lots(id, unit_id)
        );
        CREATE TABLE IF NOT EXISTS inventory_counts (
            id INTEGER PRIMARY KEY,
            unit_id INTEGER NOT NULL REFERENCES units(id),
            lot_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            system_quantity INTEGER NOT NULL,
            counted_quantity INTEGER NOT NULL CHECK(counted_quantity >= 0),
            difference INTEGER NOT NULL,
            reason TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(lot_id, unit_id) REFERENCES lots(id, unit_id)
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY,
            origin_unit_id INTEGER NOT NULL REFERENCES units(id),
            destination_unit_id INTEGER NOT NULL REFERENCES units(id),
            medicine_id INTEGER NOT NULL REFERENCES medicines(id),
            origin_lot_id INTEGER NOT NULL REFERENCES lots(id),
            destination_lot_id INTEGER REFERENCES lots(id),
            lot_number TEXT NOT NULL,
            expiration_date TEXT NOT NULL,
            quantity_sent INTEGER NOT NULL CHECK(quantity_sent > 0),
            quantity_received INTEGER CHECK(quantity_received >= 0),
            status TEXT NOT NULL CHECK(status IN ('Pendente de Recebimento','Recebida','Divergência','Recusada','Cancelada','Estornada')),
            responsible TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            refused_by INTEGER REFERENCES users(id),
            refused_at TEXT,
            refusal_reason TEXT NOT NULL DEFAULT '',
            received_by INTEGER REFERENCES users(id),
            received_at TEXT,
            discrepancy_reported_by INTEGER REFERENCES users(id),
            discrepancy_reported_at TEXT,
            discrepancy_reason TEXT NOT NULL DEFAULT '',
            discrepancy_notes TEXT NOT NULL DEFAULT '',
            resolved_by INTEGER REFERENCES users(id),
            resolved_at TEXT,
            resolution_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS transfer_events (
            id INTEGER PRIMARY KEY,
            transfer_id INTEGER NOT NULL REFERENCES transfers(id),
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            reason TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generated_pdf_files (
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, unit_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL, content BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lots_unit_expiry ON lots(unit_id, expiration_date);
        CREATE INDEX IF NOT EXISTS idx_movements_unit_date ON movements(unit_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_movements_medicine ON movements(medicine_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_transfers_destination_status ON transfers(destination_unit_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_transfers_origin_status ON transfers(origin_unit_id, status, created_at);
    """)
    migrate_organization_schema(db)
    if "exit_category" not in {r["name"] for r in db.execute("PRAGMA table_info(movements)")}:
        db.execute("ALTER TABLE movements ADD COLUMN exit_category TEXT NOT NULL DEFAULT 'Não classificado'")
    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    if "email" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    if "profile_photo" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT NOT NULL DEFAULT ''")
    medicine_columns = {row["name"] for row in db.execute("PRAGMA table_info(medicines)")}
    migrations = {
        "package_quantity": "ALTER TABLE medicines ADD COLUMN package_quantity REAL",
        "package_unit": "ALTER TABLE medicines ADD COLUMN package_unit TEXT NOT NULL DEFAULT ''",
        "stock_unit": "ALTER TABLE medicines ADD COLUMN stock_unit TEXT NOT NULL DEFAULT 'Unidade'",
        "administration_route": "ALTER TABLE medicines ADD COLUMN administration_route TEXT NOT NULL DEFAULT ''",
    }
    for column, statement in migrations.items():
        if column not in medicine_columns:
            db.execute(statement)
    if "stock_unit" not in medicine_columns:
        db.execute("""UPDATE medicines SET stock_unit = CASE lower(trim(unit))
            WHEN 'cx' THEN 'Caixa' WHEN 'caixa' THEN 'Caixa'
            WHEN 'un' THEN 'Unidade' WHEN 'unidade' THEN 'Unidade'
            WHEN 'blister' THEN 'Blister' WHEN 'cartela' THEN 'Cartela'
            WHEN 'frasco' THEN 'Frasco' WHEN 'ampola' THEN 'Ampola'
            WHEN 'bisnaga' THEN 'Bisnaga' WHEN 'sachê' THEN 'Sachê'
            ELSE COALESCE(NULLIF(trim(unit), ''), 'Unidade') END""")
    bootstrap_accounts(db)
    catalog_orders.migrate(db)
    notifications.migrate(db)
    db.commit()
    reserve_lot_code(db)
    db.close()


def bootstrap_accounts(db):
    if getattr(db, 'dialect', '') == 'postgresql':
        db.begin()
    organization_id = db.execute("SELECT id FROM organizations WHERE slug='organizacao-padrao'").fetchone()[0]
    if not db.execute("SELECT 1 FROM units LIMIT 1").fetchone():
        db.execute("INSERT INTO units(name, created_at, organization_id) VALUES (?, ?, ?)",
               ("USF Santa Maria do Bacuri", now_iso(), organization_id))
    admin_username = (os.environ.get("FARMACIA_ADMIN_USERNAME") or "admin").strip().lower()
    admin_password = os.environ.get("FARMACIA_ADMIN_PASSWORD", "")
    existing_admin = db.execute("SELECT id, username, password_hash FROM users WHERE lower(username) = ?", (admin_username,)).fetchone()

    if not existing_admin and not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        if not 12 <= len(admin_password) <= 256:
            raise ValueError("Configure ADMIN_PASSWORD com 12 a 256 caracteres para a primeira inicialização.")
        unit_id = db.execute("SELECT id FROM units ORDER BY id LIMIT 1").fetchone()["id"]
        cursor = db.execute(
            "INSERT INTO users(username, full_name, password_hash, role, created_at, organization_id) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_username, "Administrador da UBS", password_hash(admin_password), "Administrador", now_iso(), organization_id),
        )
        db.execute("INSERT INTO user_units(user_id, unit_id, organization_id) VALUES (?, ?, ?)",
               (cursor.lastrowid, unit_id, organization_id))
        print("Conta inicial criada. Consulte as credenciais configuradas no ambiente.", file=sys.stderr)
    superadmin_username = os.environ.get("FARMACIA_SUPERADMIN_USERNAME", "").strip().lower()
    superadmin_password = os.environ.get("FARMACIA_SUPERADMIN_PASSWORD", "")
    if bool(superadmin_username) != bool(superadmin_password):
        raise ValueError("Configure FARMACIA_SUPERADMIN_USERNAME e FARMACIA_SUPERADMIN_PASSWORD juntos.")
    if superadmin_username:
        if superadmin_username == admin_username.lower() or not 12 <= len(superadmin_password) <= 256:
            raise ValueError("Credenciais do SuperAdmin inválidas ou iguais às do administrador da organização.")
        superadmin = db.execute("SELECT id,is_superadmin FROM users WHERE lower(username)=?", (superadmin_username,)).fetchone()
        if superadmin:
            if not superadmin["is_superadmin"]:
                raise ValueError("O nome do SuperAdmin já pertence a outra conta. Escolha um nome exclusivo.")
        else:
            db.execute("""INSERT INTO users(username,full_name,password_hash,role,active,created_at,is_superadmin)
                VALUES(?, 'SuperAdmin', ?, 'Administrador', 1, ?, 1)""",
                (superadmin_username, password_hash(superadmin_password), now_iso()))


def backup_database():
    if DATABASE is None:
        raise ApiError(409, "Use o backup PostgreSQL administrado pelo servidor (pg_dump). Consulte o guia de produção.")
    with DATABASE_LOCK:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("farmacia-%Y%m%d-%H%M%S-%f.sqlite3")
        target = BACKUPS / name
        source = connect_db()
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return name


def reserve_lot_code(db, medicine_id=None):
    if getattr(db, 'dialect', '') == 'postgresql':
        if medicine_id is None:
            return
        return f"UBS-{medicine_id:06d}-{db.next_number('lot_code_sequence'):06d}"
    # This ledger is intentionally outside the restored stock database.
    # Codes reserved before a failed entry or a restore are never issued again.
    ledger = sqlite3.connect(DATA / "lot_codes.sqlite3", timeout=10)
    try:
        ledger.execute("CREATE TABLE IF NOT EXISTS issued_codes(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE)")
        for row in db.execute("SELECT internal_code FROM lots WHERE internal_code<>'PENDING'"):
            ledger.execute("INSERT INTO issued_codes(code) SELECT ? WHERE NOT EXISTS(SELECT 1 FROM issued_codes WHERE code=?)", (row[0], row[0]))
        if medicine_id is None:
            ledger.commit()
            return
        while True:
            number = ledger.execute("INSERT INTO issued_codes(code) VALUES(NULL)").lastrowid
            code = f"UBS-{medicine_id:06d}-{number:06d}"
            if not ledger.execute("SELECT 1 FROM issued_codes WHERE code=?", (code,)).fetchone():
                ledger.execute("UPDATE issued_codes SET code=? WHERE id=?", (code, number))
                ledger.commit()
                return code
    finally:
        ledger.close()


def automatic_backup():
    while True:
        try:
            backup_database()
        except Exception as error:
            logging.getLogger("pharmacia").error("backup_failed error_type=%s", type(error).__name__)
        threading.Event().wait(24 * 60 * 60)


def audit_snapshot(db, entity, entity_id, organization_id):
    row = db.execute(f"SELECT * FROM {entity} WHERE id=? AND organization_id=?", (entity_id, organization_id)).fetchone()
    result = dict(row) if row else {}
    for key in ("password_hash", "profile_photo"):
        result.pop(key, None)
    if entity == "users" and row:
        result["unit_ids"] = [r[0] for r in db.execute("SELECT unit_id FROM user_units WHERE user_id=? AND organization_id=? ORDER BY unit_id", (entity_id, organization_id))]
    return result


def record_audit(db, actor, entity, entity_id, before, password_changed=False):
    organization_id = require_organization(actor)
    after = audit_snapshot(db, entity, entity_id, organization_id)
    changes = {key: {"antes": before.get(key), "depois": after.get(key)} for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)}
    if password_changed:
        changes["senha"] = {"antes": "oculta", "depois": "redefinida"}
    if changes:
        actor_name = db.execute("SELECT full_name FROM users WHERE id=?", (actor["id"],)).fetchone()[0]
        db.execute("INSERT INTO audit_log(actor,entity,entity_id,changes,created_at,organization_id) VALUES(?,?,?,?,?,?)",
               (actor_name, entity, entity_id, json.dumps(changes, ensure_ascii=False), now_iso(), require_organization(actor)))


def serialized_request(method):
    @functools.wraps(method)
    def wrapped(self):
        with DATABASE_LOCK:
            return method(self)
    return wrapped


def clean_text(value, label, limit=240, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, f"Campo inválido: {label}.")
    value = value.strip()
    if required and not value:
        raise ApiError(400, f"Informe {label}.")
    if len(value) > limit:
        raise ApiError(400, f"{label} excede o limite de {limit} caracteres.")
    return value


def positive_int(value, label, allow_zero=False):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} deve ser um número inteiro.")
    if number < (0 if allow_zero else 1) or number > 2_000_000_000:
        raise ApiError(400, f"{label} fora do intervalo permitido.")
    return number


def checked_date(value, label):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} deve estar no formato AAAA-MM-DD.")
    return parsed.isoformat()


def can_manage(user):
    return user["role"] in ("Administrador", "Administrador da Organização", "SuperAdmin", "Enfermeiro", "Funcionário da Farmácia")


def is_organization_admin(user):
    return user["role"] in ("Administrador", "Administrador da Organização", "SuperAdmin")


def require_organization(user):
    organization_id = user.get("organization_id")
    if not organization_id:
        raise ApiError(403, "Selecione uma organização para continuar.")
    return organization_id


def validate_profile_photo(value):
    if value == "":
        return ""
    prefix = "data:image/png;base64,"
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 400_000:
        raise ApiError(400, "Foto inválida. Selecione uma imagem pelo perfil.")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError()
        offset, compressed, dimensions, ended = 8, bytearray(), None, False
        while offset < len(raw):
            length, kind = struct.unpack(">I4s", raw[offset:offset + 8])
            end = offset + 8 + length
            data = raw[offset + 8:end]
            crc, = struct.unpack(">I", raw[end:end + 4])
            if zlib.crc32(kind + data) & 0xffffffff != crc:
                raise ValueError()
            if dimensions is None and kind != b"IHDR":
                raise ValueError()
            if kind == b"IHDR":
                if dimensions is not None:
                    raise ValueError()
                width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
                if not (1 <= width <= 256 and 1 <= height <= 256) or depth != 8 or color not in (2, 6) or any((compression, filtering, interlace)):
                    raise ValueError()
                dimensions = (width, height, 4 if color == 6 else 3)
            elif kind == b"IDAT":
                compressed.extend(data)
            elif kind == b"IEND":
                if length or end + 4 != len(raw):
                    raise ValueError()
                ended = True
                break
            offset = end + 4
        if not ended or dimensions is None:
            raise ValueError()
        width, height, channels = dimensions
        expected = height * (1 + width * channels)
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(bytes(compressed), expected + 1)
        if len(pixels) != expected or not decoder.eof or decoder.unused_data:
            raise ValueError()
        if any(pixels[i] > 4 for i in range(0, expected, 1 + width * channels)):
            raise ValueError()
    except (ValueError, binascii.Error, struct.error, zlib.error):
        raise ApiError(400, "Foto inválida. Use uma imagem PNG, JPEG ou WebP pelo perfil.")
    return prefix + base64.b64encode(raw).decode("ascii")


class PharmacyHandler(BaseHTTPRequestHandler):
    server_version = "FarmaciaUBS/1.0"

    def log_message(self, _format, *_args):
        return

    def end_headers(self):
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if settings.production():
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        super().end_headers()

    def expected_origin(self):
        return settings.public_origin() or f"{'https' if settings.secure_cookie() else 'http'}://{self.headers.get('Host')}"

    def send_json(self, status, payload, headers=None):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def send_error_json(self, error):
        self.send_json(error.status, {"error": error.message})

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Cabeçalho de tamanho inválido.")
        if length < 1 or length > 1_000_000:
            raise ApiError(413 if length > 1_000_000 else 400, "Corpo da solicitação inválido.")
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise ApiError(415, "Use conteúdo JSON.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "JSON inválido.")
        if not isinstance(payload, dict):
            raise ApiError(400, "O corpo deve ser um objeto JSON.")
        return payload

    def cookie_value(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""
        except Exception:
            return ""

    def get_user(self):
        token = self.cookie_value()
        if not token:
            raise ApiError(401, "Faça login para continuar.")
        db = connect_db()
        row = db.execute("""
                 SELECT u.id, u.username, u.full_name, u.role, u.profile_photo, u.organization_id,
                     u.is_superadmin, s.organization_id AS session_organization_id, s.csrf_token, s.expires_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND u.active = 1
        """, (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row["expires_at"] <= now_iso():
            if row:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
                db.commit()
            db.close()
            raise ApiError(401, "Sessão expirada. Entre novamente.")
        organization_id = row["session_organization_id"] if row["is_superadmin"] else row["organization_id"]
        if not row["is_superadmin"] and row["session_organization_id"] != organization_id:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
            db.commit()
            db.close()
            raise ApiError(401, "A organização desta sessão foi alterada. Entre novamente.")
        if organization_id and not db.execute("SELECT 1 FROM organizations WHERE id=? AND active=1", (organization_id,)).fetchone():
            db.close()
            raise ApiError(403, "Esta organização está inativa.")
        units = db.execute("""
            SELECT un.id, un.name FROM units un
            JOIN user_units uu ON uu.unit_id = un.id AND uu.organization_id = un.organization_id
            WHERE uu.user_id = ? AND un.organization_id = ? ORDER BY un.name
        """, (row["id"], organization_id)).fetchall() if organization_id else []
        if organization_id and (row["is_superadmin"] or row["role"] == "Administrador"):
            units = db.execute("SELECT id, name FROM units WHERE organization_id=? ORDER BY name", (organization_id,)).fetchall()
        db.close()
        role = "SuperAdmin" if row["is_superadmin"] else "Administrador da Organização" if row["role"] == "Administrador" else row["role"]
        return {**dict(row), "role": role, "account_organization_id": row["organization_id"],
            "organization_id": organization_id, "units": [dict(unit) for unit in units]}

    def require_csrf(self, user):
        origin = self.headers.get("Origin")
        if origin:
            expected = self.expected_origin()
            if origin.rstrip("/") != expected.rstrip("/"):
                raise ApiError(403, "Origem não autorizada.")
        supplied = self.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(supplied, user["csrf_token"]):
            raise ApiError(403, "Token de segurança inválido. Atualize a página e tente novamente.")

    def require_same_origin(self):
        origin = self.headers.get("Origin")
        if not origin or origin.rstrip("/") != self.expected_origin():
            raise ApiError(403, "Origem não autorizada.")

    def create_organization(self, db, payload):
        name = clean_text(payload.get("organization_name"), "o nome da organização", 180, True)
        unit_name = clean_text(payload.get("unit_name") or "Unidade principal", "o nome da unidade", 180, True)
        full_name = clean_text(payload.get("full_name"), "o nome completo do administrador", 180, True)
        username = clean_text(payload.get("username"), "o usuário", 80, True).lower()
        email = self.clean_email(payload.get("email", ""))
        password = payload.get("password", "")
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ApiError(400, "A senha deve ter entre 12 e 256 caracteres.")
        db.execute("BEGIN IMMEDIATE")
        slug = "org-" + secrets.token_hex(12)
        organization_id = db.execute("INSERT INTO organizations(name,slug,created_at) VALUES(?,?,?)",
                                     (name, slug, now_iso())).lastrowid
        unit_id = db.execute("INSERT INTO units(name,created_at,organization_id) VALUES(?,?,?)",
                             (unit_name, now_iso(), organization_id)).lastrowid
        user_id = db.execute("""INSERT INTO users(username,full_name,email,password_hash,role,active,created_at,organization_id)
            VALUES(?,?,?,?, 'Administrador',1,?,?)""",
            (username, full_name, email, password_hash(password), now_iso(), organization_id)).lastrowid
        db.execute("INSERT INTO user_units(user_id,unit_id,organization_id) VALUES(?,?,?)",
                   (user_id, unit_id, organization_id))
        db.execute("INSERT INTO audit_log(actor,entity,entity_id,changes,created_at,organization_id) VALUES(?,?,?,?,?,?)",
                   ("Sistema", "organizations", organization_id, json.dumps({"criada": name}, ensure_ascii=False), now_iso(), organization_id))
        db.commit()
        return {"organization_id": organization_id, "unit_id": unit_id, "user_id": user_id,
                "name": name, "username": username, "role": "Administrador da Organização"}

    def register_organization(self, payload):
        db = connect_db()
        try:
            self.create_organization(db, payload)
        finally:
            db.close()
        self.login({"username": payload.get("username"), "password": payload.get("password")})

    def get_unit_id(self, db, user):
        units = user["units"]
        organization_id = require_organization(user)
        raw = self.headers.get("X-Unit-ID", "")
        unit_id = positive_int(raw, "Unidade") if raw else (units[0]["id"] if units else None)
        if not unit_id:
            raise ApiError(403, "Sua conta não tem unidades vinculadas a esta organização.")
        if not db.execute("SELECT 1 FROM units WHERE id=? AND organization_id=?", (unit_id, organization_id)).fetchone():
            raise ApiError(404, "Unidade não encontrada nesta organização.")
        if not is_organization_admin(user) and unit_id not in {unit["id"] for unit in units}:
            raise ApiError(403, "Você não tem acesso a esta unidade.")
        return unit_id

    @serialized_request
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            try:
                db = connect_db()
                try:
                    db.execute('SELECT 1').fetchone()
                finally:
                    db.close()
                self.send_json(200, {'status': 'ok'})
            except Exception:
                self.send_json(503, {'status': 'unavailable'})
            return
        if path in ("/", "/index.html", "/app.js", "/app.css", "/lot-labels.css", "/lot-barcodes.js", "/label-pdf.js", "/accessibility.js", "/accessibility.css", "/catalog-orders.js", "/catalog-orders.css", "/navigation-notifications.js", "/navigation-notifications.css", "/manifest.json", "/sw.js", "/pwa.js", "/icon-192.png", "/icon-512.png", "/vendor/qrcode.min.js", "/vendor/html5-qrcode.min.js"):
            filename = "index.html" if path in ("/", "/index.html") else path[1:]
            content_type = "text/html; charset=utf-8" if filename.endswith(".html") else "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"
            if filename.endswith('.json'):
                content_type = 'application/manifest+json'
            elif filename.endswith('.png'):
                content_type = 'image/png'
            try:
                content = (PUBLIC / filename).read_bytes()
            except OSError:
                self.send_json(404, {"error": "Arquivo não encontrado."})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            self.end_headers()
            self.wfile.write(content)
            return
        if not path.startswith("/api/"):
            self.send_json(404, {"error": "Rota não encontrada."})
            return
        db = None
        try:
            user = self.get_user()
            db = connect_db()
            if path.startswith(("/api/files/reports/", "/api/files/label-previews/")):
                self.download_pdf_file(db, user, path)
                return
            if path == "/api/organizations" or (path == "/api/session" and user["role"] == "SuperAdmin" and not user["organization_id"]):
                if path == "/api/organizations":
                    result = self.api_get(path, db, user, None)
                else:
                    result = self.api_get(path, db, user, None)
                    result["organizations"] = [dict(row) for row in db.execute(
                        "SELECT id,name,active,created_at FROM organizations ORDER BY name")]
                db.close()
                self.send_json(200, result)
                return
            unit_id = self.get_unit_id(db, user)
            result = self.api_get(path, db, user, unit_id)
            db.close()
            self.send_json(200, result)
        except ApiError as error:
            self.send_error_json(error)
        except Exception as error:
            logging.getLogger("pharmacia").error("request_failed method=%s error_type=%s", getattr(self,"command","unknown"), type(error).__name__)
            self.send_json(500, {"error": "Erro interno. Consulte o log do servidor."})
        finally:
            if db is not None:
                db.close()

    def ensure_pdf_files(self, db):
        if getattr(db, 'dialect', '') == 'postgresql':
            return  # Versioned schema migration owns DDL in production.
        db.execute("""CREATE TABLE IF NOT EXISTS generated_pdf_files(
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, unit_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL, content BLOB NOT NULL, organization_id INTEGER REFERENCES organizations(id))""")

    def download_pdf_file(self, db, user, path):
        self.ensure_pdf_files(db)
        preview = path.startswith("/api/files/label-previews/")
        token = path.rsplit("/", 1)[-1].removesuffix(".svg" if preview else ".pdf")
        organization_id = require_organization(user)
        row = db.execute("SELECT * FROM generated_pdf_files WHERE token=? AND user_id=? AND organization_id=? AND expires_at>?",
                 (token, user["id"], organization_id, now_iso())).fetchone()
        if not row:
            raise ApiError(404, "Não foi possível gerar o PDF. Tente novamente.")
        is_pdf = bytes(row["content"]).startswith(b"%PDF-")
        if is_pdf == preview:
            raise ApiError(404, "Arquivo não encontrado.")
        self.require_unit_access(user, row["unit_id"])
        requested_unit = positive_int(parse_qs(urlparse(self.path).query).get("unit_id", [""])[0], "Unidade")
        if requested_unit != row["unit_id"]:
            raise ApiError(403, "Não foi possível gerar o PDF. Tente novamente.")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8" if preview else "application/pdf")
        self.send_header("Content-Disposition", 'inline; filename="folha.svg"' if preview else 'attachment; filename="relatorio.pdf"')
        if preview:
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(row["content"])))
        self.end_headers()
        self.wfile.write(row["content"])

    def generate_pdf_file(self, db, user, unit_id, payload):
        from pdf_reports import render_pdf, render_label_sheet_pdf
        self.require_unit_access(user, unit_id)
        if "unit_id" in payload and positive_int(payload["unit_id"], "Unidade") != unit_id:
            raise ApiError(403, "Unidade diferente da selecionada.")
        kind = payload.get("kind", "movements")
        organization_id = require_organization(user)
        unit = db.execute("SELECT name FROM units WHERE id=? AND organization_id=?", (unit_id, organization_id)).fetchone()
        if not unit:
            raise ApiError(404, "Unidade não encontrada nesta organização.")
        unit = unit[0]
        sections, barcode = [], None
        title = "Relatório da unidade"
        fields = [("name", "Medicamento"), ("concentration", "Concentração"), ("dosage_form", "Forma farmacêutica"),
                  ("presentation", "Apresentação"), ("stock_unit", "Unidade de estoque"), ("administration_route", "Via"),
                  ("minimum_stock", "Estoque mínimo"), ("manufacturer_barcode", "Código do fabricante"), ("notes", "Observações")]
        if kind == "order":
            content = self.catalog_service(db, user, unit_id).pdf(payload.get("order_id"))
            self.ensure_pdf_files(db)
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(timespec="seconds")
            db.execute("INSERT INTO generated_pdf_files(token,user_id,unit_id,expires_at,content,organization_id) VALUES(?,?,?,?,?,?)",
                       (token,user["id"],unit_id,expires,content,organization_id))
            db.commit()
            return {"download_url": f"/api/files/reports/{token}.pdf?unit_id={unit_id}", "expires_at": expires}
        if kind == "lot_label":
            code = clean_text(payload.get("code"), "Código", 100, True)
            total = positive_int(payload.get("copies", 30), "Quantidade total")
            per_sheet = positive_int(payload.get("codes_per_sheet", 30), "Quantidade por folha")
            if total > 1000 or per_sheet not in (30, 35, 40):
                raise ApiError(400, "Use de 1 a 1000 etiquetas, com 30, 35 ou 40 por folha.")
            if payload.get("variant", "qr") != "qr":
                raise ApiError(400, "As etiquetas utilizam exclusivamente QR Code.")
            lot = self.scan_lot(db, unit_id, code, organization_id)["lot"]
            labels = [{"code": lot["internal_code"], "medicine_name": lot["medicine_name"], "concentration": lot["concentration"]} for _ in range(total)]
            if payload.get("preview") is True:
                from pdf_reports import render_label_preview
                result = render_label_preview(labels, per_sheet)
                self.ensure_pdf_files(db)
                db.execute("DELETE FROM generated_pdf_files WHERE expires_at<=? AND organization_id=?", (now_iso(), organization_id))
                expires = (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(timespec="seconds")
                urls = []
                for svg in result.pop("sheets"):
                    token = secrets.token_urlsafe(32)
                    db.execute("INSERT INTO generated_pdf_files(token,user_id,unit_id,expires_at,content,organization_id) VALUES(?,?,?,?,?,?)",
                               (token,user["id"],unit_id,expires,svg.encode("utf-8"),organization_id))
                    urls.append(f"/api/files/label-previews/{token}.svg?unit_id={unit_id}")
                db.commit()
                return {**result, "sheet_urls": urls}
            content = render_label_sheet_pdf(labels, per_sheet)
            self.ensure_pdf_files(db)
            db.execute("DELETE FROM generated_pdf_files WHERE expires_at<=? AND organization_id=?", (now_iso(), organization_id))
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(timespec="seconds")
            db.execute("INSERT INTO generated_pdf_files(token,user_id,unit_id,expires_at,content,organization_id) VALUES(?,?,?,?,?,?)",
                       (token,user["id"],unit_id,expires,content,organization_id))
            db.commit()
            return {"download_url": f"/api/files/reports/{token}.pdf?unit_id={unit_id}", "expires_at": expires}
        elif kind == "medicine_label":
            raise ApiError(400, "Selecione um lote para gerar a etiqueta QR com seu código interno.")
        else:
            if kind not in ("movements", "weekly", "monthly", "entries", "exits", "stock", "expiring", "losses", "inventory", "transfers", "consolidated", "replenishment", "history"):
                raise ApiError(400, "Tipo de relatório indisponível.")
            old_path = self.path
            filters = {key: payload[key] for key in ("period", "date", "start", "end", "medicine_id") if payload.get(key)}
            if kind in ("weekly", "monthly"):
                filters["period"] = kind
            try:
                self.path = "/api/reports/movements?" + urlencode(filters)
                report = self.movement_report(db, unit_id, organization_id)
            finally:
                self.path = old_path
            start, end = report["start_date"], report["end_date"]
            title = {"movements":"Movimentações", "weekly":"Relatório semanal", "monthly":"Relatório mensal", "entries":"Entradas", "exits":"Saídas", "stock":"Estoque atual", "expiring":"Medicamentos próximos do vencimento", "losses":"Reduções registradas em inventário", "inventory":"Inventário atual", "transfers":"Transferências", "consolidated":"Relatório consolidado da unidade", "replenishment":"Reposição", "requisitions":"Requisições", "history":"Histórico de movimentações"}[kind]
            if kind in ("movements", "weekly", "monthly", "consolidated"):
                sections.append((f"Movimentações: {start} a {end}", ["Medicamento", "Unidade", "Entradas", "Saídas", "Ajustes"],
                    [[r["medicine_name"]+" "+r["concentration"],r["stock_unit"],r["entries"],r["exits"],r["adjustments"]] for r in report["items"]]))
            if kind in ("entries", "exits"):
                rows = db.execute("""SELECT m.name,m.concentration,l.lot_number,mv.quantity,m.stock_unit,mv.reason,mv.created_at FROM movements mv
                    JOIN medicines m ON m.id=mv.medicine_id JOIN lots l ON l.id=mv.lot_id
                    WHERE mv.unit_id=? AND mv.organization_id=? AND m.organization_id=? AND l.organization_id=?
                    AND mv.kind=? AND substr(mv.created_at,1,10) BETWEEN ? AND ?
                    AND (?='' OR m.id=?) ORDER BY mv.created_at,mv.id""", (unit_id,organization_id,organization_id,organization_id,
                    "Entrada" if kind=="entries" else "Saída",start,end,filters.get("medicine_id",""),filters.get("medicine_id",""))).fetchall()
                sections.append((f"{start} a {end}", ["Medicamento", "Lote", "Quantidade", "Motivo", "Data"],
                    [[r[0]+" "+r[1],r[2],str(r[3])+" "+r[4],r[5],r[6]] for r in rows]))
            if kind in ("stock", "inventory", "expiring", "consolidated"):
                rows = self.api_get("/api/lots", db, user, unit_id)["items"]
                if filters.get("medicine_id"):
                    rows = [r for r in rows if r["medicine_id"] == positive_int(filters["medicine_id"], "Medicamento")]
                if kind == "expiring":
                    rows = [r for r in rows if r["quantity"]>0 and date.today().isoformat()<=r["expiration_date"]<=(date.today()+timedelta(days=90)).isoformat()]
                sections.append(("Estoque na geração" if kind!="expiring" else "Vencimento nos próximos 90 dias", ["Medicamento", "Lote", "Validade", "Saldo", "Código interno"],
                    [[r["medicine_name"]+" "+r["concentration"],r["lot_number"],r["expiration_date"],str(r["quantity"])+" "+r["measure_unit"],r["internal_code"]] for r in rows]))
            if kind in ("losses", "history"):
                old_path=self.path
                try:
                    filters={k:payload[k] for k in ("start","end","user","medicine","lot","code") if payload.get(k)}
                    filters.setdefault("start",start); filters.setdefault("end",end)
                    self.path="/api/movements?"+urlencode(filters)
                    rows=self.filtered_movements(db,unit_id,losses=kind=="losses",organization_id=organization_id)["items"]
                finally:
                    self.path=old_path
                title="Perdas registradas" if kind=="losses" else "Histórico de movimentações"
                sections.append((title,["Medicamento / lote","Quantidade","Categoria / motivo","Usuário","Data"],
                    [[r["medicine_name"]+" / "+r["lot_number"],r["quantity"],r["exit_category"]+" / "+r["reason"],r["user_name"],r["created_at"]] for r in rows]))
            if kind in ("transfers", "consolidated"):
                rows = self.list_transfers(db, unit_id, require_organization(user))
                rows = [r for r in rows if start<=r["created_at"][:10]<=end]
                sections.append((f"Transferências: {start} a {end}", ["Medicamento / lote", "Origem", "Destino", "Enviado / recebido", "Status"],
                    [[r["medicine_name"]+" / "+r["lot_number"],r["origin_name"],r["destination_name"],str(r["quantity_sent"])+" / "+str(r["quantity_received"] if r["quantity_received"] is not None else "pendente"),r["status"]] for r in rows]))
            if kind == "replenishment":
                rows = self.api_get("/api/replenishment",db,user,unit_id)["items"]
                sections.append(("Reposição atual", ["Medicamento", "Saldo válido", "Mínimo", "Repor"], [[r["medicine_name"],r["stock"],r["minimum_stock"],r["needed"]] for r in rows]))
        content = render_pdf(title, unit, sections, barcode, minimal=(kind == "lot_label"))
        self.ensure_pdf_files(db)
        db.execute("DELETE FROM generated_pdf_files WHERE expires_at<=? AND organization_id=?", (now_iso(), organization_id))
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(timespec="seconds")
        db.execute("INSERT INTO generated_pdf_files(token,user_id,unit_id,expires_at,content,organization_id) VALUES(?,?,?,?,?,?)",
               (token,user["id"],unit_id,expires,content,organization_id))
        db.commit()
        return {"download_url": f"/api/files/reports/{token}.pdf?unit_id={unit_id}", "expires_at": expires}

    def catalog_service(self, db, user, unit_id):
        return catalog_orders.Service(self, db, user, unit_id, DATA)

    def api_get(self, path, db, user, unit_id):
        if path == "/api/notifications":
            return notifications.list_notifications(db, user)
        if path in ("/api/catalog", "/api/orders") or path.startswith("/api/orders/"):
            return self.catalog_service(db, user, unit_id).get(path, parse_qs(urlparse(self.path).query))
        if path == "/api/lots/scan":
            code = clean_text(parse_qs(urlparse(self.path).query).get("code", [""])[0], "o código interno", 100, True)
            return self.scan_lot(db, unit_id, code, require_organization(user))
        if path == "/api/transfer-units":
            self.require_transfer_manager(user)
            organization_id = require_organization(user)
            return {"items": [dict(row) for row in db.execute("SELECT id,name FROM units WHERE organization_id=? ORDER BY name", (organization_id,))]}
        if path == "/api/notifications/transfers":
            items = self.list_transfers(db, unit_id, require_organization(user), pending_only=True)
            return {"items": items, "count": len(items)}
        if path in ("/api/backups", "/api/audit"):
            if not is_organization_admin(user):
                raise ApiError(403, "Apenas administradores podem acessar esta área.")
            if path == "/api/audit":
                organization_id = require_organization(user)
                return {"items": [dict(row) for row in db.execute("SELECT * FROM audit_log WHERE organization_id=? ORDER BY id DESC", (organization_id,))]}
            if user["role"] != "SuperAdmin":
                raise ApiError(403, "Backups completos estão disponíveis somente ao SuperAdmin.")
            files = sorted(BACKUPS.glob("farmacia-*.sqlite3"), reverse=True)
            return {"items": [{"name": f.name, "size": f.stat().st_size,
                              "created_at": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()} for f in files]}
        if path == "/api/replenishment":
            return {"items": [dict(row) for row in db.execute("""
                SELECT m.id, m.name AS medicine_name, m.concentration, m.dosage_form, m.presentation, m.administration_route, m.manufacturer_barcode, m.stock_unit, m.minimum_stock,
                       COALESCE(SUM(l.quantity),0) AS stock,
                       m.minimum_stock - COALESCE(SUM(l.quantity),0) AS needed
                FROM medicines m LEFT JOIN lots l ON l.medicine_id=m.id AND l.unit_id=? AND l.expiration_date>=? AND l.organization_id=?
                WHERE m.active=1 AND m.organization_id=? GROUP BY m.id HAVING stock < m.minimum_stock ORDER BY m.name
            """, (unit_id, date.today().isoformat(), require_organization(user), require_organization(user)))]}
        if path == "/api/session":
            return {"user": {"id": user["id"], "username": user["username"], "name": user["full_name"], "role": user["role"], "profilePhoto": user["profile_photo"]}, "organizationId": user["organization_id"], "units": user["units"], "csrfToken": user["csrf_token"], "activeUnitId": unit_id, "expiresAt": user.get("expires_at")}
        if path == "/api/organizations":
            if user["role"] != "SuperAdmin":
                raise ApiError(403, "Apenas o SuperAdmin pode listar organizações.")
            return {"items": [dict(row) for row in db.execute("SELECT id,name,active,created_at FROM organizations ORDER BY name")]}
        if path == "/api/users":
            if not is_organization_admin(user):
                raise ApiError(403, "Apenas administradores da organização podem gerenciar usuários.")
            organization_id = require_organization(user)
            items = [dict(row) for row in db.execute("""
                SELECT u.id, u.username, u.full_name, u.email, u.role, u.active, u.created_at,
                       COALESCE(GROUP_CONCAT(un.name, ' | '), '') AS unit_names,
                       COALESCE(GROUP_CONCAT(un.id, ','), '') AS unit_ids
                FROM users u
                LEFT JOIN user_units uu ON uu.user_id = u.id AND uu.organization_id=?
                LEFT JOIN units un ON un.id = uu.unit_id AND un.organization_id=?
                WHERE u.organization_id=? AND u.is_superadmin=0
                GROUP BY u.id ORDER BY u.active DESC, u.full_name
            """, (organization_id, organization_id, organization_id)).fetchall()]
            for item in items:
                if item["role"] == "Administrador":
                    item["role"] = "Administrador da Organização"
            return {"items": items,
                    "units": [dict(row) for row in db.execute("SELECT id, name FROM units WHERE organization_id=? ORDER BY name", (organization_id,)).fetchall()]}
        if path == "/api/transfers":
            items = self.list_transfers(db, unit_id, require_organization(user))
            pending = sum(item["destination_unit_id"] == unit_id and item["status"] == TRANSFER_PENDING for item in items)
            return {"items": items, "pending_receipts": pending}
        if path == "/api/reports/movements":
            return self.movement_report(db, unit_id, require_organization(user))
        if path == "/api/medicines":
            query = "%" + parse_qs(urlparse(self.path).query).get("q", [""])[0].strip() + "%"
            return {"items": [dict(row) for row in db.execute("""
                SELECT m.*, EXISTS(SELECT 1 FROM movements mv WHERE mv.medicine_id=m.id AND mv.organization_id=m.organization_id) AS stock_unit_locked, COALESCE(SUM(l.quantity), 0) AS stock
                FROM medicines m LEFT JOIN lots l ON l.medicine_id = m.id AND l.unit_id = ? AND l.organization_id=m.organization_id
                WHERE m.organization_id=? AND m.active = 1 AND (m.name LIKE ? OR m.concentration LIKE ? OR m.dosage_form LIKE ? OR m.presentation LIKE ? OR m.stock_unit LIKE ? OR m.administration_route LIKE ? OR m.manufacturer_barcode LIKE ?)
                GROUP BY m.id ORDER BY m.name
            """, (unit_id, require_organization(user), query, query, query, query, query, query, query)).fetchall()]}
        if path == "/api/lots":
            return {"items": [dict(row) for row in db.execute("""
                SELECT l.*, m.name AS medicine_name, m.concentration, m.dosage_form, m.presentation,
                       m.administration_route, m.manufacturer_barcode, m.stock_unit AS measure_unit,
                       m.minimum_stock, m.code AS medicine_code, u.name AS unit_name
                FROM lots l JOIN medicines m ON m.id = l.medicine_id JOIN units u ON u.id = l.unit_id
                WHERE l.unit_id = ? AND l.organization_id=? AND m.organization_id=? ORDER BY l.expiration_date, m.name
            """, (unit_id, require_organization(user), require_organization(user))).fetchall()]}
        if path in ("/api/movements", "/api/losses"):
            return self.filtered_movements(db, unit_id, losses=path.endswith("losses"), organization_id=require_organization(user))
        if path == "/api/requisitions":
            return {"items": [dict(r) for r in db.execute("""SELECT r.*, a.name AS requester_name,b.name AS supplier_name,
                m.name AS medicine_name,m.concentration,m.stock_unit,u.full_name AS creator_name,d.full_name AS decider_name,
                t.status AS transfer_status
                FROM requisitions r JOIN units a ON a.id=r.requester_unit_id JOIN units b ON b.id=r.supplier_unit_id
                JOIN medicines m ON m.id=r.medicine_id JOIN users u ON u.id=r.created_by
                LEFT JOIN users d ON d.id=r.decided_by LEFT JOIN transfers t ON t.id=r.transfer_id
                WHERE r.organization_id=? AND (r.requester_unit_id=? OR r.supplier_unit_id=?) ORDER BY r.id DESC""", (require_organization(user), unit_id,unit_id))]}
        if path == "/api/alerts":
            return {"items": self.alerts(db, unit_id, require_organization(user))}
        if path == "/api/inventory":
            return {"items": [dict(row) for row in db.execute("""
                SELECT l.id AS lot_id, l.lot_number, l.quantity AS system_quantity, l.expiration_date,
                       m.id AS medicine_id, m.name AS medicine_name, m.concentration, m.dosage_form, m.presentation,
                       m.administration_route, m.manufacturer_barcode, m.stock_unit AS measure_unit
                FROM lots l JOIN medicines m ON m.id = l.medicine_id
                WHERE l.unit_id = ? AND l.organization_id=? AND m.organization_id=? ORDER BY m.name, l.expiration_date
            """, (unit_id, require_organization(user), require_organization(user))).fetchall()]}
        if path == "/api/dashboard":
            today = date.today()
            month_start = today.replace(day=1).isoformat()
            counts = db.execute("""
                SELECT COUNT(DISTINCT CASE WHEN m.active = 1 THEN m.id END) AS medicines,
                       COALESCE(SUM(l.quantity),0) AS units,
                       COUNT(DISTINCT CASE WHEN l.quantity = 0 THEN l.id END) AS empty_lots
                FROM medicines m LEFT JOIN lots l ON l.medicine_id = m.id AND l.unit_id = ? AND l.organization_id=?
                WHERE m.organization_id=?
            """, (unit_id, require_organization(user), require_organization(user))).fetchone()
            month = db.execute("""
                SELECT kind, COALESCE(SUM(quantity),0) AS total FROM movements
                WHERE unit_id = ? AND organization_id=? AND substr(created_at,1,10) >= ? GROUP BY kind
            """, (unit_id, require_organization(user), month_start)).fetchall()
            alerts = self.alerts(db, unit_id, require_organization(user))
            pending_transfers = db.execute("SELECT COUNT(*) FROM transfers WHERE destination_unit_id=? AND organization_id=? AND status=?", (unit_id, require_organization(user), TRANSFER_PENDING)).fetchone()[0]
            near = sum(item["type"] == "expiry" and item["days_remaining"] >= 0 for item in alerts)
            expired = sum(item["type"] == "expiry" and item["days_remaining"] < 0 for item in alerts)
            return {"summary": {**dict(counts), "low_stock": sum(item["type"] == "low_stock" for item in alerts), "expiring": near, "expired": expired,
                    "entries_month": next((row["total"] for row in month if row["kind"] == "Entrada"), 0),
                    "exits_month": next((row["total"] for row in month if row["kind"] == "Saída"), 0),
                    "alerts": len(alerts), "pending_transfers": pending_transfers}, "alerts": alerts[:8], "recent": [dict(row) for row in db.execute("""
                  SELECT mv.id, mv.kind, mv.quantity, mv.created_at, m.name AS medicine_name, m.concentration,
                      m.dosage_form, m.presentation, m.administration_route, m.manufacturer_barcode, m.stock_unit, l.lot_number
                FROM movements mv JOIN medicines m ON m.id=mv.medicine_id JOIN lots l ON l.id=mv.lot_id
                WHERE mv.unit_id=? AND mv.organization_id=? AND m.organization_id=? AND l.organization_id=? ORDER BY mv.created_at DESC LIMIT 8
            """, (unit_id, require_organization(user), require_organization(user), require_organization(user))).fetchall()]}
        raise ApiError(404, "Rota não encontrada.")

    def restore_backup(self, user, payload):
        if DATABASE is None:
            raise ApiError(409, 'Restaure PostgreSQL pelo procedimento operacional documentado, em janela de manutenção.')
        name = payload.get("name")
        if not isinstance(name, str) or name != Path(name).name or not name.startswith("farmacia-") or not name.endswith(".sqlite3"):
            raise ApiError(400, "Backup inválido.")
        target = BACKUPS / name
        if not target.is_file():
            raise ApiError(404, "Backup não encontrado.")
        account_db = connect_db()
        try:
            account = account_db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
            password = payload.get("password", "")
            if not isinstance(password, str) or not verify_password(password, account[0]):
                raise ApiError(403, "Senha atual incorreta.")
            reserve_lot_code(account_db)
        finally:
            account_db.close()
        source = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
        try:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ApiError(400, "O backup está corrompido.")
            required = {"users", "medicines", "lots", "movements", "units", "audit_log"}
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= tables or source.execute("PRAGMA foreign_key_check").fetchone():
                raise ApiError(400, "Backup incompatível.")
            backup_database()
            destination = connect_db()
            try:
                source.backup(destination)
                destination.execute("CREATE TABLE IF NOT EXISTS quick_exit_requests(request_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,unit_id INTEGER NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL)")
                destination.executescript("        CREATE TABLE IF NOT EXISTS requisitions (\n            id INTEGER PRIMARY KEY, requester_unit_id INTEGER NOT NULL REFERENCES units(id),\n            supplier_unit_id INTEGER NOT NULL REFERENCES units(id), medicine_id INTEGER NOT NULL REFERENCES medicines(id),\n            quantity INTEGER NOT NULL CHECK(quantity>0), reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pendente',\n            created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,\n            decided_by INTEGER REFERENCES users(id), decided_at TEXT, decision_reason TEXT NOT NULL DEFAULT '',\n            transfer_id INTEGER UNIQUE REFERENCES transfers(id)\n        );\n")
                migrate_organization_schema(destination)
                if "exit_category" not in {r["name"] for r in destination.execute("PRAGMA table_info(movements)")}:
                    destination.execute("ALTER TABLE movements ADD COLUMN exit_category TEXT NOT NULL DEFAULT 'Não classificado'")
                destination.execute("DELETE FROM sessions")
                destination.execute("DELETE FROM password_resets")
                destination.execute("INSERT INTO audit_log(actor,entity,entity_id,changes,created_at,organization_id) VALUES(?,?,?,?,?,?)",
                    (user["full_name"], "backup", 0, json.dumps({"restaurado": name}), now_iso(), user.get("organization_id")))
                destination.commit()
            finally:
                destination.close()
        finally:
            source.close()

    def list_transfers(self, db, unit_id, organization_id=None, pending_only=False):
        if organization_id is None:
            organization_id = db.execute("SELECT organization_id FROM units WHERE id=?", (unit_id,)).fetchone()[0]
        rows = db.execute("""
            SELECT t.*, origin.name AS origin_name, destination.name AS destination_name,
                   m.name AS medicine_name, m.concentration, m.dosage_form, m.presentation,
                   m.stock_unit, m.administration_route, m.manufacturer_barcode, sender.full_name AS sender_name, receiver.full_name AS receiver_name,
                   reporter.full_name AS discrepancy_reporter_name, resolver.full_name AS resolver_name
            FROM transfers t
            JOIN units origin ON origin.id=t.origin_unit_id
            JOIN units destination ON destination.id=t.destination_unit_id
            JOIN medicines m ON m.id=t.medicine_id
            JOIN users sender ON sender.id=t.created_by
            LEFT JOIN users receiver ON receiver.id=t.received_by
            LEFT JOIN users reporter ON reporter.id=t.discrepancy_reported_by
            LEFT JOIN users resolver ON resolver.id=t.resolved_by
                        WHERE t.organization_id=? AND origin.organization_id=? AND destination.organization_id=? AND m.organization_id=?
                            AND (t.origin_unit_id=? OR t.destination_unit_id=?)
              AND (?=0 OR (t.destination_unit_id=? AND t.status=?))
            ORDER BY CASE WHEN t.destination_unit_id=? AND t.status=? THEN 0 ELSE 1 END, t.created_at DESC, t.id DESC
          """, (organization_id, organization_id, organization_id, organization_id, unit_id, unit_id,
              int(pending_only), unit_id, TRANSFER_PENDING, unit_id, TRANSFER_PENDING)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["events"] = [dict(event) for event in db.execute("""
                SELECT e.id, e.event_type, e.status, e.reason, e.notes, e.created_at, u.full_name AS user_name
                FROM transfer_events e JOIN users u ON u.id=e.user_id
                WHERE e.transfer_id=? AND e.organization_id=? ORDER BY e.created_at, e.id
            """, (item["id"], organization_id)).fetchall()]
            result.append(item)
        return result

    def record_transfer_event(self, db, transfer_id, event_type, status, user_id, organization_id, reason="", notes=""):
        db.execute("""INSERT INTO transfer_events(transfer_id,event_type,status,user_id,reason,notes,created_at,organization_id)
            VALUES(?,?,?,?,?,?,?,?)""", (transfer_id, event_type, status, user_id, reason, notes, now_iso(), organization_id))

    def require_transfer_manager(self, user):
        if not can_manage(user):
            raise ApiError(403, "Seu perfil não pode gerenciar transferências.")

    def require_transfer_resolution(self, user):
        if user["role"] not in ("Administrador", "Administrador da Organização", "SuperAdmin", "Enfermeiro"):
            raise ApiError(403, "Apenas administradores e enfermeiros podem resolver divergências ou estornar transferências.")

    def require_unit_access(self, user, unit_id):
        if not is_organization_admin(user) and unit_id not in {unit["id"] for unit in user["units"]}:
            raise ApiError(403, "Você não tem acesso a esta unidade.")

    def load_transfer(self, db, transfer_id, organization_id):
        transfer = db.execute("SELECT * FROM transfers WHERE id=? AND organization_id=?", (transfer_id, organization_id)).fetchone()
        if not transfer:
            raise ApiError(404, "Transferência não encontrada.")
        return transfer

    def create_transfer(self, db, user, active_unit_id, payload, commit=True):
        self.require_transfer_manager(user)
        organization_id = require_organization(user)
        origin_unit_id = positive_int(payload.get("origin_unit_id"), "Unidade de origem")
        destination_unit_id = positive_int(payload.get("destination_unit_id"), "Unidade de destino")
        if origin_unit_id != active_unit_id:
            raise ApiError(403, "A unidade de origem deve ser a unidade atualmente selecionada.")
        self.require_unit_access(user, origin_unit_id)
        if destination_unit_id == origin_unit_id:
            raise ApiError(400, "A unidade de destino deve ser diferente da origem.")
        if not db.execute("SELECT 1 FROM units WHERE id=? AND organization_id=?", (destination_unit_id, organization_id)).fetchone():
            raise ApiError(404, "Unidade de destino não encontrada.")
        medicine_id = positive_int(payload.get("medicine_id"), "Medicamento")
        lot_id = positive_int(payload.get("lot_id"), "Lote")
        quantity = positive_int(payload.get("quantity"), "Quantidade")
        responsible = clean_text(payload.get("responsible"), "o responsável pela transferência", 180, True)
        notes = clean_text(payload.get("notes"), "observações", 1000)
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        lot = db.execute("""SELECT l.*, m.name AS medicine_name, m.stock_unit
            FROM lots l JOIN medicines m ON m.id=l.medicine_id
            WHERE l.id=? AND l.unit_id=? AND l.medicine_id=? AND l.organization_id=? AND m.organization_id=? AND m.active=1""",
            (lot_id, origin_unit_id, medicine_id, organization_id, organization_id)).fetchone()
        if not lot:
            raise ApiError(404, "Lote não encontrado na unidade de origem.")
        if payload.get("expiration_date") not in (None, lot["expiration_date"]):
            raise ApiError(409, "A validade deve corresponder ao lote selecionado.")
        if lot["expiration_date"] < date.today().isoformat():
            raise ApiError(409, "Não é possível transferir um lote vencido.")
        if quantity > lot["quantity"]:
            raise ApiError(409, f"Estoque insuficiente no lote {lot['lot_number']}. Disponível: {lot['quantity']} {lot['stock_unit']}.")
        before, after = lot["quantity"], lot["quantity"] - quantity
        db.execute("UPDATE lots SET quantity=? WHERE id=? AND organization_id=?", (after, lot_id, organization_id))
        cursor = db.execute("""INSERT INTO transfers(origin_unit_id,destination_unit_id,medicine_id,origin_lot_id,
            lot_number,expiration_date,quantity_sent,status,responsible,notes,created_by,created_at,organization_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (origin_unit_id, destination_unit_id, medicine_id, lot_id,
            lot["lot_number"], lot["expiration_date"], quantity, TRANSFER_PENDING, responsible, notes, user["id"], now_iso(), organization_id))
        transfer_id = cursor.lastrowid
        destination_name = db.execute("SELECT name FROM units WHERE id=? AND organization_id=?", (destination_unit_id, organization_id)).fetchone()["name"]
        self.insert_movement(db, origin_unit_id, medicine_id, lot_id, user["id"], "Saída", quantity, before, after, {
            "reason": "Transferência entre unidades", "destination": destination_name,
            "document": f"TRF-{transfer_id:06d}", "responsible": responsible, "notes": notes
        }, require_organization(user))
        self.record_transfer_event(db, transfer_id, "Enviada", TRANSFER_PENDING, user["id"], organization_id, notes=notes)
        if not commit:
            return {"id": transfer_id, "status": TRANSFER_PENDING}
        db.commit()
        try:
            email_status = self.send_transfer_email(db, transfer_id, organization_id)
        except Exception as error:
            print(f"Falha na notificação por e-mail da transferência {transfer_id}: {type(error).__name__}", file=sys.stderr)
            email_status = "failed"
        return {"id": transfer_id, "status": TRANSFER_PENDING, "quantity_sent": quantity, "stock_before": before,
                "stock_after": after, "stock_unit": lot["stock_unit"], "lot_number": lot["lot_number"],
                "expiration_date": lot["expiration_date"], "email_status": email_status}

    def send_transfer_email(self, db, transfer_id, organization_id):
        host = os.environ.get("FARMACIA_SMTP_HOST", "").strip()
        sender = os.environ.get("FARMACIA_SMTP_FROM", "").strip()
        if not host or not sender:
            return "not_configured"
        row = db.execute("""SELECT t.*, origin.name AS origin_name, destination.name AS destination_name,
            m.name AS medicine_name, m.stock_unit, m.administration_route, m.manufacturer_barcode, sender.full_name AS sender_name FROM transfers t
            JOIN units origin ON origin.id=t.origin_unit_id JOIN units destination ON destination.id=t.destination_unit_id
            JOIN medicines m ON m.id=t.medicine_id JOIN users sender ON sender.id=t.created_by
            WHERE t.id=? AND t.organization_id=? AND origin.organization_id=? AND destination.organization_id=? AND m.organization_id=?""",
            (transfer_id, organization_id, organization_id, organization_id, organization_id)).fetchone()
        recipients = [item[0] for item in db.execute("""SELECT DISTINCT u.email FROM users u
            LEFT JOIN user_units uu ON uu.user_id=u.id AND uu.organization_id=? WHERE u.active=1 AND u.organization_id=?
            AND u.role IN ('Administrador','Enfermeiro','Funcionário da Farmácia') AND u.email<>''
            AND (u.role='Administrador' OR uu.unit_id=?)""",
            (organization_id, organization_id, row["destination_unit_id"])).fetchall()]
        if not recipients:
            return "no_recipients"
        message = EmailMessage()
        message["Subject"] = f"Transferência pendente de recebimento · TRF-{transfer_id:06d}"
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content(f"""Transferência pendente de recebimento

Origem: {row['origin_name']}
Destino: {row['destination_name']}
Medicamento: {row['medicine_name']}
Lote: {row['lot_number']}
Quantidade enviada: {row['quantity_sent']} {row['stock_unit']}
Validade: {date.fromisoformat(row['expiration_date']).strftime('%d/%m/%Y')}
Enviado por: {row['sender_name']}
Responsável pela transferência: {row['responsible']}
Data e hora: {row['created_at']}
Observações: {row['notes'] or '—'}
""")
        try:
            port = int(os.environ.get("FARMACIA_SMTP_PORT", "587"))
            with smtplib.SMTP(host, port, timeout=10) as client:
                client.ehlo()
                if os.environ.get("FARMACIA_SMTP_TLS", "1") != "0":
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                smtp_user = os.environ.get("FARMACIA_SMTP_USER", "")
                smtp_password = os.environ.get("FARMACIA_SMTP_PASSWORD", "")
                if smtp_user:
                    client.login(smtp_user, smtp_password)
                client.send_message(message)
            return "sent"
        except (OSError, smtplib.SMTPException, ValueError) as error:
            logging.getLogger('pharmacia').error('smtp_failed error_type=%s', type(error).__name__)
            return "failed"

    def transfer_action(self, db, user, active_unit_id, path, payload):
        self.require_transfer_manager(user)
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0:2] != ["api", "transfers"]:
            raise ApiError(404, "Rota de transferência não encontrada.")
        transfer_id = positive_int(parts[2], "Transferência")
        action = clean_text(payload.get("action"), "a ação", 30, True)
        if action == "receber":
            return self.receive_transfer(db, user, active_unit_id, transfer_id)
        if action == "divergencia":
            return self.report_transfer_discrepancy(db, user, active_unit_id, transfer_id, payload)
        if action == "recusar":
            return self.refuse_transfer(db, user, active_unit_id, transfer_id, payload)
        if action == "cancelar":
            return self.cancel_transfer(db, user, active_unit_id, transfer_id, payload)
        if action == "resolver":
            return self.resolve_transfer(db, user, active_unit_id, transfer_id, payload)
        if action == "estornar":
            return self.refund_transfer(db, user, active_unit_id, transfer_id, payload)
        raise ApiError(400, "Ação de transferência inválida.")

    def receive_transfer(self, db, user, active_unit_id, transfer_id):
        db.execute("BEGIN IMMEDIATE")
        transfer = self.load_transfer(db, transfer_id, require_organization(user))
        if transfer["destination_unit_id"] != active_unit_id:
            raise ApiError(403, "Somente a unidade de destino pode confirmar o recebimento.")
        self.require_unit_access(user, transfer["destination_unit_id"])
        if transfer["status"] != TRANSFER_PENDING:
            raise ApiError(409, "Esta transferência não está pendente de recebimento.")
        destination_lot_id = self.credit_transfer_destination(db, transfer, user, transfer["quantity_sent"])
        received_at = now_iso()
        db.execute("""UPDATE transfers SET status=?, quantity_received=quantity_sent, destination_lot_id=?,
            received_by=?, received_at=? WHERE id=? AND organization_id=?""",
            (TRANSFER_RECEIVED, destination_lot_id, user["id"], received_at, transfer_id, require_organization(user)))
        self.record_transfer_event(db, transfer_id, "Recebimento confirmado", TRANSFER_RECEIVED, user["id"], require_organization(user))
        db.commit()
        return {"id": transfer_id, "status": TRANSFER_RECEIVED, "quantity_received": transfer["quantity_sent"]}

    def report_transfer_discrepancy(self, db, user, active_unit_id, transfer_id, payload):
        quantity_received = positive_int(payload.get("quantity_received"), "Quantidade recebida", allow_zero=True)
        reason = clean_text(payload.get("reason"), "o motivo da divergência", 500, True)
        notes = clean_text(payload.get("notes"), "observações", 1000)
        db.execute("BEGIN IMMEDIATE")
        transfer = self.load_transfer(db, transfer_id, require_organization(user))
        if transfer["destination_unit_id"] != active_unit_id:
            raise ApiError(403, "Somente a unidade de destino pode informar divergência.")
        self.require_unit_access(user, transfer["destination_unit_id"])
        if transfer["status"] != TRANSFER_PENDING:
            raise ApiError(409, "Esta transferência não está pendente de recebimento.")
        if quantity_received == transfer["quantity_sent"]:
            raise ApiError(400, "A quantidade recebida corresponde ao envio. Use Confirmar recebimento.")
        reported_at = now_iso()
        db.execute("""UPDATE transfers SET status=?, quantity_received=?, discrepancy_reported_by=?,
            discrepancy_reported_at=?, discrepancy_reason=?, discrepancy_notes=? WHERE id=? AND organization_id=?""",
            (TRANSFER_DIVERGENCE, quantity_received, user["id"], reported_at, reason, notes, transfer_id, require_organization(user)))
        self.record_transfer_event(db, transfer_id, "Divergência informada", TRANSFER_DIVERGENCE, user["id"], require_organization(user), reason, notes)
        db.commit()
        return {"id": transfer_id, "status": TRANSFER_DIVERGENCE, "quantity_sent": transfer["quantity_sent"],
                "quantity_received": quantity_received, "difference": transfer["quantity_sent"] - quantity_received}

    def refuse_transfer(self, db, user, active_unit_id, transfer_id, payload):
        reason = clean_text(payload.get("reason"), "a justificativa da recusa", 500, True)
        notes = clean_text(payload.get("notes"), "observações", 1000)
        db.execute("BEGIN IMMEDIATE")
        transfer = self.load_transfer(db, transfer_id, require_organization(user))
        if transfer["destination_unit_id"] != active_unit_id:
            raise ApiError(403, "Somente a unidade de destino pode recusar o recebimento.")
        self.require_unit_access(user, transfer["destination_unit_id"])
        if transfer["status"] != TRANSFER_PENDING:
            raise ApiError(409, "Esta transferência não está pendente de recebimento.")
        refused_at = now_iso()
        db.execute("UPDATE transfers SET status=?, refused_by=?, refused_at=?, refusal_reason=?, discrepancy_notes=? WHERE id=? AND organization_id=?",
            (TRANSFER_REFUSED, user["id"], refused_at, reason, notes, transfer_id, require_organization(user)))
        self.record_transfer_event(db, transfer_id, "Recebimento recusado", TRANSFER_REFUSED, user["id"], require_organization(user), reason, notes)
        db.commit()
        return {"id": transfer_id, "status": TRANSFER_REFUSED}

    def cancel_transfer(self, db, user, active_unit_id, transfer_id, payload):
        reason = clean_text(payload.get("reason"), "o motivo do cancelamento", 500, True)
        db.execute("BEGIN IMMEDIATE")
        transfer = self.load_transfer(db, transfer_id, require_organization(user))
        if transfer["origin_unit_id"] != active_unit_id:
            raise ApiError(403, "Somente a unidade de origem pode cancelar a transferência.")
        self.require_unit_access(user, transfer["origin_unit_id"])
        if transfer["status"] != TRANSFER_PENDING:
            raise ApiError(409, "Somente transferências pendentes podem ser canceladas.")
        self.restore_transfer_origin(db, transfer, user, "Cancelamento de transferência", reason)
        resolved_at = now_iso()
        db.execute("UPDATE transfers SET status=?, resolved_by=?, resolved_at=?, resolution_reason=? WHERE id=? AND organization_id=?",
            (TRANSFER_CANCELLED, user["id"], resolved_at, reason, transfer_id, require_organization(user)))
        self.record_transfer_event(db, transfer_id, "Transferência cancelada", TRANSFER_CANCELLED, user["id"], require_organization(user), reason)
        db.commit()
        return {"id": transfer_id, "status": TRANSFER_CANCELLED}

    def resolve_transfer(self, db, user, active_unit_id, transfer_id, payload):
        self.require_transfer_resolution(user)
        decision = clean_text(payload.get("decision"), "a decisão", 20, True)
        if decision == "estornar":
            return self.refund_transfer(db, user, active_unit_id, transfer_id, payload)
        if decision != "receber":
            raise ApiError(400, "Escolha receber a quantidade informada ou estornar a transferência.")
        reason = clean_text(payload.get("reason"), "o motivo da resolução", 500, True)
        notes = clean_text(payload.get("notes"), "observações", 1000)
        db.execute("BEGIN IMMEDIATE")
        transfer = self.load_transfer(db, transfer_id, require_organization(user))
        if transfer["destination_unit_id"] != active_unit_id:
            raise ApiError(403, "A resolução com recebimento deve ser confirmada por um responsável autorizado da unidade de destino.")
        self.require_unit_access(user, transfer["destination_unit_id"])
        if transfer["status"] != TRANSFER_DIVERGENCE or transfer["quantity_received"] is None:
            raise ApiError(409, "A transferência não possui divergência pendente de resolução.")
        if transfer["quantity_received"] == 0:
            raise ApiError(400, "Não há quantidade recebida para dar entrada. Estorne a transferência após conferir a devolução.")
        destination_lot_id = self.credit_transfer_destination(db, transfer, user, transfer["quantity_received"], reason)
        resolved_at = now_iso()
        db.execute("""UPDATE transfers SET status=?, destination_lot_id=?, received_by=?,
            received_at=?, resolved_by=?, resolved_at=?, resolution_reason=? WHERE id=? AND organization_id=?""",
            (TRANSFER_RECEIVED, destination_lot_id, user["id"], resolved_at, user["id"], resolved_at, reason, transfer_id, require_organization(user)))
        event_notes = "; ".join(part for part in (transfer["discrepancy_notes"], notes) if part)
        self.record_transfer_event(db, transfer_id, "Divergência resolvida: quantidade recebida aceita", TRANSFER_RECEIVED, user["id"], require_organization(user), reason, event_notes)
        db.commit()
        return {"id": transfer_id, "status": TRANSFER_RECEIVED, "quantity_received": transfer["quantity_received"]}

    def refund_transfer(self, db, user, active_unit_id, transfer_id, payload):
        self.require_transfer_resolution(user)
        reason = clean_text(payload.get("reason"), "a justificativa do estorno", 500, True)
        db.execute("BEGIN IMMEDIATE")
        transfer = self.load_transfer(db, transfer_id, require_organization(user))
        if transfer["origin_unit_id"] != active_unit_id:
            raise ApiError(403, "Somente a unidade de origem pode estornar a transferência.")
        self.require_unit_access(user, transfer["origin_unit_id"])
        if transfer["status"] not in (TRANSFER_REFUSED, TRANSFER_DIVERGENCE):
            raise ApiError(409, "Somente transferências recusadas ou divergentes podem ser estornadas.")
        self.restore_transfer_origin(db, transfer, user, "Estorno de transferência", reason)
        resolved_at = now_iso()
        db.execute("UPDATE transfers SET status=?, resolved_by=?, resolved_at=?, resolution_reason=? WHERE id=? AND organization_id=?",
            (TRANSFER_REVERSED, user["id"], resolved_at, reason, transfer_id, require_organization(user)))
        self.record_transfer_event(db, transfer_id, "Estorno confirmado e devolvido à origem", TRANSFER_REVERSED, user["id"], require_organization(user), reason)
        db.commit()
        return {"id": transfer_id, "status": TRANSFER_REVERSED}

    def restore_transfer_origin(self, db, transfer, user, movement_reason, reason):
        organization_id = require_organization(user)
        lot = db.execute("SELECT * FROM lots WHERE id=? AND unit_id=? AND organization_id=?", (transfer["origin_lot_id"], transfer["origin_unit_id"], organization_id)).fetchone()
        if not lot:
            raise ApiError(409, "O lote original não existe mais na unidade de origem.")
        before = lot["quantity"]
        after = before + transfer["quantity_sent"]
        db.execute("UPDATE lots SET quantity=? WHERE id=? AND organization_id=?", (after, lot["id"], organization_id))
        origin_name = db.execute("SELECT name FROM units WHERE id=? AND organization_id=?", (transfer["origin_unit_id"], organization_id)).fetchone()["name"]
        self.insert_movement(db, transfer["origin_unit_id"], transfer["medicine_id"], lot["id"], user["id"], "Entrada",
            transfer["quantity_sent"], before, after, {"reason": movement_reason, "source": "Unidade de destino",
            "document": f"TRF-{transfer['id']:06d}", "responsible": user["full_name"], "notes": f"{origin_name}: {reason}"},
            require_organization(user))

    def credit_transfer_destination(self, db, transfer, user, quantity, reason=""):
        organization_id = require_organization(user)
        if transfer["expiration_date"] < date.today().isoformat():
            raise ApiError(409, "O lote venceu durante o transporte e não pode entrar no estoque. Recuse ou resolva com estorno.")
        existing = db.execute("""SELECT * FROM lots WHERE organization_id=? AND unit_id=? AND medicine_id=? AND lot_number=?""",
            (organization_id, transfer["destination_unit_id"], transfer["medicine_id"], transfer["lot_number"])).fetchone()
        if existing and existing["expiration_date"] != transfer["expiration_date"]:
            raise ApiError(409, "Já existe um lote com o mesmo número e outra validade no destino.")
        if existing:
            destination_lot_id = existing["id"]
            before = existing["quantity"]
            after = before + quantity
            db.execute("UPDATE lots SET quantity=? WHERE id=? AND organization_id=?", (after, destination_lot_id, organization_id))
        else:
            manufacture_date = db.execute("SELECT manufacture_date FROM lots WHERE id=? AND organization_id=?", (transfer["origin_lot_id"], organization_id)).fetchone()["manufacture_date"]
            cursor = db.execute("""INSERT INTO lots(unit_id,medicine_id,lot_number,internal_code,manufacture_date,
                expiration_date,quantity,created_at,organization_id) VALUES(?,?,?,?,?,?,0,?,?)""",
                (transfer["destination_unit_id"], transfer["medicine_id"], transfer["lot_number"], "PENDING",
                 manufacture_date, transfer["expiration_date"], now_iso(), organization_id))
            destination_lot_id = cursor.lastrowid
            internal_code = reserve_lot_code(db, transfer["medicine_id"])
            before, after = 0, quantity
            db.execute("UPDATE lots SET internal_code=?, quantity=? WHERE id=? AND organization_id=?", (internal_code, after, destination_lot_id, organization_id))
        if existing:
            after = before + quantity
        destination_name = db.execute("SELECT name FROM units WHERE id=? AND organization_id=?", (transfer["destination_unit_id"], organization_id)).fetchone()["name"]
        origin_name = db.execute("SELECT name FROM units WHERE id=? AND organization_id=?", (transfer["origin_unit_id"], organization_id)).fetchone()["name"]
        notes = "; ".join(part for part in (transfer["notes"], transfer["discrepancy_reason"], transfer["discrepancy_notes"], reason) if part)
        self.insert_movement(db, transfer["destination_unit_id"], transfer["medicine_id"], destination_lot_id, user["id"], "Entrada",
            quantity, before, after, {"reason": "Recebimento de transferência", "source": origin_name,
            "destination": destination_name, "document": f"TRF-{transfer['id']:06d}",
            "responsible": user["full_name"], "notes": notes}, organization_id)
        return destination_lot_id

    def movement_report(self, db, unit_id, organization_id=None):
        if organization_id is None:
            organization_id = db.execute("SELECT organization_id FROM units WHERE id=?", (unit_id,)).fetchone()[0]
        query = parse_qs(urlparse(self.path).query)
        period = query.get("period", ["weekly"])[0]
        if period not in ("weekly", "monthly", "custom"):
            raise ApiError(400, "Selecione um período válido.")
        raw_reference = query.get("date", [date.today().isoformat()])[0]
        try:
            reference = date.fromisoformat(raw_reference)
        except (TypeError, ValueError):
            raise ApiError(400, "Informe uma data de referência válida no formato AAAA-MM-DD.")
        if period == "custom":
            start = date.fromisoformat(checked_date(query.get("start", [""])[0], "Data inicial"))
            end = date.fromisoformat(checked_date(query.get("end", [""])[0], "Data final"))
            if end < start:
                raise ApiError(400, "A data final deve ser igual ou posterior à inicial.")
            label = "Personalizado"
        elif period == "weekly":
            start = reference - timedelta(days=reference.weekday())
            end = start + timedelta(days=6)
            label = "Semanal"
        else:
            start = date(reference.year, reference.month, 1)
            end = date(reference.year, reference.month, monthrange(reference.year, reference.month)[1])
            label = "Mensal"
        rows = [dict(row) for row in db.execute("""
            SELECT m.id AS medicine_id, m.name AS medicine_name, m.concentration,
                   m.dosage_form, m.presentation, m.administration_route, m.manufacturer_barcode, m.stock_unit,
                   COALESCE(SUM(CASE WHEN mv.kind='Entrada' THEN mv.quantity ELSE 0 END),0) AS entries,
                   COALESCE(SUM(CASE WHEN mv.kind='Saída' THEN mv.quantity ELSE 0 END),0) AS exits,
                   COALESCE(SUM(CASE WHEN mv.kind='Ajuste' THEN mv.quantity ELSE 0 END),0) AS adjustments,
                   SUM(CASE WHEN mv.kind='Entrada' THEN 1 ELSE 0 END) AS entry_records,
                   SUM(CASE WHEN mv.kind='Saída' THEN 1 ELSE 0 END) AS exit_records
            FROM movements mv JOIN medicines m ON m.id=mv.medicine_id
            WHERE mv.unit_id=? AND mv.organization_id=? AND m.organization_id=? AND substr(mv.created_at,1,10) BETWEEN ? AND ?
            GROUP BY m.id
            HAVING SUM(CASE WHEN mv.kind IN ('Entrada','Saída','Ajuste') THEN 1 ELSE 0 END)>0
            ORDER BY m.name COLLATE NOCASE
        """, (unit_id, organization_id, organization_id, start.isoformat(), end.isoformat())).fetchall()]
        medicine_id = query.get("medicine_id", [""])[0]
        if medicine_id:
            medicine_id = positive_int(medicine_id, "Medicamento")
            rows = [row for row in rows if row["medicine_id"] == medicine_id]
        totals = {
            "entries": sum(row["entries"] for row in rows),
            "exits": sum(row["exits"] for row in rows),
            "adjustments": sum(row["adjustments"] for row in rows),
            "entry_records": sum(row["entry_records"] for row in rows),
            "exit_records": sum(row["exit_records"] for row in rows),
        }
        unit_name = db.execute("SELECT name FROM units WHERE id=? AND organization_id=?", (unit_id, organization_id)).fetchone()["name"]
        return {"period": period, "period_label": label, "reference_date": reference.isoformat(),
                "start_date": start.isoformat(), "end_date": end.isoformat(), "unit": unit_name,
                "items": rows, "totals": totals}

    def alerts(self, db, unit_id, organization_id=None):
        if organization_id is None:
            organization_id = db.execute("SELECT organization_id FROM units WHERE id=?", (unit_id,)).fetchone()[0]
        today = date.today()
        lots = db.execute("""
                 SELECT l.id, l.lot_number, l.quantity, l.expiration_date, m.id AS medicine_id,
                     m.name AS medicine_name, m.concentration, m.dosage_form, m.presentation,
                     m.administration_route, m.minimum_stock, m.stock_unit
            FROM lots l JOIN medicines m ON m.id = l.medicine_id
            WHERE l.unit_id = ? AND l.organization_id=? AND m.organization_id=? AND l.quantity > 0 AND l.expiration_date <= ?
            ORDER BY l.expiration_date
        """, (unit_id, organization_id, organization_id, (today + timedelta(days=90)).isoformat())).fetchall()
        result = []
        for row in lots:
            days = (date.fromisoformat(row["expiration_date"]) - today).days
            if days <= 90:
                result.append({"type": "expiry", "severity": "expired" if days < 0 else "critical" if days <= 30 else "warning" if days <= 60 else "notice", "days_remaining": days, **dict(row)})
        stock = db.execute("""
                 SELECT m.id AS medicine_id, m.name AS medicine_name, m.concentration, m.dosage_form, m.presentation,
                     m.administration_route, m.minimum_stock, m.stock_unit,
                   COALESCE(SUM(l.quantity),0) AS stock
            FROM medicines m LEFT JOIN lots l ON l.medicine_id=m.id AND l.unit_id=? AND l.organization_id=?
            WHERE m.organization_id=? AND m.active=1 AND m.minimum_stock>0 GROUP BY m.id
        """, (unit_id, organization_id, organization_id)).fetchall()
        for row in stock:
            if row["stock"] < row["minimum_stock"]:
                result.append({"type": "low_stock", "severity": "critical" if row["stock"] == 0 else "warning", **dict(row), "recommended_restock": max(row["minimum_stock"] * 2 - row["stock"], 0)})
        return sorted(result, key=lambda item: (0 if item["severity"] in ("expired", "critical") else 1, item.get("days_remaining", 999), item["medicine_name"]))

    @serialized_request
    def do_POST(self):
        path = urlparse(self.path).path
        db = None
        try:
            payload = self.read_json()
            if path == "/api/register":
                if settings.production() and os.environ.get('ALLOW_REGISTRATION', '0') != '1':
                    raise ApiError(403, 'Cadastro público indisponível. Procure o administrador.')
                self.require_same_origin()
                self.register_organization(payload)
                return
            if path == "/api/login":
                if self.headers.get('Origin'):
                    self.require_same_origin()
                self.login(payload)
                return
            if path == "/api/password-reset/request":
                self.request_password_reset(payload)
                return
            if path == "/api/password-reset/confirm":
                self.confirm_password_reset(payload)
                return
            user = self.get_user()
            self.require_csrf(user)
            if path == "/api/session/extend":
                token = self.cookie_value()
                expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
                db = connect_db()
                db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=? AND user_id=? AND organization_id IS ?",
                           (expires, hashlib.sha256(token.encode()).hexdigest(), user["id"], user["organization_id"]))
                db.commit()
                secure = "; Secure" if settings.secure_cookie() else ""
                self.send_json(200, {"expiresAt": expires}, {"Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"})
                return
            if path == "/api/organizations/select":
                if user["role"] != "SuperAdmin":
                    raise ApiError(403, "Apenas o SuperAdmin pode selecionar organizações.")
                organization_id = positive_int(payload.get("organization_id"), "Organização")
                db = connect_db()
                if not db.execute("SELECT 1 FROM organizations WHERE id=? AND active=1", (organization_id,)).fetchone():
                    raise ApiError(404, "Organização não encontrada ou inativa.")
                db.execute("UPDATE sessions SET organization_id=? WHERE token_hash=? AND user_id=?",
                    (organization_id, hashlib.sha256(self.cookie_value().encode()).hexdigest(), user["id"]))
                db.commit()
                db.close()
                self.send_json(200, {"organization_id": organization_id})
                return
            if path == "/api/organizations":
                if user["role"] != "SuperAdmin":
                    raise ApiError(403, "Apenas o SuperAdmin pode cadastrar organizações.")
                db = connect_db()
                result = self.create_organization(db, payload)
                db.close()
                self.send_json(201, result)
                return
            if path == "/api/reports/pdf":
                db = connect_db()
                unit_id = self.get_unit_id(db, user)
                try:
                    result = self.generate_pdf_file(db, user, unit_id, payload)
                    self.send_json(201, result)
                except Exception as error:
                    logging.getLogger("pharmacia").error("pdf_failed error_type=%s", type(error).__name__)
                    self.send_json(error.status if isinstance(error, ApiError) else 500,
                                   {"error": "Não foi possível gerar o PDF. Tente novamente."})
                return
            if path in ("/api/backups", "/api/backups/restore"):
                if user["role"] != "SuperAdmin":
                    raise ApiError(403, "Apenas o SuperAdmin pode gerenciar backups completos.")
                if path.endswith("/restore"):
                    self.restore_backup(user, payload)
                    self.send_json(200, {"ok": True})
                else:
                    self.send_json(201, {"name": backup_database()})
                return
            if path == "/api/profile/photo":
                photo = validate_profile_photo(payload.get("photo"))
                db = connect_db()
                db.execute("UPDATE users SET profile_photo=? WHERE id=? AND organization_id IS ? AND is_superadmin=?",
                            (photo, user["id"], user.get("account_organization_id"), user["is_superadmin"]))
                db.commit()
                self.send_json(200, {"profilePhoto": photo})
                return
            if path == "/api/logout":
                token = self.cookie_value()
                db = connect_db()
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
                db.commit()
                db.close()
                self.send_json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if settings.secure_cookie() else "")})
                return
            if path == "/api/password":
                current = payload.get("current_password", "")
                new_password = payload.get("new_password", "")
                if not isinstance(current, str) or not isinstance(new_password, str) or len(new_password) < 12 or len(new_password) > 256:
                    raise ApiError(400, "A nova senha deve ter entre 12 e 256 caracteres.")
                db = connect_db()
                account = db.execute("SELECT password_hash FROM users WHERE id=? AND organization_id IS ? AND is_superadmin=?",
                                     (user["id"], user.get("account_organization_id"), user["is_superadmin"])).fetchone()
                if not account or not verify_password(current, account["password_hash"]):
                    db.close()
                    raise ApiError(400, "A senha atual está incorreta.")
                token_hash = hashlib.sha256(self.cookie_value().encode()).hexdigest()
                db.execute("UPDATE users SET password_hash=? WHERE id=? AND organization_id IS ? AND is_superadmin=?",
                            (password_hash(new_password), user["id"], user.get("account_organization_id"), user["is_superadmin"]))
                if user["is_superadmin"]:
                    db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], token_hash))
                else:
                    db.execute("DELETE FROM sessions WHERE user_id=? AND organization_id=? AND token_hash<>?",
                               (user["id"], user["organization_id"], token_hash))
                db.commit()
                db.close()
                self.send_json(200, {"ok": True})
                return
            db = connect_db()
            unit_id = self.get_unit_id(db, user)
            if path == "/api/notifications/read":
                result = notifications.mark_read(db, user, payload)
            elif path in ("/api/orders", "/api/monthly-needs") or path.startswith("/api/orders/"):
                result = self.catalog_service(db, user, unit_id).post(path, payload)
            elif path == "/api/medicines":
                result = self.create_medicine(db, user, payload)
            elif path == "/api/entries":
                result = self.create_entry(db, user, unit_id, payload)
            elif path == "/api/losses":
                result = self.create_loss(db, user, unit_id, payload)
            elif path == "/api/requisitions":
                result = self.create_requisition(db, user, unit_id, payload)
            elif path.startswith("/api/requisitions/"):
                result = self.decide_requisition(db, user, unit_id, path.rsplit("/",1)[1], payload)
            elif path == "/api/exits":
                result = self.create_exit(db, user, unit_id, payload)
            elif path == "/api/quick-exits":
                result = self.quick_exit(db, user, unit_id, payload)
            elif path == "/api/inventory":
                result = self.create_inventory_adjustment(db, user, unit_id, payload)
            elif path == "/api/users":
                result = self.create_user(db, user, payload)
            elif path.startswith("/api/users/"):
                result = self.update_user(db, user, path.rsplit("/", 1)[1], payload)
            elif path == "/api/units":
                result = self.create_unit(db, user, payload)
            elif path == "/api/transfers":
                result = self.create_transfer(db, user, unit_id, payload)
            elif path.startswith("/api/transfers/"):
                result = self.transfer_action(db, user, unit_id, path, payload)
            else:
                raise ApiError(404, "Rota não encontrada.")
            db.close()
            self.send_json(201, result)
        except ApiError as error:
            self.send_error_json(error)
        except sqlite3.IntegrityError as error:
            self.send_json(409, {"error": "Registro duplicado ou referência inválida."})
        except Exception as error:
            logging.getLogger("pharmacia").error("request_failed method=%s error_type=%s", getattr(self,"command","unknown"), type(error).__name__)
            self.send_json(500, {"error": "Erro interno. Consulte o log do servidor."})
        finally:
            if db is not None:
                db.close()

    @serialized_request
    def do_PUT(self):
        path = urlparse(self.path).path
        db = None
        try:
            payload = self.read_json() if int(self.headers.get("Content-Length", "0")) > 0 else {}
            user = self.get_user()
            self.require_csrf(user)
            if not path.startswith("/api/medicines/"):
                raise ApiError(404, "Rota não encontrada.")
            medicine_id = path.rsplit("/", 1)[1]
            db = connect_db()
            result = self.update_medicine(db, user, medicine_id, payload)
            db.close()
            self.send_json(200, result)
        except ApiError as error:
            self.send_error_json(error)
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "Registro duplicado ou referência inválida."})
        except Exception as error:
            logging.getLogger("pharmacia").error("request_failed method=%s error_type=%s", getattr(self,"command","unknown"), type(error).__name__)
            self.send_json(500, {"error": "Erro interno. Consulte o log do servidor."})
        finally:
            if db is not None:
                db.close()

    @serialized_request
    def do_DELETE(self):
        path = urlparse(self.path).path
        db = None
        try:
            payload = self.read_json() if int(self.headers.get("Content-Length", "0")) > 0 else {}
            user = self.get_user()
            self.require_csrf(user)
            if not path.startswith("/api/medicines/"):
                raise ApiError(404, "Rota não encontrada.")
            medicine_id = path.rsplit("/", 1)[1]
            db = connect_db()
            result = self.delete_medicine(db, user, medicine_id, payload)
            db.close()
            self.send_json(200, result)
        except ApiError as error:
            self.send_error_json(error)
        except Exception as error:
            logging.getLogger("pharmacia").error("request_failed method=%s error_type=%s", getattr(self,"command","unknown"), type(error).__name__)
            self.send_json(500, {"error": "Erro interno. Consulte o log do servidor."})
        finally:
            if db is not None:
                db.close()

    def create_user(self, db, actor, payload):
        if not is_organization_admin(actor):
            raise ApiError(403, "Apenas administradores da organização podem criar usuários.")
        organization_id = require_organization(actor)
        username = clean_text(payload.get("username"), "o usuário", 80, True).lower()
        full_name = clean_text(payload.get("full_name"), "o nome completo", 180, True)
        email = self.clean_email(payload.get("email", ""))
        password = payload.get("password", "")
        role = clean_text(payload.get("role"), "o perfil", 40, True)
        if role not in ROLES:
            raise ApiError(400, "Perfil de usuário inválido.")
        if not isinstance(password, str) or len(password) < 12 or len(password) > 256:
            raise ApiError(400, "A senha deve ter entre 12 e 256 caracteres.")
        stored_role = "Administrador" if role == "Administrador da Organização" else role
        unit_ids = self.valid_unit_ids(db, payload.get("unit_ids"), required=role not in ("Administrador", "Administrador da Organização"), organization_id=organization_id)
        cursor = db.execute("""INSERT INTO users(username, full_name, email, password_hash, role, active, created_at, organization_id)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)""", (username, full_name, email, password_hash(password), stored_role, now_iso(), organization_id))
        self.replace_user_units(db, cursor.lastrowid, unit_ids, organization_id)
        record_audit(db, actor, "users", cursor.lastrowid, {})
        db.commit()
        return {"id": cursor.lastrowid, "username": username, "full_name": full_name, "email": email, "role": role}

    def create_unit(self, db, actor, payload):
        if not is_organization_admin(actor):
            raise ApiError(403, "Apenas administradores da organização podem cadastrar unidades.")
        organization_id = require_organization(actor)
        name = clean_text(payload.get("name"), "o nome da USF", 180, True)
        cursor = db.execute("INSERT INTO units(name, created_at, organization_id) VALUES (?, ?, ?)", (name, now_iso(), organization_id))
        db.commit()
        return {"id": cursor.lastrowid, "name": name}

    def update_user(self, db, actor, raw_id, payload):
        if not is_organization_admin(actor):
            raise ApiError(403, "Apenas administradores da organização podem editar usuários.")
        organization_id = require_organization(actor)
        user_id = positive_int(raw_id, "Usuário")
        target = db.execute("SELECT * FROM users WHERE id=? AND organization_id=? AND is_superadmin=0", (user_id, organization_id)).fetchone()
        if not target:
            raise ApiError(404, "Usuário não encontrado.")
        if user_id == actor["id"] and (payload.get("active") is False or payload.get("role") not in (None, "Administrador", "Administrador da Organização")):
            raise ApiError(400, "Você não pode desativar ou rebaixar sua própria conta.")
        username = clean_text(payload.get("username", target["username"]), "o usuário", 80, True).lower()
        full_name = clean_text(payload.get("full_name", target["full_name"]), "o nome completo", 180, True)
        email = self.clean_email(payload.get("email", target["email"]))
        role = clean_text(payload.get("role", target["role"]), "o perfil", 40, True)
        if role not in ROLES:
            raise ApiError(400, "Perfil de usuário inválido.")
        active = 1 if payload.get("active", bool(target["active"])) else 0
        if not active and target["active"] and target["role"] == "Administrador":
            administrators = db.execute("SELECT COUNT(*) AS total FROM users WHERE role='Administrador' AND active=1 AND is_superadmin=0 AND organization_id=? AND id<>?", (organization_id, user_id)).fetchone()["total"]
            if administrators == 0:
                raise ApiError(400, "Não é possível desativar o último administrador ativo.")
        before = audit_snapshot(db, "users", user_id, organization_id)
        new_password = payload.get("password")
        if new_password is not None:
            if not isinstance(new_password, str) or len(new_password) < 12 or len(new_password) > 256:
                raise ApiError(400, "A senha deve ter entre 12 e 256 caracteres.")
            db.execute("UPDATE users SET password_hash=? WHERE id=? AND organization_id=?", (password_hash(new_password), user_id, organization_id))
            db.execute("DELETE FROM sessions WHERE user_id=? AND organization_id=?", (user_id, organization_id))
            db.execute("DELETE FROM login_security WHERE username IN (?, ?)", (target["username"].lower(), username))
            db.execute("DELETE FROM password_resets WHERE user_id=? AND organization_id=?", (user_id, organization_id))
        stored_role = "Administrador" if role == "Administrador da Organização" else role
        unit_ids = self.valid_unit_ids(db, payload.get("unit_ids"), required=role not in ("Administrador", "Administrador da Organização"), organization_id=organization_id) if "unit_ids" in payload else None
        db.execute("UPDATE users SET username=?, full_name=?, email=?, role=?, active=? WHERE id=? AND organization_id=?",
                   (username, full_name, email, stored_role, active, user_id, organization_id))
        if unit_ids is not None:
            self.replace_user_units(db, user_id, unit_ids, organization_id)
        record_audit(db, actor, "users", user_id, before, password_changed=new_password is not None)
        db.commit()
        return {"id": user_id, "username": username, "full_name": full_name, "email": email, "role": role, "active": active}

    def clean_email(self, value):
        email = clean_text(value, "o e-mail", 180)
        if email and (parseaddr(email)[1] != email or "@" not in email):
            raise ApiError(400, "Informe um endereço de e-mail válido.")
        return email

    def valid_unit_ids(self, db, raw_ids, required=False, organization_id=None):
        if organization_id is None:
            raise ApiError(403, "Organização não definida na sessão.")
        if raw_ids is None:
            raw_ids = []
        if not isinstance(raw_ids, list):
            raise ApiError(400, "Unidades inválidas.")
        try:
            unit_ids = sorted(set(int(value) for value in raw_ids))
        except (TypeError, ValueError):
            raise ApiError(400, "Unidades inválidas.")
        if any(value < 1 for value in unit_ids):
            raise ApiError(400, "Unidades inválidas.")
        found = {row["id"] for row in db.execute("SELECT id FROM units WHERE organization_id=? AND id IN (%s)" % ",".join("?" * len(unit_ids)), [organization_id, *unit_ids]).fetchall()} if unit_ids else set()
        if found != set(unit_ids):
            raise ApiError(400, "Uma ou mais unidades não foram encontradas.")
        if required and not unit_ids:
            raise ApiError(400, "Vincule pelo menos uma unidade a este perfil.")
        return unit_ids

    def replace_user_units(self, db, user_id, unit_ids, organization_id):
        db.execute("DELETE FROM user_units WHERE user_id=? AND organization_id=?", (user_id, organization_id))
        if unit_ids:
            db.executemany("INSERT INTO user_units(user_id, unit_id, organization_id) VALUES (?, ?, ?)",
                           [(user_id, unit_id, organization_id) for unit_id in unit_ids])

    def request_password_reset(self, payload):
        username = clean_text(payload.get("username"), "usuário", 80, True).lower()
        message = {"ok": True, "message": "Se a conta possuir e-mail cadastrado, você receberá um código. Caso não receba, procure o administrador para redefinir a senha e desbloquear o acesso."}
        db = connect_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM users WHERE lower(username)=? AND active=1", (username,)).fetchone()
            host = os.environ.get("FARMACIA_SMTP_HOST", "").strip()
            sender = os.environ.get("FARMACIA_SMTP_FROM", "").strip()
            if not row or not row["email"] or not host or not sender:
                db.rollback()
                self.send_json(200, message)
                return
            previous = db.execute("SELECT requested_at FROM password_resets WHERE user_id=? AND organization_id IS ?",
                                  (row["id"], row["organization_id"])).fetchone()
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
            if previous and previous["requested_at"] > cutoff:
                db.rollback()
                self.send_json(200, message)
                return
            code = secrets.token_hex(8).upper()
            expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds")
            db.execute("INSERT OR REPLACE INTO password_resets(user_id,token_hash,expires_at,requested_at,attempts,organization_id) VALUES(?,?,?,?,0,?)",
                       (row["id"], hashlib.sha256(code.encode()).hexdigest(), expires, now_iso(), row["organization_id"]))
            db.commit()
            mail = EmailMessage()
            mail["Subject"] = "Redefinição de senha - Pharmacia"
            mail["From"] = sender
            mail["To"] = row["email"]
            mail.set_content(f"Seu código de redefinição é: {code}\n\nVálido por 15 minutos e para uso único. Se você não solicitou a redefinição, ignore esta mensagem.")
            try:
                with smtplib.SMTP(host, int(os.environ.get("FARMACIA_SMTP_PORT", "587")), timeout=10) as client:
                    client.ehlo()
                    if os.environ.get("FARMACIA_SMTP_TLS", "1") != "0":
                        client.starttls(context=ssl.create_default_context())
                        client.ehlo()
                    if os.environ.get("FARMACIA_SMTP_USER"):
                        client.login(os.environ["FARMACIA_SMTP_USER"], os.environ.get("FARMACIA_SMTP_PASSWORD", ""))
                    client.send_message(mail)
            except (OSError, smtplib.SMTPException, ValueError):
                print("Falha no envio de e-mail de redefinição de senha.", file=sys.stderr)
            self.send_json(200, message)
        finally:
            db.close()

    def confirm_password_reset(self, payload):
        username = clean_text(payload.get("username"), "usuário", 80, True).lower()
        code = clean_text(payload.get("code"), "código", 100, True).upper()
        password = payload.get("new_password")
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ApiError(400, "A senha deve ter entre 12 e 256 caracteres.")
        db = connect_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.*, u.username FROM password_resets r JOIN users u ON u.id=r.user_id WHERE lower(u.username)=? AND u.active=1 AND r.organization_id IS u.organization_id", (username,)).fetchone()
            if not row or row["expires_at"] <= now_iso() or row["attempts"] >= 5:
                raise ApiError(400, "Código inválido ou expirado. Solicite um novo código.")
            if not hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), row["token_hash"]):
                db.execute("UPDATE password_resets SET attempts=attempts+1 WHERE user_id=? AND organization_id IS ?",
                           (row["user_id"], row["organization_id"]))
                db.commit()
                raise ApiError(400, "Código inválido ou expirado. Solicite um novo código se necessário.")
            db.execute("UPDATE users SET password_hash=? WHERE id=? AND organization_id IS ?",
                        (password_hash(password), row["user_id"], row["organization_id"]))
            db.execute("DELETE FROM sessions WHERE user_id=? AND organization_id IS ?", (row["user_id"], row["organization_id"]))
            db.execute("DELETE FROM password_resets WHERE user_id=? AND organization_id IS ?", (row["user_id"], row["organization_id"]))
            db.execute("DELETE FROM login_security WHERE username=?", (username,))
            db.commit()
            self.send_json(200, {"ok": True})
        finally:
            db.close()

    def login(self, payload):
        username = clean_text(payload.get("username"), "usuário", 80, True).lower()
        password = payload.get("password", "")
        if not isinstance(password, str) or len(password) > 256:
            raise ApiError(400, "Credenciais inválidas.")
        db = connect_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            security = db.execute("SELECT failures FROM login_security WHERE username=?", (username,)).fetchone()
            failures = security["failures"] if security else 0
            if failures >= 5:
                db.rollback()
                self.send_json(423, {"error": "Login bloqueado após 5 tentativas incorretas. Redefina sua senha ou procure o administrador.", "reset_required": True, "locked": True})
                return
            row = db.execute("SELECT * FROM users WHERE lower(username)=? AND active=1", (username,)).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                failures += 1
                db.execute("INSERT INTO login_security(username,failures) VALUES(?,?) ON CONFLICT(username) DO UPDATE SET failures=excluded.failures", (username, failures))
                db.commit()
                message = "Usuário ou senha incorretos."
                if failures >= 5:
                    message = "Login bloqueado após 5 tentativas incorretas. Redefina sua senha ou procure o administrador."
                elif failures >= 3:
                    message = f"Senha incorreta. Recomendamos redefinir sua senha. Restam {5 - failures} tentativa(s) antes do bloqueio."
                self.send_json(423 if failures >= 5 else 401, {"error": message, "reset_required": failures >= 3, "locked": failures >= 5})
                return
            db.execute("DELETE FROM login_security WHERE username=?", (username,))
            raw_token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
            db.execute("INSERT INTO sessions(token_hash, csrf_token, user_id, expires_at, created_at, organization_id) VALUES (?, ?, ?, ?, ?, ?)",
                       (hashlib.sha256(raw_token.encode()).hexdigest(), csrf, row["id"], expires, now_iso(), row["organization_id"]))
            db.commit()
        finally:
            db.close()
        secure = "; Secure" if settings.secure_cookie() else ""
        self.send_json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE_NAME}={raw_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"})

    def create_medicine(self, db, user, payload):
        if user["role"] not in ("Administrador", "Administrador da Organização", "SuperAdmin", "Enfermeiro"):
            raise ApiError(403, "Seu perfil não pode cadastrar medicamentos.")
        if getattr(db, 'dialect', '') == 'postgresql':
            db.begin()
        catalog_orders.duplicate(db, require_organization(user), payload)
        name = clean_text(payload.get("name"), "o nome do medicamento", 180, True)
        code = "MED-" + secrets.token_hex(5).upper()
        minimum = positive_int(payload.get("minimum_stock", 0), "Estoque mínimo", True)
        stock_unit = clean_text(payload.get("stock_unit"), "a unidade de estoque", 40, True)
        values = [name, "", clean_text(payload.get("concentration"), "concentração", 100), clean_text(payload.get("dosage_form"), "forma farmacêutica", 120),
                  clean_text(payload.get("presentation"), "apresentação / embalagem", 100), stock_unit,
              None, "", stock_unit,
                  clean_text(payload.get("administration_route"), "via de administração", 80),
              "Geral", code,
                  clean_text(payload.get("manufacturer_barcode"), "código de barras", 100), minimum,
                  clean_text(payload.get("notes"), "observações", 1000), now_iso()]
        cursor = db.execute("""INSERT INTO medicines(name,active_ingredient,concentration,dosage_form,presentation,unit,package_quantity,package_unit,stock_unit,administration_route,category,code,manufacturer_barcode,minimum_stock,notes,created_at,organization_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [*values, require_organization(user)])
        catalog_orders.update_metadata(db, require_organization(user), cursor.lastrowid, payload)
        record_audit(db, user, "medicines", cursor.lastrowid, {})
        db.commit()
        return {"id": cursor.lastrowid, "code": code, "name": name}

    def update_medicine(self, db, user, raw_id, payload):
        if user["role"] not in ("Administrador", "Administrador da Organização", "SuperAdmin", "Enfermeiro"):
            raise ApiError(403, "Seu perfil não pode editar medicamentos.")
        if getattr(db, 'dialect', '') == 'postgresql':
            db.begin()
        medicine_id = positive_int(raw_id, "Medicamento")
        organization_id = require_organization(user)
        row = db.execute("SELECT * FROM medicines WHERE id=? AND organization_id=? AND active=1", (medicine_id, organization_id)).fetchone()
        if not row:
            raise ApiError(404, "Medicamento não encontrado.")
        catalog_orders.duplicate(db, organization_id, {**dict(row), **payload}, medicine_id)
        before = audit_snapshot(db, "medicines", medicine_id, require_organization(user))
        new_name = clean_text(payload.get("name", row["name"]), "o nome do medicamento", 180, True)
        new_code = row["code"]
        minimum = positive_int(payload.get("minimum_stock", row["minimum_stock"]), "Estoque mínimo", True)
        stock_unit = clean_text(payload.get("stock_unit", row["stock_unit"]), "a unidade de estoque", 40, True)
        if stock_unit != row["stock_unit"] and db.execute("SELECT 1 FROM movements WHERE medicine_id=? AND organization_id=? LIMIT 1", (medicine_id, organization_id)).fetchone():
            raise ApiError(409, "A unidade de estoque não pode ser alterada após movimentações. Cadastre uma nova apresentação para utilizar outra unidade.")
        db.execute("""UPDATE medicines SET
            name=?, active_ingredient=?, concentration=?, dosage_form=?, presentation=?, unit=?, package_quantity=?, package_unit=?, stock_unit=?, administration_route=?, category=?, code=?, manufacturer_barcode=?, minimum_stock=?, notes=?
            WHERE id=? AND organization_id=?""",
            (
                new_name,
                row["active_ingredient"],
                clean_text(payload.get("concentration", row["concentration"]), "concentração", 100),
                clean_text(payload.get("dosage_form", row["dosage_form"]), "forma farmacêutica", 120),
                clean_text(payload.get("presentation", row["presentation"]), "apresentação / embalagem", 100),
                stock_unit,
                row["package_quantity"],
                row["package_unit"],
                stock_unit,
                clean_text(payload.get("administration_route", row["administration_route"]), "via de administração", 80),
                row["category"],
                new_code,
                clean_text(payload.get("manufacturer_barcode", row["manufacturer_barcode"]), "código de barras", 100),
                minimum,
                clean_text(payload.get("notes", row["notes"]), "observações", 1000),
                medicine_id,
                organization_id,
            ))
        catalog_orders.update_metadata(db, organization_id, medicine_id, payload)
        record_audit(db, user, "medicines", medicine_id, before)
        db.commit()
        return {"id": medicine_id, "code": new_code, "name": new_name}

    def delete_medicine(self, db, user, raw_id, payload):
        if user["role"] not in ("Administrador", "Administrador da Organização", "SuperAdmin", "Enfermeiro"):
            raise ApiError(403, "Seu perfil não pode excluir medicamentos.")
        medicine_id = positive_int(raw_id, "Medicamento")
        organization_id = require_organization(user)
        row = db.execute("SELECT * FROM medicines WHERE id=? AND organization_id=? AND active=1", (medicine_id, organization_id)).fetchone()
        if not row:
            raise ApiError(404, "Medicamento não encontrado.")
        before = audit_snapshot(db, "medicines", medicine_id, organization_id)
        db.execute("UPDATE medicines SET active=0 WHERE id=? AND organization_id=?", (medicine_id, organization_id))
        record_audit(db, user, "medicines", medicine_id, before)
        db.commit()
        return {"id": medicine_id, "deleted": True, "name": row["name"]}

    def create_entry(self, db, user, unit_id, payload, commit=True):
        if not can_manage(user):
            raise ApiError(403, "Seu perfil não pode registrar entradas.")
        medicine_id = positive_int(payload.get("medicine_id"), "Medicamento")
        quantity = positive_int(payload.get("quantity"), "Quantidade")
        lot_number = clean_text(payload.get("lot_number"), "o número do lote", 100, True)
        expiry = checked_date(payload.get("expiration_date"), "Validade")
        manufacture = payload.get("manufacture_date")
        manufacture = checked_date(manufacture, "Data de fabricação") if manufacture else None
        organization_id = require_organization(user)
        medicine = db.execute("SELECT id, stock_unit FROM medicines WHERE id=? AND organization_id=? AND active=1", (medicine_id, organization_id)).fetchone()
        if not medicine:
            raise ApiError(404, "Medicamento não encontrado ou inativo.")
        if expiry < date.today().isoformat():
            raise ApiError(400, "Não é possível receber um lote vencido.")
        if manufacture and manufacture > expiry:
            raise ApiError(400, "A fabricação não pode ser posterior à validade.")
        if commit:
            db.execute("BEGIN IMMEDIATE")
        lot = db.execute("SELECT * FROM lots WHERE organization_id=? AND unit_id=? AND medicine_id=? AND lot_number=?", (organization_id, unit_id, medicine_id, lot_number)).fetchone()
        if lot and lot["expiration_date"] != expiry:
            raise ApiError(409, "O lote já existe com outra validade; confira o cadastro antes de receber.")
        if lot:
            lot_id, before = lot["id"], lot["quantity"]
            db.execute("UPDATE lots SET quantity=quantity+? WHERE id=? AND organization_id=?", (quantity, lot_id, organization_id))
        else:
            cursor = db.execute("INSERT INTO lots(unit_id,medicine_id,lot_number,internal_code,manufacture_date,expiration_date,quantity,created_at,organization_id) VALUES(?,?,?,?,?,?,0,?,?)",
                (unit_id, medicine_id, lot_number, "PENDING", manufacture, expiry, now_iso(), organization_id))
            lot_id, before = cursor.lastrowid, 0
            internal = reserve_lot_code(db, medicine_id)
            db.execute("UPDATE lots SET internal_code=?, quantity=? WHERE id=? AND organization_id=?", (internal, quantity, lot_id, organization_id))
        after = before + quantity
        movement_id = self.insert_movement(db, unit_id, medicine_id, lot_id, user["id"], "Entrada", quantity, before, after, payload, organization_id)
        if commit:
            db.commit()
        code = db.execute("SELECT internal_code FROM lots WHERE id=? AND organization_id=?", (lot_id, organization_id)).fetchone()[0]
        return {"movement_id": movement_id, "lot_id": lot_id, "internal_code": code, "stock_before": before, "quantity": quantity, "stock_after": after, "stock_unit": medicine["stock_unit"]}

    def filtered_movements(self, db, unit_id, losses=False, organization_id=None):
        if organization_id is None:
            organization_id = db.execute("SELECT organization_id FROM units WHERE id=?", (unit_id,)).fetchone()[0]
        query = parse_qs(urlparse(self.path).query)
        clauses, params = ["mv.unit_id=?", "mv.organization_id=?", "m.organization_id=?", "l.organization_id=?", "u.organization_id=?"], [unit_id, organization_id, organization_id, organization_id, organization_id]
        start, end = query.get("start", [""])[0], query.get("end", [""])[0]
        if start:
            clauses.append("substr(mv.created_at,1,10)>=?"); params.append(checked_date(start,"Data inicial"))
        if end:
            clauses.append("substr(mv.created_at,1,10)<=?"); params.append(checked_date(end,"Data final"))
        if start and end and start>end:
            raise ApiError(400,"A data final deve ser posterior à inicial.")
        for key, column in [("user","u.full_name"),("medicine","m.name"),("lot","l.lot_number"),("code","l.internal_code")]:
            value=query.get(key,[""])[0].strip()
            if value:
                clauses.append(column+" LIKE ?"); params.append("%"+value+"%")
        if losses:
            clauses.append("mv.exit_category IN ('Vencimento','Avaria','Extravio')")
        sql="""SELECT mv.*,m.name AS medicine_name,m.concentration,m.dosage_form,m.presentation,m.administration_route,
            m.stock_unit,l.lot_number,l.internal_code,u.full_name AS user_name FROM movements mv
            JOIN medicines m ON m.id=mv.medicine_id JOIN lots l ON l.id=mv.lot_id JOIN users u ON u.id=mv.user_id
            WHERE """+" AND ".join(clauses)+" ORDER BY mv.created_at DESC,mv.id DESC"
        return {"items":[dict(r) for r in db.execute(sql,params)]}

    def create_loss(self, db, user, unit_id, payload):
        self.require_transfer_resolution(user)
        organization_id = require_organization(user)
        category=clean_text(payload.get("category"),"Motivo",40,True)
        if category not in ("Vencimento","Avaria","Extravio"):
            raise ApiError(400,"Selecione um motivo de perda válido.")
        reason=clean_text(payload.get("reason"),"Justificativa",100,True)
        quantity=positive_int(payload.get("quantity"),"Quantidade")
        lot_id=positive_int(payload.get("lot_id"),"Lote")
        db.execute("BEGIN IMMEDIATE")
        lot=db.execute("SELECT * FROM lots WHERE id=? AND unit_id=? AND organization_id=?",(lot_id,unit_id,organization_id)).fetchone()
        if not lot:
            raise ApiError(404,"Lote não encontrado nesta unidade.")
        if quantity>lot["quantity"]:
            raise ApiError(409,"Quantidade maior que o saldo disponível.")
        if category=="Vencimento" and lot["expiration_date"]>=date.today().isoformat():
            raise ApiError(409,"O lote ainda não está vencido.")
        after=lot["quantity"]-quantity
        db.execute("UPDATE lots SET quantity=? WHERE id=? AND organization_id=?",(after,lot_id,organization_id))
        self.insert_movement(db,unit_id,lot["medicine_id"],lot_id,user["id"],"Saída",quantity,lot["quantity"],after,
            {"reason":reason,"notes":payload.get("notes",""),"responsible":user["full_name"],"exit_category":category}, organization_id)
        db.commit()
        return {"stock_before":lot["quantity"],"stock_after":after,"quantity":quantity}

    def create_requisition(self, db, user, unit_id, payload):
        self.require_transfer_manager(user)
        organization_id = require_organization(user)
        supplier=positive_int(payload.get("supplier_unit_id"),"Unidade fornecedora")
        medicine=positive_int(payload.get("medicine_id"),"Medicamento")
        quantity=positive_int(payload.get("quantity"),"Quantidade")
        reason=clean_text(payload.get("reason"),"Justificativa",500,True)
        if supplier==unit_id or not db.execute("SELECT 1 FROM units WHERE id=? AND organization_id=?",(supplier,organization_id)).fetchone():
            raise ApiError(400,"Selecione outra unidade fornecedora.")
        if not db.execute("SELECT 1 FROM medicines WHERE id=? AND organization_id=? AND active=1",(medicine,organization_id)).fetchone():
            raise ApiError(404,"Medicamento não encontrado.")
        cursor=db.execute("INSERT INTO requisitions(requester_unit_id,supplier_unit_id,medicine_id,quantity,reason,created_by,created_at,organization_id) VALUES(?,?,?,?,?,?,?,?)",
            (unit_id,supplier,medicine,quantity,reason,user["id"],now_iso(),organization_id))
        db.commit()
        return {"id":cursor.lastrowid}

    def decide_requisition(self, db, user, unit_id, raw_id, payload):
        self.require_transfer_resolution(user)
        organization_id = require_organization(user)
        request_id=positive_int(raw_id,"Requisição")
        action=payload.get("action")
        if action not in ("aprovar","recusar","cancelar"):
            raise ApiError(400,"Ação inválida.")
        reason=clean_text(payload.get("reason"),"Justificativa",500,True)
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM requisitions WHERE id=? AND organization_id=?",(request_id,organization_id)).fetchone()
        if not row:
            raise ApiError(404,"Requisição não encontrada.")
        expected=row["requester_unit_id"] if action=="cancelar" else row["supplier_unit_id"]
        if unit_id!=expected:
            raise ApiError(403,"Esta unidade não pode realizar essa ação.")
        self.require_unit_access(user,unit_id)
        if row["status"]!="Pendente":
            raise ApiError(409,"Requisição já analisada.")
        transfer_id=None
        status={"aprovar":"Aprovada","recusar":"Recusada","cancelar":"Cancelada"}[action]
        if action=="aprovar":
            result=self.create_transfer(db,user,unit_id,{"origin_unit_id":unit_id,"destination_unit_id":row["requester_unit_id"],
                "medicine_id":row["medicine_id"],"quantity":row["quantity"],"lot_id":payload.get("lot_id"),
                "responsible":user["full_name"],"notes":f"Requisição REQ-{request_id}: {reason}"},commit=False)
            transfer_id=result["id"]
        db.execute("UPDATE requisitions SET status=?,decided_by=?,decided_at=?,decision_reason=?,transfer_id=? WHERE id=? AND organization_id=?",
            (status,user["id"],now_iso(),reason,transfer_id,request_id,organization_id))
        db.commit()
        if transfer_id:
            try:
                self.send_transfer_email(db, transfer_id, organization_id)
            except Exception as error:
                print(f"Falha de e-mail da requisição: {type(error).__name__}",file=sys.stderr)
        return {"id":request_id,"status":status,"transfer_id":transfer_id}

    def create_exit(self, db, user, unit_id, payload, commit=True):
        if not can_manage(user):
            raise ApiError(403, "Seu perfil não pode registrar saídas.")
        organization_id = require_organization(user)
        medicine_id = positive_int(payload.get("medicine_id"), "Medicamento")
        quantity = positive_int(payload.get("quantity"), "Quantidade")
        reason = clean_text(payload.get("reason"), "o motivo", 100, True)
        category=payload.get("exit_category", "Dispensação" if reason=="Dispensação" else "Outros")
        if category not in ("Dispensação","Devolução","Outros"):
            raise ApiError(400,"Para vencimento, avaria ou extravio, utilize Registrar perda.")
        payload={**payload,"exit_category":category}
        lot_id = payload.get("lot_id")
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        medicine = db.execute("SELECT * FROM medicines WHERE id=? AND organization_id=? AND active=1", (medicine_id, organization_id)).fetchone()
        if not medicine:
            raise ApiError(404, "Medicamento não encontrado ou inativo.")
        params = [unit_id, medicine_id, date.today().isoformat(), organization_id]
        clause = ""
        if lot_id:
            clause = " AND id=?"
            params.append(positive_int(lot_id, "Lote"))
        lots = db.execute("SELECT * FROM lots WHERE unit_id=? AND medicine_id=? AND expiration_date>=? AND organization_id=? AND quantity>0" + clause + " ORDER BY expiration_date,id", params).fetchall()
        available = sum(lot["quantity"] for lot in lots)
        if quantity > available:
            raise ApiError(409, f"Estoque válido insuficiente. Disponível: {available}.")
        remaining, allocations = quantity, []
        for lot in lots:
            used = min(remaining, lot["quantity"])
            after = lot["quantity"] - used
            db.execute("UPDATE lots SET quantity=? WHERE id=? AND organization_id=?", (after, lot["id"], organization_id))
            self.insert_movement(db, unit_id, medicine_id, lot["id"], user["id"], "Saída", used, lot["quantity"], after, payload, organization_id)
            allocations.append({"lot_id": lot["id"], "lot_number": lot["lot_number"], "quantity": used, "stock_after": after})
            remaining -= used
            if remaining == 0:
                break
        if commit:
            db.commit()
        return {"allocations": allocations, "quantity": quantity, "stock_unit": medicine["stock_unit"]}

    def scan_lot(self, db, unit_id, code, organization_id=None):
        if organization_id is None:
            organization_id = db.execute("SELECT organization_id FROM units WHERE id=?", (unit_id,)).fetchone()[0]
        row = db.execute("""SELECT l.*, m.name AS medicine_name, m.concentration, m.dosage_form,
            m.presentation, m.administration_route, m.stock_unit, m.active, u.name AS unit_name
            FROM lots l JOIN medicines m ON m.id=l.medicine_id JOIN units u ON u.id=l.unit_id
            WHERE l.internal_code=? AND l.organization_id=? AND m.organization_id=? AND u.organization_id=?""",
            (code, organization_id, organization_id, organization_id)).fetchone()
        if not row:
            raise ApiError(404, "Código interno de lote não encontrado.")
        if row["unit_id"] != unit_id:
            raise ApiError(403, "O lote não pertence à unidade atualmente selecionada.")
        lot = dict(row)
        blocked = ("Medicamento inativo." if not lot["active"] else
                   "Lote vencido. Saída bloqueada." if lot["expiration_date"] < date.today().isoformat() else
                   "Estoque zerado. Saída bloqueada." if lot["quantity"] <= 0 else "")
        recommended = db.execute("""SELECT lot_number,expiration_date,quantity,internal_code FROM lots
            WHERE unit_id=? AND medicine_id=? AND quantity>0 AND expiration_date>=? AND expiration_date<?
            ORDER BY expiration_date,id LIMIT 1""", (unit_id, lot["medicine_id"], date.today().isoformat(), lot["expiration_date"])).fetchone()
        return {"lot": lot, "blocked": blocked, "recommended": dict(recommended) if recommended else None}

    def quick_exit(self, db, user, unit_id, payload):
        if not can_manage(user):
            raise ApiError(403, "Seu perfil não pode registrar saídas.")
        organization_id = require_organization(user)
        code = clean_text(payload.get("code"), "o código interno", 100, True)
        request_id = clean_text(payload.get("request_id"), "a identificação da operação", 100, True)
        quantity = positive_int(payload.get("quantity"), "Quantidade")
        reason = clean_text(payload.get("reason"), "o motivo", 100, True)
        category=payload.get("exit_category", "Dispensação" if reason=="Dispensação" else "Outros")
        signature = json.dumps([code, quantity, reason, category], ensure_ascii=False)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM quick_exit_requests WHERE request_id=? AND organization_id=?", (request_id, organization_id)).fetchone()
        if previous:
            if previous["user_id"] != user["id"] or previous["unit_id"] != unit_id or previous["payload"] != signature:
                raise ApiError(409, "Identificação de operação já utilizada. Leia o lote novamente.")
            db.commit()
            return json.loads(previous["result"])
        scan = self.scan_lot(db, unit_id, code, organization_id)
        if scan["blocked"]:
            raise ApiError(409, scan["blocked"])
        lot = scan["lot"]
        result = self.create_exit(db, user, unit_id, {"medicine_id": lot["medicine_id"], "lot_id": lot["id"],
            "quantity": quantity, "reason": reason, "exit_category":category, "responsible": user["full_name"], "document": code}, commit=False)
        result.update({"internal_code": code, "stock_before": lot["quantity"], "stock_after": result["allocations"][0]["stock_after"]})
        db.execute("INSERT INTO quick_exit_requests(request_id,user_id,unit_id,payload,result,organization_id) VALUES(?,?,?,?,?,?)",
               (request_id, user["id"], unit_id, signature, json.dumps(result), organization_id))
        db.commit()
        return result


    def create_inventory_adjustment(self, db, user, unit_id, payload):
        if user["role"] not in ("Administrador", "Administrador da Organização", "SuperAdmin", "Enfermeiro"):
            raise ApiError(403, "Seu perfil não pode ajustar o inventário.")
        organization_id = require_organization(user)
        lot_id = positive_int(payload.get("lot_id"), "Lote")
        counted = positive_int(payload.get("counted_quantity"), "Quantidade contada", True)
        reason = clean_text(payload.get("reason"), "o motivo", 240, True)
        db.execute("BEGIN IMMEDIATE")
        lot = db.execute("SELECT * FROM lots WHERE id=? AND unit_id=? AND organization_id=?", (lot_id, unit_id, organization_id)).fetchone()
        if not lot:
            raise ApiError(404, "Lote não encontrado nesta unidade.")
        before, difference = lot["quantity"], counted - lot["quantity"]
        db.execute("INSERT INTO inventory_counts(unit_id,lot_id,user_id,system_quantity,counted_quantity,difference,reason,notes,created_at,organization_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (unit_id, lot_id, user["id"], before, counted, difference, reason, clean_text(payload.get("notes"), "observações", 1000), now_iso(), organization_id))
        if difference:
            db.execute("UPDATE lots SET quantity=? WHERE id=? AND organization_id=?", (counted, lot_id, organization_id))
            self.insert_movement(db, unit_id, lot["medicine_id"], lot_id, user["id"], "Ajuste", abs(difference), before, counted,
                                 {"reason": reason, "notes": payload.get("notes", ""), "responsible": user["full_name"]}, organization_id)
        db.commit()
        stock_unit = db.execute("SELECT m.stock_unit FROM medicines m JOIN lots l ON l.medicine_id=m.id AND l.organization_id=m.organization_id WHERE l.id=? AND l.organization_id=? AND m.organization_id=?",
                    (lot_id, organization_id, organization_id)).fetchone()["stock_unit"]
        return {"lot_id": lot_id, "stock_before": before, "counted_quantity": counted, "difference": difference, "stock_after": counted, "stock_unit": stock_unit}

    def insert_movement(self, db, unit_id, medicine_id, lot_id, user_id, kind, quantity, before, after, payload, organization_id):
        cursor = db.execute("""INSERT INTO movements(unit_id,medicine_id,lot_id,user_id,kind,quantity,stock_before,stock_after,reason,destination,source,document,responsible,notes,created_at,organization_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                unit_id, medicine_id, lot_id, user_id, kind, quantity, before, after,
                clean_text(payload.get("reason"), "motivo", 240), clean_text(payload.get("destination"), "destino", 180),
                clean_text(payload.get("source"), "origem", 180), clean_text(payload.get("document"), "documento", 120),
                clean_text(payload.get("responsible"), "responsável", 180) or "", clean_text(payload.get("notes"), "observações", 1000), now_iso(), organization_id))
        db.execute("UPDATE movements SET exit_category=? WHERE id=? AND organization_id=?",(payload.get("exit_category","Não classificado"),cursor.lastrowid,organization_id))
        return cursor.lastrowid



if __name__ == "__main__":
    settings.load_environment()
    settings.validate()
    DATA = settings.data_directory()
    DATABASE = settings.sqlite_path()
    BACKUPS = DATA / 'backups'
    if settings.production():
        raise SystemExit('Em produção execute python Pharmacia/serve.py (Waitress).')
    initialize_db()
    threading.Thread(target=automatic_backup, daemon=True).start()
    host = os.environ.get('HOST', os.environ.get("FARMACIA_HOST", "127.0.0.1"))
    port = int(os.environ.get('PORT', os.environ.get("FARMACIA_PORT", "8000")))
    print(f"Farmácia UBS disponível em http://{host}:{port}")
    ThreadingHTTPServer((host, port), PharmacyHandler).serve_forever()   
