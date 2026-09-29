"""Existing keyboard/stock flows through real Waitress, plus private-data-free PWA."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from browser_accessibility_check import sync_playwright, run, checks, expect
import wsgi
from waitress import create_server

app=wsgi.app


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{
        'FARMACIA_ADMIN_PASSWORD':'Browser-Test-123','PUBLIC_ORIGIN':'','APP_ENV':'development','SECURE_COOKIE':'0'}):
        app.DATA=Path(directory);app.DATABASE=app.DATA/'test.db';app.BACKUPS=app.DATA/'backups'
        with contextlib.redirect_stderr(io.StringIO()):app.initialize_db()
        with contextlib.closing(app.connect_db()) as db:
            actor=dict(db.execute('SELECT * FROM users LIMIT 1').fetchone())
            db.execute("INSERT INTO units(id,name,created_at,organization_id) VALUES(2,'Destino',?,?)",(app.now_iso(),actor['organization_id']));db.commit()
            h=object.__new__(app.PharmacyHandler)
            mid=h.create_medicine(db,actor,{'name':'Paracetamol','concentration':'500 mg','stock_unit':'Comprimido'})['id']
            h.create_entry(db,actor,1,{'medicine_id':mid,'lot_number':'A11Y-TEST','expiration_date':'2090-01-01','quantity':80})
        server=create_server(wsgi.application,host='127.0.0.1',port=0,threads=4,max_request_body_size=1_000_000)
        threading.Thread(target=server.run,daemon=True).start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                context=browser.new_context(viewport={'width':1280,'height':800},reduced_motion='reduce')
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                url=f'http://127.0.0.1:{server.effective_port}'
                assert context.request.get(url+'/health').json()=={'status':'ok'}
                page.goto(url);run(page)
                assert not errors,errors
                page.evaluate('async()=>await navigator.serviceWorker.ready')
                page.wait_for_function('()=>!!navigator.serviceWorker.controller')
                assert page.evaluate('async()=>(await caches.keys()).length')==0
                context.set_offline(True);page.goto(url)
                expect(page.locator('h1')).to_have_text('Sem conexão')
                assert 'Paracetamol' not in page.content()
                context.set_offline(False);browser.close()
            assert not any(c['violations'] for c in checks)
            print('WAITRESS_BROWSER_PASS: full keyboard/stock/transfer/report flows, responsive layouts, 34 axe scenarios, PWA offline without sensitive cache')
        finally:
            server.close();server.task_dispatcher.shutdown()
            (Path(__file__).parent/'production-browser-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
