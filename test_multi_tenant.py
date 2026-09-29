import contextlib
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_password_security import app


class MultiTenantTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        credentials = patch.dict(os.environ, {'FARMACIA_ADMIN_PASSWORD':'Tenant-Fixture-Password-123'})
        credentials.start()
        self.addCleanup(credentials.stop)
        root = Path(temporary.name)
        for name, value in [('DATA', root), ('DATABASE', root / 'test.db'), ('BACKUPS', root / 'backups')]:
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with contextlib.redirect_stderr(io.StringIO()):
            app.initialize_db()
        self.db = app.connect_db()
        self.addCleanup(self.db.close)
        self.handler = object.__new__(app.PharmacyHandler)
        self.handler.path = '/api/medicines'

    def test_legacy_rows_migrate_to_default_organization(self):
        legacy = sqlite3.connect(':memory:')
        legacy.row_factory = sqlite3.Row
        legacy.executescript("""
            CREATE TABLE units(id INTEGER PRIMARY KEY, name TEXT UNIQUE, created_at TEXT);
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, role TEXT);
            CREATE TABLE medicines(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE lots(id INTEGER PRIMARY KEY, lot_number TEXT);
            CREATE TABLE movements(id INTEGER PRIMARY KEY, quantity INTEGER);
            CREATE TABLE unit_references(unit_id INTEGER REFERENCES units(id));
            INSERT INTO units VALUES(7, 'Unidade antiga', '2026-01-01');
            INSERT INTO users VALUES(9, 'usuario', 'Consulta');
            INSERT INTO medicines VALUES(11, 'Medicamento antigo');
            INSERT INTO lots VALUES(13, 'LOTE-ANTIGO');
            INSERT INTO movements VALUES(17, 42);
            INSERT INTO unit_references VALUES(7);
        """)
        app.migrate_organization_schema(legacy)
        organization_id = legacy.execute("SELECT id FROM organizations WHERE slug='organizacao-padrao'").fetchone()[0]
        for table, row_id in [('units', 7), ('users', 9), ('medicines', 11), ('lots', 13), ('movements', 17)]:
            self.assertEqual(legacy.execute(f'SELECT organization_id FROM {table} WHERE id=?', (row_id,)).fetchone()[0], organization_id)
        self.assertEqual(legacy.execute('SELECT name FROM medicines WHERE id=11').fetchone()[0], 'Medicamento antigo')
        other_organization = legacy.execute("INSERT INTO organizations(name,slug,created_at) VALUES('Outra organização','outra-org','2026-01-01')").lastrowid
        legacy.execute("INSERT INTO units(name,created_at,organization_id) VALUES('Unidade antiga','2026-01-02',?)", (other_organization,))
        self.assertEqual(legacy.execute("SELECT COUNT(*) FROM units WHERE name='Unidade antiga'").fetchone()[0], 2)
        self.assertEqual(legacy.execute('PRAGMA foreign_key_check').fetchall(), [])
        legacy.close()

    def test_initialize_backups_populated_legacy_database_before_migration(self):
        legacy_path = self.db.execute('PRAGMA database_list').fetchone()[2]
        legacy_path = Path(legacy_path).with_name('legacy.db')
        with patch.object(app, 'DATABASE', legacy_path):
            legacy = app.connect_db()
            legacy.execute('CREATE TABLE units(id INTEGER PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL)')
            legacy.execute("INSERT INTO units VALUES(5,'Unidade preservada','2025-01-01')")
            legacy.commit()
            legacy.close()
            with contextlib.redirect_stderr(io.StringIO()):
                app.initialize_db()
        backup_path = max(app.BACKUPS.glob('farmacia-*.sqlite3'), key=lambda item: item.stat().st_mtime)
        backup = sqlite3.connect(backup_path)
        self.assertEqual(backup.execute("SELECT name FROM units WHERE id=5").fetchone()[0], 'Unidade preservada')
        self.assertFalse(backup.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='organizations'").fetchone())
        backup.close()
        with patch.object(app, 'DATABASE', legacy_path):
            migrated = app.connect_db()
            row = migrated.execute('SELECT name,organization_id FROM units WHERE id=5').fetchone()
            self.assertEqual(row['name'], 'Unidade preservada')
            self.assertIsNotNone(row['organization_id'])
            migrated.close()

    def test_registration_creates_organization_unit_admin_and_session(self):
        response = {}
        self.handler.send_json = lambda status, body, headers=None: response.update(status=status, body=body, headers=headers or {})
        self.handler.headers = {}
        self.handler.register_organization({
            'organization_name': 'Rede Exemplo',
            'unit_name': 'Unidade Centro',
            'full_name': 'Ana Administradora',
            'username': 'ana@example',
            'email': 'ana@example.com',
            'password': 'Strong-Password-123',
        })
        self.assertEqual(response['status'], 200)
        cookie = response['headers']['Set-Cookie'].split(';')[0]
        self.handler.headers = {'Cookie': cookie}
        session = self.handler.get_user()
        self.assertEqual(session['role'], 'Administrador da Organização')
        self.assertEqual(session['units'][0]['name'], 'Unidade Centro')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organizations WHERE name=?', ('Rede Exemplo',)).fetchone()[0], 1)

    def test_cross_organization_ids_and_catalog_reads_are_denied(self):
        admin = dict(self.db.execute('SELECT * FROM users WHERE username="admin"').fetchone())
        admin.update(role='Administrador da Organização', units=[{'id': 1, 'name': 'USF Santa Maria do Bacuri'}])
        organization_two = self.handler.create_organization(self.db, {
            'organization_name': 'Outra Rede', 'unit_name': 'Unidade Norte',
            'full_name': 'Outro Administrador', 'username': 'outro-admin',
            'password': 'Other-Strong-Password-123',
        })
        unit_two = organization_two['unit_id']
        organization_id_two = organization_two['organization_id']
        admin_two = {'id': organization_two['user_id'], 'username': 'outro-admin', 'full_name': 'Outro Administrador',
                     'role': 'Administrador da Organização', 'organization_id': organization_id_two, 'units': [{'id': unit_two, 'name': 'Unidade Norte'}]}
        medicine_one = self.handler.create_medicine(self.db, admin, {'name': 'Medicamento da rede um', 'stock_unit': 'Unidade'})['id']
        medicine_two = self.handler.create_medicine(self.db, admin_two, {'name': 'Medicamento da rede dois', 'stock_unit': 'Unidade'})['id']
        self.handler.create_entry(self.db, admin, 1, {'medicine_id': medicine_one, 'quantity': 5,
            'lot_number': 'LOTE-UM', 'expiration_date': '2099-01-01'})
        entry_two = self.handler.create_entry(self.db, admin_two, unit_two, {'medicine_id': medicine_two, 'quantity': 7,
            'lot_number': 'LOTE-DOIS', 'expiration_date': '2099-01-01'})
        visible = self.handler.api_get('/api/medicines', self.db, admin, 1)['items']
        self.assertEqual({item['id'] for item in visible}, {medicine_one})
        visible_users = self.handler.api_get('/api/users', self.db, admin, 1)['items']
        self.assertNotIn('other-admin', {item['username'] for item in visible_users})
        self.assertTrue(all(item['role'] == 'Administrador da Organização' for item in visible_users))
        with self.assertRaises(app.ApiError):
            self.handler.create_user(self.db, admin, {'username': 'cross-unit-user', 'full_name': 'Usuário externo',
                'password': 'Cross-Unit-Strong-Password-123', 'role': 'Consulta', 'unit_ids': [unit_two]})
        with self.assertRaises(app.ApiError):
            self.handler.create_entry(self.db, admin, 1, {'medicine_id': medicine_two, 'quantity': 1,
                'lot_number': 'INVASAO', 'expiration_date': '2099-01-01'})
        with self.assertRaises(app.ApiError):
            self.handler.valid_unit_ids(self.db, [unit_two], required=True, organization_id=admin['organization_id'])
        with self.assertRaises(app.ApiError):
            self.handler.update_medicine(self.db, admin, medicine_two, {'name': 'Alterado'})
        with self.assertRaises(app.ApiError):
            self.handler.scan_lot(self.db, 1, entry_two['internal_code'], admin['organization_id'])
        with self.assertRaises(app.ApiError):
            self.handler.create_transfer(self.db, admin, 1, {'origin_unit_id': 1, 'destination_unit_id': unit_two,
                'medicine_id': medicine_one, 'lot_id': self.db.execute("SELECT id FROM lots WHERE lot_number='LOTE-UM'").fetchone()[0],
                'quantity': 1, 'responsible': 'Tentativa cruzada'})
        self.handler.headers = {'X-Unit-ID': str(unit_two)}
        with self.assertRaises(app.ApiError):
            self.handler.get_unit_id(self.db, admin)

    def test_superadmin_tenant_context_is_selected_into_session(self):
        with patch.dict(os.environ, {
            'FARMACIA_SUPERADMIN_USERNAME': 'platform-root',
            'FARMACIA_SUPERADMIN_PASSWORD': 'Platform-Strong-Password-123',
        }), contextlib.redirect_stderr(io.StringIO()):
            app.initialize_db()
        organization = self.handler.create_organization(self.db, {
            'organization_name': 'Tenant Supervisionado', 'unit_name': 'UBS Supervisão',
            'full_name': 'Admin do Tenant', 'username': 'tenant-admin',
            'password': 'Tenant-Strong-Password-123',
        })
        response = {}
        self.handler.send_json = lambda status, body, headers=None: response.update(status=status, body=body, headers=headers or {})
        self.handler.headers = {}
        self.handler.login({'username': 'platform-root', 'password': 'Platform-Strong-Password-123'})
        self.handler.headers = {'Cookie': response['headers']['Set-Cookie'].split(';')[0]}
        user = self.handler.get_user()
        self.assertEqual((user['role'], user['organization_id'], user['units']), ('SuperAdmin', None, []))
        token = self.handler.cookie_value()
        self.db.execute('UPDATE sessions SET organization_id=? WHERE token_hash=?',
                        (organization['organization_id'], app.hashlib.sha256(token.encode()).hexdigest()))
        self.db.commit()
        active = self.handler.get_user()
        self.assertEqual(active['organization_id'], organization['organization_id'])
        self.assertEqual(active['units'][0]['name'], 'UBS Supervisão')

    def test_http_registration_and_tenant_header_tampering(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.PharmacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*server.server_address)
        origin = f'http://{server.server_address[0]}:{server.server_address[1]}'
        try:
            body = {'organization_name': 'Cadastro HTTP', 'unit_name': 'UBS HTTP',
                    'full_name': 'Admin HTTP', 'username': 'http-admin',
                    'password': 'Http-Strong-Password-123'}
            connection.request('POST', '/api/register', json.dumps(body), {'Content-Type': 'application/json'})
            self.assertEqual(connection.getresponse().status, 403)
            connection.request('POST', '/api/register', json.dumps(body),
                               {'Content-Type': 'application/json', 'Origin': origin})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader('Set-Cookie').split(';')[0]
            response.read()
            headers = {'Cookie': cookie, 'Origin': origin}
            connection.request('GET', '/api/session', headers=headers)
            session_response = connection.getresponse()
            session = json.loads(session_response.read())
            self.assertEqual(session['user']['role'], 'Administrador da Organização')
            tenant_unit_id = session['activeUnitId']
            other = self.handler.create_organization(self.db, {
                'organization_name': 'Outro cadastro', 'unit_name': 'UBS Externa',
                'full_name': 'Outro Admin', 'username': 'other-http-admin',
                'password': 'Other-Http-Strong-Password-123',
            })
            headers['X-Unit-ID'] = str(other['unit_id'])
            connection.request('GET', '/api/medicines', headers=headers)
            self.assertEqual(connection.getresponse().status, 404)
            headers.update({'X-Unit-ID': str(tenant_unit_id), 'X-CSRF-Token': session['csrfToken'],
                            'Content-Type': 'application/json'})
            connection.request('POST', '/api/medicines', json.dumps({
                'name': 'Somente no tenant autenticado', 'stock_unit': 'Unidade',
                'organization_id': other['organization_id'],
            }), headers)
            created = json.loads(connection.getresponse().read())
            row = self.db.execute('SELECT organization_id FROM medicines WHERE id=?', (created['id'],)).fetchone()
            self.assertEqual(row['organization_id'], session['organizationId'])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_superadmin_http_selects_organization_in_session(self):
        with patch.dict(os.environ, {
            'FARMACIA_SUPERADMIN_USERNAME': 'root-http',
            'FARMACIA_SUPERADMIN_PASSWORD': 'Root-Http-Strong-Password-123',
        }), contextlib.redirect_stderr(io.StringIO()):
            app.initialize_db()
        organization = self.handler.create_organization(self.db, {
            'organization_name': 'Seleção HTTP', 'unit_name': 'UBS Seleção',
            'full_name': 'Admin Seleção', 'username': 'selection-admin',
            'password': 'Selection-Strong-Password-123',
        })
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.PharmacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*server.server_address)
        origin = f'http://{server.server_address[0]}:{server.server_address[1]}'
        try:
            connection.request('POST', '/api/login', json.dumps({
                'username': 'root-http', 'password': 'Root-Http-Strong-Password-123'}),
                {'Content-Type': 'application/json', 'Origin': origin})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader('Set-Cookie').split(';')[0]
            response.read()
            headers = {'Cookie': cookie, 'Origin': origin}
            connection.request('GET', '/api/session', headers=headers)
            session_response = connection.getresponse()
            session = json.loads(session_response.read())
            self.assertEqual(session['organizationId'], None)
            self.assertEqual(session['units'], [])
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': session['csrfToken']})
            connection.request('POST', '/api/organizations/select', json.dumps({
                'organization_id': organization['organization_id']}), headers)
            self.assertEqual(connection.getresponse().status, 200)
            connection.request('GET', '/api/session', headers=headers)
            selected = json.loads(connection.getresponse().read())
            self.assertEqual(selected['organizationId'], organization['organization_id'])
            self.assertEqual([unit['id'] for unit in selected['units']], [organization['unit_id']])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
