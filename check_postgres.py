"""Integration acceptance on an EMPTY disposable PostgreSQL database ending _test.
Never points to DATABASE_URL unless PG_TEST_DATABASE_URL explicitly provides it.
"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'vendor_python'))
import psycopg


def run():
    url=os.environ['PG_TEST_DATABASE_URL']
    if not urlsplit(url).path.endswith('_test'):
        raise RuntimeError('Test database name must end _test.')
    with psycopg.connect(url) as raw:
        if raw.execute("SELECT to_regclass('public.users')").fetchone()[0]:
            raise RuntimeError('Use an empty disposable test database.')
    password=secrets.token_urlsafe(24)
    with patch.dict(os.environ,{'DATABASE_URL':url,'FARMACIA_ADMIN_PASSWORD':password,
        'FARMACIA_ADMIN_USERNAME':'admin','FARMACIA_SUPERADMIN_USERNAME':'','FARMACIA_SUPERADMIN_PASSWORD':''}):
        import wsgi
        app=wsgi.app
        app.DATABASE=None
        app.initialize_db()
        app.initialize_db()
        from scripts.import_catalog import run_postgres
        source=ROOT/'data/catalogo_caf_ipixuna_COMPLETO.json'
        assert run_postgres(source,dry_run=True)['inserted']==250
        assert run_postgres(source)['inserted']==250
        assert run_postgres(source)['existing']==250
        h=object.__new__(app.PharmacyHandler)
        db=app.connect_db()
        try:
            user=dict(db.execute('SELECT * FROM users LIMIT 1').fetchone())
            user['csrf_token']='test-only'
            user['units']=[dict(r) for r in db.execute('SELECT id,name FROM units')]
            svc=h.catalog_service(db,user,1)
            items=svc.catalog({'q':['amoxi']})['items'];assert items
            mid=items[0]['id']
            entry=h.create_entry(db,user,1,{'medicine_id':mid,'quantity':280,'lot_number':'PG-FIRST','expiration_date':'2090-01-01'})
            code=entry['internal_code'];assert h.scan_lot(db,1,code)['lot']['quantity']==280
            svc.post('/api/monthly-needs',{'items':[{'medicine_id':mid,'quantity':1000}]})
            order=svc.post('/api/orders',{'year':2026,'month':9,'responsible':'Test','item_type':'MEDICAMENTO'})
            line=next(l for l in order['lines'] if l['medicine_id']==mid)
            assert (line['stock_snapshot'],line['suggestion'])==(280,720)
            path=f"/api/orders/{order['id']}"
            lines=[{'id':l['id'],'requested':800 if l['id']==line['id'] else 0} for l in order['lines']]
            order=svc.post(path,{'action':'finalize','version':order['version'],'lines':lines})
            order=svc.post(path,{'action':'release','version':order['version'],'lines':[{'id':line['id'],'released':800}]})
            for quantity in (200,600):
                order=svc.post(path,{'action':'receive','version':order['version'],'line_id':line['id'],'request_id':secrets.token_hex(16),
                    'quantity':quantity,'lot_number':'PG-RECEIPT','expiration_date':'2091-01-01'})
            assert order['status']=='Recebido'
            assert db.execute('SELECT SUM(quantity) FROM lots WHERE medicine_id=?',(mid,)).fetchone()[0]==1080
            assert svc.pdf(order['id']).startswith(b'%PDF')
            unit=h.create_unit(db,user,{'name':'Destination'})['id']
            user['units'].append({'id':unit,'name':'Destination'})
            transfer=h.create_transfer(db,user,1,{'origin_unit_id':1,'destination_unit_id':unit,'medicine_id':mid,'lot_id':entry['lot_id'],'quantity':10,'responsible':'Test'})
            assert db.execute('SELECT COALESCE(SUM(quantity),0) FROM lots WHERE unit_id=?',(unit,)).fetchone()[0]==0
            notices=app.notifications.list_notifications(db,user)
            assert notices['items']
            h.receive_transfer(db,user,unit,transfer['id'])
            assert db.execute('SELECT SUM(quantity) FROM lots WHERE unit_id=?',(unit,)).fetchone()[0]==10
            h.create_inventory_adjustment(db,user,1,{'lot_id':entry['lot_id'],'counted_quantity':10,'reason':'Test'})
            barrier=threading.Barrier(2)
            def withdraw(_):
                connection=app.connect_db()
                try:
                    barrier.wait()
                    h.create_exit(connection,user,1,{'medicine_id':mid,'lot_id':entry['lot_id'],'quantity':7,'reason':'Test'})
                    return 201
                except app.ApiError as e:return e.status
                finally:connection.close()
            with ThreadPoolExecutor(2) as executor:assert sorted(executor.map(withdraw,range(2)))==[201,409]
            assert db.execute('SELECT quantity FROM lots WHERE id=?',(entry['lot_id'],)).fetchone()[0]==3
            for route in ('session','dashboard','users','medicines','lots','movements','inventory','alerts','replenishment','transfers','orders','catalog','notifications','reports/movements','audit','requisitions','losses'):
                h.path='/api/'+route
                h.api_get(h.path,db,user,1)
            from pdf_reports import render_label_sheet_pdf
            assert render_label_sheet_pdf([{'code':code,'medicine_name':'Test','concentration':'500 mg'}]*100).startswith(b'%PDF')
            print('POSTGRES_PASS: schema/restart, catalog twice/dry-run, search, entry, orders, partial receipts, PDF/QR, transfers, inventory, concurrency, all read routes')
        finally:db.close()


if __name__=='__main__':run()
