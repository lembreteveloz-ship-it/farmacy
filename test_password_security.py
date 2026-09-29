import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pharmacy', Path(__file__).with_name('import hashlib.py'))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class PasswordSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app.DATA = Path(self.temp.name)
        app.DATABASE = app.DATA / 'test.sqlite3'
        self.env = patch.dict(os.environ, {'FARMACIA_ADMIN_USERNAME': 'admin',
            'FARMACIA_ADMIN_PASSWORD': 'Initial-Password-123', 'FARMACIA_SMTP_HOST': 'smtp.test',
            'FARMACIA_SMTP_FROM': 'sender@example.com'})
        self.env.start()
        self.addCleanup(self.env.stop)
        with contextlib.redirect_stderr(io.StringIO()):
            app.initialize_db()
        with contextlib.closing(app.connect_db()) as db:
            db.execute("UPDATE users SET email='user@example.com'")
            db.commit()
        self.handler = object.__new__(app.PharmacyHandler)
        self.handler.send_json = lambda status, data, headers=None: setattr(self, 'response', (status, data))

    def login(self, password='wrong'):
        self.handler.login({'username': 'ADMIN', 'password': password})
        return self.response

    def request_code(self):
        with patch.object(app.smtplib, 'SMTP') as smtp:
            self.handler.request_password_reset({'username': 'admin'})
            mail = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            return re.search(r'\b[A-F0-9]{16}\b', mail.get_content()).group()

    def confirm(self, code):
        self.handler.confirm_password_reset({'username': 'admin', 'code': code,
            'new_password': 'Replacement-Password-123'})

    def test_thresholds_and_persistent_block(self):
        for attempt in range(1, 6):
            status, data = self.login()
            self.assertEqual(status, 423 if attempt == 5 else 401)
            self.assertEqual(data['reset_required'], attempt >= 3)
        app.initialize_db()
        self.assertEqual(self.login('Initial-Password-123')[0], 423)

    def test_success_clears_consecutive_errors(self):
        self.login()
        self.login()
        self.assertEqual(self.login('Initial-Password-123')[0], 200)
        self.assertFalse(self.login()[1]['reset_required'])

    def test_reset_unlocks_revokes_sessions_and_is_single_use(self):
        self.login('Initial-Password-123')
        for _ in range(5):
            self.login()
        code = self.request_code()
        self.confirm(code)
        with contextlib.closing(app.connect_db()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        with self.assertRaises(app.ApiError):
            self.confirm(code)
        app.initialize_db()
        self.assertEqual(self.login('Replacement-Password-123')[0], 200)

    def test_reset_code_attempt_limit_and_expiration(self):
        code = self.request_code()
        for _ in range(5):
            with self.assertRaises(app.ApiError):
                self.confirm('wrong')
        with self.assertRaises(app.ApiError):
            self.confirm(code)
        with contextlib.closing(app.connect_db()) as db:
            db.execute("UPDATE password_resets SET attempts=0, expires_at='2000-01-01'")
            db.commit()
        with self.assertRaises(app.ApiError):
            self.confirm(code)

    def test_admin_password_reset_unlocks(self):
        for _ in range(5):
            self.login()
        with contextlib.closing(app.connect_db()) as db:
            organization_id = db.execute('SELECT organization_id FROM users WHERE id=1').fetchone()[0]
            self.handler.update_user(db, {'id': 1, 'role': 'Administrador', 'organization_id': organization_id}, '1',
                                     {'password': 'Replacement-Password-123'})
        self.assertEqual(self.login('Replacement-Password-123')[0], 200)

    def test_request_is_throttled_and_does_not_unlock(self):
        for _ in range(5):
            self.login()
        self.request_code()
        with patch.object(app.smtplib, 'SMTP') as smtp:
            self.handler.request_password_reset({'username': 'admin'})
            smtp.assert_not_called()
        self.assertEqual(self.login('Initial-Password-123')[0], 423)


if __name__ == '__main__':
    unittest.main()
