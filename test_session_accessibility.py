"""Session warnings expose only expiry; extension keeps authentication and CSRF."""
import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from test_password_security import app


class SessionAccessibilityTests(unittest.TestCase):
    def test_expiry_extension_and_password_privacy(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'FARMACIA_ADMIN_PASSWORD':'Session-Test-123'}):
            app.DATA=Path(directory);app.DATABASE=app.DATA/'test.db';app.BACKUPS=app.DATA/'backups'
            with contextlib.redirect_stderr(io.StringIO()):app.initialize_db()
            server=app.ThreadingHTTPServer(('127.0.0.1',0),app.PharmacyHandler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            connection=http.client.HTTPConnection(*server.server_address)
            headers={'Content-Type':'application/json'}
            def request(method,path,body=None):
                connection.request(method,path,json.dumps(body) if body is not None else None,headers)
                response=connection.getresponse()
                return response.status,dict(response.getheaders()),json.loads(response.read())
            try:
                self.assertEqual(request('POST','/api/session/extend',{})[0],401)
                status,cookie,_=request('POST','/api/login',{'username':'admin','password':'Session-Test-123'})
                self.assertEqual(status,200);headers['Cookie']=cookie['Set-Cookie'].split(';')[0]
                status,_,session=request('GET','/api/session')
                self.assertEqual(status,200);self.assertTrue(session['expiresAt'])
                self.assertNotIn('password',json.dumps(session).lower())
                _,_,users=request('GET','/api/users')
                self.assertNotIn('password',json.dumps(users).lower())
                self.assertEqual(request('POST','/api/session/extend',{})[0],403)
                headers['X-CSRF-Token']=session['csrfToken']
                status,response,renewed=request('POST','/api/session/extend',{})
                self.assertEqual(status,200);self.assertGreaterEqual(renewed['expiresAt'],session['expiresAt'])
                self.assertIn('HttpOnly',response['Set-Cookie']);self.assertIn('SameSite=Strict',response['Set-Cookie'])
                with contextlib.closing(app.connect_db()) as db:
                    stored=db.execute('SELECT password_hash FROM users LIMIT 1').fetchone()[0]
                    self.assertNotIn('Session-Test-123',stored)
                    self.assertTrue(app.verify_password('Session-Test-123',stored))
                    db.execute("UPDATE sessions SET expires_at='2000-01-01'");db.commit()
                self.assertEqual(request('POST','/api/session/extend',{})[0],401)
            finally:
                connection.close();server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
