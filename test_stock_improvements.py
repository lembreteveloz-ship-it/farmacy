import contextlib
import io
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_password_security import app


class ImprovementsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in [('DATA', root), ('DATABASE', root / 'test.db'), ('BACKUPS', root / 'backups')]:
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {'FARMACIA_ADMIN_PASSWORD': 'Test-Password-123'})
        env.start()
        self.addCleanup(env.stop)
        with contextlib.redirect_stderr(io.StringIO()):
            app.initialize_db()
        self.db = app.connect_db()
        self.addCleanup(self.db.close)
        self.user = dict(self.db.execute('SELECT * FROM users LIMIT 1').fetchone())
        self.handler = object.__new__(app.PharmacyHandler)
        self.medicine = self.handler.create_medicine(self.db, self.user, {'name': 'Teste', 'stock_unit': 'Unidade', 'minimum_stock': 20})['id']
        for lot, quantity, expiry in [('primeiro', 3, '2090-01-01'), ('segundo', 8, '2091-01-01')]:
            self.handler.create_entry(self.db, self.user, 1, {'medicine_id': self.medicine, 'quantity': quantity, 'lot_number': lot, 'expiration_date': expiry})

    def test_exit_distributes_by_expiry_and_records_each_lot(self):
        result = self.handler.create_exit(self.db, self.user, 1, {'medicine_id': self.medicine, 'quantity': 5, 'reason': 'Teste'})
        self.assertEqual([(x['lot_number'], x['quantity']) for x in result['allocations']], [('primeiro', 3), ('segundo', 2)])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM movements WHERE kind='Saída'").fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT SUM(quantity) FROM lots').fetchone()[0], 6)

    def test_insufficient_exit_does_not_change_stock_and_expired_excluded(self):
        self.db.execute("UPDATE lots SET expiration_date='2000-01-01' WHERE lot_number='primeiro'")
        self.db.commit()
        with self.assertRaises(app.ApiError):
            self.handler.create_exit(self.db, self.user, 1, {'medicine_id': self.medicine, 'quantity': 10, 'reason': 'Teste'})
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT SUM(quantity) FROM lots').fetchone()[0], 11)
        self.handler.path = '/api/replenishment'
        row = self.handler.api_get(self.handler.path, self.db, self.user, 1)['items'][0]
        self.assertEqual((row['stock'], row['needed']), (8, 12))

    def test_reports_dates_filter_and_unit_isolation(self):
        today = app.date.today().isoformat()
        self.db.execute("UPDATE movements SET created_at=? WHERE unit_id=1", (today + 'T12:00:00+00:00',))
        self.db.commit()
        self.handler.path = f'/api/reports/movements?period=custom&start={today}&end={today}&medicine_id={self.medicine}'
        self.assertEqual(self.handler.movement_report(self.db, 1)['totals']['entries'], 11)
        organization_id = self.db.execute('SELECT organization_id FROM units WHERE id=1').fetchone()[0]
        self.db.execute("INSERT INTO units(id,name,created_at,organization_id) VALUES(2,'Outra unidade',?,?)", (app.now_iso(), organization_id))
        self.db.commit()
        self.assertEqual(self.handler.movement_report(self.db, 2)['items'], [])
        self.handler.path += '0'
        self.assertEqual(self.handler.movement_report(self.db, 1)['items'], [])
        self.handler.path = '/api/reports/movements?period=custom&start=2026-12-31&end=2026-01-01'
        with self.assertRaises(app.ApiError):
            self.handler.movement_report(self.db, 1)

    def test_audit_tracks_changes_without_password(self):
        self.handler.update_medicine(self.db, self.user, self.medicine, {'name': 'Novo nome'})
        change = json.loads(self.db.execute("SELECT changes FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(change['name'], {'antes': 'Teste', 'depois': 'Novo nome'})
        self.handler.update_user(self.db, self.user, self.user['id'], {'password': 'New-Password-123'})
        changes = self.db.execute("SELECT changes FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertNotIn('New-Password-123', changes)
        self.assertNotIn('password_hash', changes)
        self.assertIn('redefinida', changes)

    def test_backup_restore_preserves_recovery_copy_and_revokes_sessions(self):
        organization_id = self.db.execute('SELECT organization_id FROM users WHERE id=1').fetchone()[0]
        self.db.execute("INSERT INTO sessions(token_hash,csrf_token,user_id,expires_at,created_at,organization_id) VALUES('token','csrf',1,'2099-01-01','2026-01-01',?)", (organization_id,))
        self.db.commit()
        name = app.backup_database()
        self.handler.update_medicine(self.db, self.user, self.medicine, {'name': 'Depois do backup'})
        self.handler.restore_backup(self.user, {'name': name, 'password': 'Test-Password-123'})
        self.assertEqual(self.db.execute('SELECT name FROM medicines WHERE id=?', (self.medicine,)).fetchone()[0], 'Teste')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(len(list(app.BACKUPS.glob('*.sqlite3'))), 2)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_restore_rejects_wrong_password_and_path_traversal(self):
        name = app.backup_database()
        for payload in [{'name': name, 'password': 'wrong'}, {'name': '../test.db', 'password': 'Test-Password-123'}]:
            with self.assertRaises(app.ApiError):
                self.handler.restore_backup(self.user, payload)
        self.assertEqual(self.db.execute('SELECT SUM(quantity) FROM lots').fetchone()[0], 11)

    def test_admin_pages_reject_consultation_role(self):
        user = {**self.user, 'role': 'Consulta'}
        for path in ['/api/backups', '/api/audit']:
            with self.assertRaises(app.ApiError) as error:
                self.handler.api_get(path, self.db, user, 1)
            self.assertEqual(error.exception.status, 403)

    def test_http_backup_report_and_replenishment(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.PharmacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address)
            self.addCleanup(connection.close)
            connection.request('POST', '/api/login', json.dumps({'username': 'admin', 'password': 'Test-Password-123'}), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader('Set-Cookie').split(';')[0]
            response.read()
            headers = {'Cookie': cookie}
            connection.request('GET', '/api/session', headers=headers)
            session = json.loads(connection.getresponse().read())
            headers.update({'X-CSRF-Token': session['csrfToken'], 'Content-Type': 'application/json'})
            connection.request('POST', '/api/backups', '{}', headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request('GET', '/api/backups', headers=headers)
            self.assertEqual(connection.getresponse().status, 403)
            for path in ['/api/audit', '/api/replenishment', '/api/reports/movements?period=monthly']:
                connection.request('GET', path, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200, path)
                self.assertTrue(json.loads(response.read())['items'], path)
            connection.request('POST', '/api/backups/restore', '{}', {'Cookie': cookie, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
