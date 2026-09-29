"""Production transport/security checks with an isolated database."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import wsgi

app = wsgi.app


class ProductionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in [('DATA', root), ('DATABASE', root/'test.db'), ('BACKUPS', root/'backups')]:
            p = patch.object(app, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'FARMACIA_ADMIN_PASSWORD':'Isolated-Production-Test-123',
            'FARMACIA_ADMIN_USERNAME':'admin', 'APP_ENV':'production',
            'PUBLIC_ORIGIN':'https://farmacia.example.org', 'SECURE_COOKIE':'1'})
        p.start(); self.addCleanup(p.stop)
        with contextlib.redirect_stderr(StringIO()):app.initialize_db()

    def request(self, path, method='GET', payload=None, cookie='', csrf='', origin=None, host='farmacia.example.org'):
        raw=json.dumps(payload).encode() if payload is not None else b''
        environ={'REQUEST_METHOD':method,'PATH_INFO':path,'HTTP_HOST':host,
                 'wsgi.input':BytesIO(raw),'CONTENT_LENGTH':str(len(raw)), 'CONTENT_TYPE':'application/json',
                 'HTTP_COOKIE':cookie,'HTTP_X_CSRF_TOKEN':csrf}
        if origin is not None:environ['HTTP_ORIGIN']=origin
        result={}
        def start(status,headers):result.update(status=int(status.split()[0]),headers=dict(headers))
        result['body']=b''.join(wsgi.application(environ,start))
        return result

    def login(self):
        r=self.request('/api/login','POST',{'username':'admin','password':'Isolated-Production-Test-123'},origin='https://farmacia.example.org')
        self.assertEqual(r['status'],200)
        cookie=r['headers']['Set-Cookie']
        session=self.request('/api/session',cookie=cookie)
        return cookie,json.loads(session['body'])['csrfToken']

    def test_https_login_csrf_logout_and_headers(self):
        cookie,csrf=self.login()
        for flag in ('HttpOnly','Secure','SameSite=Strict'):self.assertIn(flag,cookie)
        for origin,status in [('https://evil.example',403),('https://farmacia.example.org',200)]:
            r=self.request('/api/session/extend','POST',{},cookie,csrf,origin)
            self.assertEqual(r['status'],status)
        self.assertEqual(self.request('/api/logout','POST',{},cookie,'bad')['status'],403)
        r=self.request('/api/logout','POST',{},cookie,csrf)
        self.assertEqual(r['status'],200)
        self.assertEqual(self.request('/api/session',cookie=cookie)['status'],401)
        self.assertEqual(r['headers']['X-Frame-Options'],'DENY')
        self.assertIn('max-age',r['headers']['Strict-Transport-Security'])

    def test_health_static_pwa_no_auth_and_invalid_host(self):
        for path in ('/health','/','/app.js','/manifest.json','/sw.js','/icon-192.png','/icon-512.png'):
            r=self.request(path);self.assertEqual(r['status'],200,path)
            self.assertNotIn('Access-Control-Allow-Origin',r['headers'])
        self.assertEqual(json.loads(self.request('/health')['body']),{'status':'ok'})
        self.assertEqual(self.request('/',host='evil.example')['status'],400)
        self.assertEqual(self.request('/not-found')['status'],404)
        self.assertEqual(self.request('/../data/farmacia.sqlite3')['status'],404)

    def test_no_default_password_and_restart_preserves_hash(self):
        with contextlib.closing(app.connect_db()) as db:
            before=db.execute('SELECT password_hash FROM users WHERE id=1').fetchone()[0]
        with patch.dict(os.environ,{'FARMACIA_ADMIN_PASSWORD':'Different-Password-123'}):app.initialize_db()
        with contextlib.closing(app.connect_db()) as db:
            self.assertEqual(before,db.execute('SELECT password_hash FROM users WHERE id=1').fetchone()[0])
        with patch.object(app,'DATABASE',app.DATA/'empty.db'),patch.dict(os.environ,{'FARMACIA_ADMIN_PASSWORD':''}):
            with self.assertRaises(ValueError):app.initialize_db()

    def test_login_injection_origin_and_password_privacy(self):
        self.assertEqual(self.request('/api/login','POST',{'username':"' OR 1=1 --",'password':'wrong'})['status'],401)
        self.assertEqual(self.request('/api/login','POST',{'username':'admin','password':'Isolated-Production-Test-123'},origin='https://evil.example')['status'],403)
        cookie,_=self.login()
        for path in ('/api/session','/api/users'):
            text=self.request(path,cookie=cookie)['body'].decode()
            self.assertNotIn('password',text);self.assertNotIn('Isolated-Production',text)

    def test_concurrent_exit_uses_database_transaction(self):
        handler=object.__new__(app.PharmacyHandler)
        with contextlib.closing(app.connect_db()) as db:
            user=dict(db.execute('SELECT * FROM users LIMIT 1').fetchone())
            mid=handler.create_medicine(db,user,{'name':'Concurrency','stock_unit':'Unidade'})['id']
            lot=handler.create_entry(db,user,1,{'medicine_id':mid,'lot_number':'CONCURRENT','expiration_date':'2090-01-01','quantity':10})['lot_id']
        barrier=threading.Barrier(2)
        def withdraw(_):
            db=app.connect_db()
            try:
                barrier.wait()
                handler.create_exit(db,user,1,{'medicine_id':mid,'lot_id':lot,'quantity':7,'reason':'Test'})
                return 201
            except app.ApiError as error:return error.status
            finally:db.close()
        with ThreadPoolExecutor(2) as executor:statuses=list(executor.map(withdraw,range(2)))
        self.assertEqual(sorted(statuses),[201,409])
        with contextlib.closing(app.connect_db()) as db:
            self.assertEqual(db.execute('SELECT quantity FROM lots WHERE id=?',(lot,)).fetchone()[0],3)


if __name__=='__main__':unittest.main()
