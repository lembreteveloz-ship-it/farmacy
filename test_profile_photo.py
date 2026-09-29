import base64
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from test_password_security import app


def png(width=1, height=1):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    raw = b'\x89PNG\r\n\x1a\n'
    raw += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    raw += chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0\xff' * width) * height))
    raw += chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(raw).decode()


class PhotoTests(unittest.TestCase):
    def test_photo_validation(self):
        self.assertEqual(app.validate_profile_photo(png()), png())
        self.assertEqual(app.validate_profile_photo(png(256, 256)), png(256, 256))
        self.assertEqual(app.validate_profile_photo(''), '')
        for invalid in [None, {}, 'data:image/svg+xml;base64,abcd', 'data:image/png;base64,!!!!',
                        png(257, 1), png()[:-8], png() + 'AAAA']:
            with self.subTest(invalid=str(invalid)[:40]), self.assertRaises(app.ApiError):
                app.validate_profile_photo(invalid)

    def test_authenticated_save_read_remove_and_account_isolation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'FARMACIA_ADMIN_USERNAME': 'admin', 'FARMACIA_ADMIN_PASSWORD': 'Test-Password-123'
        }), patch.object(app, 'DATA', Path(directory)), patch.object(app, 'DATABASE', Path(directory) / 'test.db'):
            with contextlib.redirect_stderr(io.StringIO()):
                app.initialize_db()
            with contextlib.closing(app.connect_db()) as db:
                db.execute("INSERT INTO users(username,full_name,password_hash,role,created_at) VALUES('other','Other',?,'Consulta',?)",
                           (app.password_hash('Other-Password-123'), app.now_iso()))
                db.commit()
            server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.PharmacyHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            cookie = ''

            def request(path, body=None, csrf=''):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    headers = {'Cookie': cookie, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
                    connection.request('GET' if body is None else 'POST', '/api/' + path,
                                       None if body is None else json.dumps(body), headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read()), response.getheader('Set-Cookie')
                finally:
                    connection.close()
            try:
                self.assertEqual(request('profile/photo', {'photo': png()})[0], 401)
                status, _, token = request('login', {'username': 'admin', 'password': 'Test-Password-123'})
                self.assertEqual(status, 200)
                cookie = token.split(';')[0]
                csrf = request('session')[1]['csrfToken']
                self.assertEqual(request('profile/photo', {'photo': png()})[0], 403)
                self.assertEqual(request('profile/photo', {'photo': 'invalid'}, csrf)[0], 400)
                self.assertEqual(request('profile/photo', {'photo': png(), 'user_id': 2}, csrf)[0], 200)
                self.assertEqual(request('session')[1]['user']['profilePhoto'], png())
                app.initialize_db()
                with contextlib.closing(app.connect_db()) as db:
                    self.assertEqual(db.execute('SELECT profile_photo FROM users WHERE id=1').fetchone()[0], png())
                    self.assertEqual(db.execute('SELECT profile_photo FROM users WHERE id=2').fetchone()[0], '')
                self.assertEqual(request('profile/photo', {'photo': ''}, csrf)[0], 200)
                self.assertEqual(request('session')[1]['user']['profilePhoto'], '')
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
