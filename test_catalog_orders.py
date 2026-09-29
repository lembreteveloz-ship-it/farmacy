import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_password_security import app
import catalog_orders as feature
from scripts.import_catalog import run as import_run

SOURCE=Path(__file__).with_name('data')/'catalogo_caf_ipixuna_COMPLETO.json'


class CatalogOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        app.DATA=Path(self.temp.name);app.DATABASE=app.DATA/'test.sqlite3';app.BACKUPS=app.DATA/'backups'
        with patch.dict(os.environ,{'FARMACIA_ADMIN_PASSWORD':'Catalog-Test-123'}),contextlib.redirect_stderr(io.StringIO()):app.initialize_db()
        self.db=app.connect_db();self.addCleanup(self.db.close)
        self.user=dict(self.db.execute('SELECT * FROM users LIMIT 1').fetchone());self.user['units']=[{'id':1}]
        self.h=object.__new__(app.PharmacyHandler);self.s=feature.Service(self.h,self.db,self.user,1,app.DATA)
        self.source=feature.read_source(SOURCE)

    def imported(self):
        self.db.execute('BEGIN IMMEDIATE');r=feature.import_items(self.db,self.source,1);self.db.commit();return r

    def item(self):
        return self.db.execute("SELECT * FROM medicines WHERE name='Amoxicilina' AND concentration='500 MG'").fetchone()

    def stock(self):
        return self.db.execute('SELECT COALESCE(SUM(quantity),0) FROM lots').fetchone()[0]

    def order(self,need=1000,stock=280):
        self.imported();mid=self.item()['id']
        if stock:self.h.create_entry(self.db,self.user,1,{'medicine_id':mid,'quantity':stock,'lot_number':'INITIAL','expiration_date':'2090-01-01'})
        self.s.post('/api/monthly-needs',{'items':[{'medicine_id':mid,'quantity':need}]})
        o=self.s.post('/api/orders',{'month':9,'year':2026,'responsible':'Responsável teste','item_type':'MEDICAMENTO'})
        return o,next(x for x in o['lines'] if x['medicine_id']==mid)

    def action(self,o,action,**payload):
        try:return self.s.post('/api/orders/'+str(o['id']),{'action':action,'version':o['version'],**payload})
        except Exception:self.db.rollback();raise

    def test_import_real_source_idempotency_types_controls(self):
        self.assertEqual(self.imported()['inserted'],250)
        self.assertEqual(self.imported()['existing'],250)
        self.assertEqual(self.db.execute('SELECT count(*) FROM medicines').fetchone()[0],250)
        counts=dict(self.db.execute('SELECT item_type,count(*) FROM medicines GROUP BY item_type'))
        self.assertEqual(counts,{'MEDICAMENTO':173,'MATERIAL':72,'TESTE_RAPIDO':5})
        self.assertEqual(self.db.execute('SELECT count(*) FROM medicines WHERE controlled=1').fetchone()[0],37)
        self.assertEqual(self.db.execute('SELECT count(*) FROM medicines WHERE review_required=1').fetchone()[0],250)
        self.assertEqual(self.db.execute('SELECT count(DISTINCT category) FROM medicines').fetchone()[0],9)
        for raw in self.source['items']:
            row=self.db.execute('SELECT * FROM medicines WHERE catalog_key=?',(raw['catalog_key'],)).fetchone()
            for key in ('original_description','control_class','category','volume','item_type'):self.assertEqual(row[key],raw[key])
        self.assertEqual(len(self.s.catalog({'q':['AMOXI']})['items']),2)
        self.assertTrue(self.s.catalog({'q':['capsula']})['items'])

    def test_clear_existing_match_links_without_stock_changes(self):
        mid=self.h.create_medicine(self.db,self.user,{'name':'AMOXICILINA','concentration':'500mg','dosage_form':'Capsula','presentation':'','stock_unit':'Cápsula'})['id']
        self.h.create_entry(self.db,self.user,1,{'medicine_id':mid,'quantity':30,'lot_number':'KEEP','expiration_date':'2090-01-01'})
        before={t:[tuple(r) for r in self.db.execute('SELECT * FROM '+t)] for t in ('lots','movements','transfers','inventory_counts')}
        result=self.imported();self.assertEqual(result['linked'],1);self.assertEqual(result['inserted'],249)
        for t,rows in before.items():self.assertEqual(rows,[tuple(r) for r in self.db.execute('SELECT * FROM '+t)])
        self.assertEqual(self.db.execute('SELECT name FROM medicines WHERE id=?',(mid,)).fetchone()[0],'AMOXICILINA')

    def test_invalid_row_continues_and_ambiguous_match_is_reported(self):
        self.source['items'].insert(1,{'name':'invalid'})
        result=self.imported();self.assertEqual(result['invalid'],1);self.assertEqual(result['inserted'],250)
        self.assertEqual(result['issues'][0]['index'],2)

    def test_dry_run_no_writes_backup_and_critical_rollback(self):
        before=hashlib.sha256(app.DATABASE.read_bytes()).hexdigest()
        r=import_run(app.DATABASE,SOURCE,dry_run=True);self.assertEqual(r['inserted'],250)
        self.assertEqual(hashlib.sha256(app.DATABASE.read_bytes()).hexdigest(),before)
        self.assertFalse(app.BACKUPS.exists())
        self.db.execute("CREATE TRIGGER fail_catalog BEFORE INSERT ON medicines WHEN NEW.name='Amoxicilina' BEGIN SELECT RAISE(ABORT,'injected failure'); END");self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):import_run(app.DATABASE,SOURCE)
        self.assertEqual(self.db.execute('SELECT count(*) FROM medicines').fetchone()[0],0)
        self.db.execute('DROP TRIGGER fail_catalog');self.db.commit()
        first=import_run(app.DATABASE,SOURCE);second=import_run(app.DATABASE,SOURCE)
        self.assertTrue(Path(first['backup']).is_file());self.assertNotEqual(first['backup'],second['backup']);self.assertEqual(second['existing'],250)

    def test_manual_duplicates_and_presentations_stay_separate(self):
        self.imported();item=dict(self.item())
        with self.assertRaises(app.ApiError):self.h.create_medicine(self.db,self.user,{**item,'name':'AMOXICILINA','concentration':'500mg'})
        created=self.h.create_medicine(self.db,self.user,{**item,'concentration':'250mg','stock_unit':'Cápsula'})
        self.assertNotEqual(created['id'],item['id'])

    def test_review_is_preserved_on_reimport(self):
        self.imported();item=self.item()
        self.h.update_medicine(self.db,self.user,item['id'],{'name':'Amoxicilina revisada','review_required':False})
        self.imported()
        row=self.db.execute('SELECT * FROM medicines WHERE id=?',(item['id'],)).fetchone()
        self.assertEqual(row['name'],'Amoxicilina revisada');self.assertEqual(row['review_required'],0)
        self.assertEqual(row['original_description'],item['original_description'])

    def test_ambiguous_legacy_duplicates_not_merged(self):
        item=self.source['items'][3]
        for number in (1,2):
            self.db.execute('INSERT INTO medicines(name,concentration,dosage_form,presentation,code,created_at,organization_id) VALUES(?,?,?,?,?,?,1)',
                (item['name'],item['concentration'],item['dosage_form'],item['presentation'],f'LEGACY-{number}',app.now_iso()))
        self.db.commit();result=self.imported()
        self.assertEqual(result['invalid'],1);self.assertEqual(result['inserted'],249)
        self.assertEqual(len(result['issues'][0]['ids']),2)

    def test_complete_order_partial_then_total_receipts_and_qr(self):
        o,line=self.order();self.assertEqual((line['monthly_need'],line['stock_snapshot'],line['suggestion']),(1000,280,720))
        self.assertEqual(self.stock(),280)
        o=self.action(o,'save',lines=[{'id':line['id'],'requested':800}]);self.assertEqual(o['status'],'Rascunho')
        o=self.action(o,'finalize',lines=[]);self.assertEqual(o['status'],'Finalizado')
        o=self.action(o,'send');self.assertEqual(o['status'],'Enviado');self.assertEqual(self.stock(),280)
        o=self.action(o,'release',lines=[{'id':line['id'],'released':300}]);self.assertEqual(o['status'],'Parcialmente atendido');self.assertEqual(self.stock(),280)
        payload={'line_id':line['id'],'quantity':200,'lot_number':'CAF-ONE','expiration_date':'2091-01-01','document':'NF-123','request_id':'receipt-one'}
        previous=o;received=self.action(o,'receive',**payload);self.assertEqual(received['status'],'Parcialmente atendido');self.assertEqual(self.stock(),480)
        self.assertTrue(received['label_code'].startswith('UBS-'))
        repeat=self.action(previous,'receive',**payload);self.assertEqual(self.stock(),480);self.assertEqual(received['label_code'],repeat['label_code'])
        o=self.action(received,'release',lines=[{'id':line['id'],'released':800}])
        o=self.action(o,'receive',**{**payload,'quantity':600,'lot_number':'CAF-TWO','request_id':'receipt-two'})
        self.assertEqual(o['status'],'Recebido');self.assertEqual(self.stock(),1080);self.assertEqual(len(o['receipts']),2)
        line=next(x for x in o['lines'] if x['id']==line['id']);self.assertEqual((line['stock_snapshot'],line['requested'],line['received'],line['balance']),(280,800,800,0))
        self.assertEqual(len(o['events']),8)
        self.assertTrue(self.s.pdf(o['id']).startswith(b'%PDF-'))
        with patch('pdf_reports.render_pdf',return_value=b'%PDF-test') as render:
            self.s.pdf(o['id']);self.assertIn('280',str(render.call_args))

    def test_suggestion_zero_needs_persist_and_snapshots_are_immutable(self):
        o,l=self.order(100,280);self.assertEqual(l['suggestion'],0)
        self.s.post('/api/monthly-needs',{'items':[{'medicine_id':l['medicine_id'],'quantity':900}]})
        self.h.update_medicine(self.db,self.user,l['medicine_id'],{'name':'Nome corrigido'})
        old=self.s.detail(o['id'])['lines'];old=next(x for x in old if x['id']==l['id'])
        self.assertEqual(old['monthly_need'],100);self.assertEqual(old['item']['name'],'Amoxicilina')
        new=self.s.create({'month':10,'year':2026,'responsible':'Pessoa','item_type':'MEDICAMENTO'})
        new=next(x for x in new['lines'] if x['medicine_id']==l['medicine_id']);self.assertEqual(new['monthly_need'],900)

    def test_reject_overrelease_overreceipt_stale_and_expired(self):
        o,l=self.order();old=o;o=self.action(o,'finalize',lines=[])
        with self.assertRaises(app.ApiError):self.action(old,'save',lines=[])
        with self.assertRaises(app.ApiError):self.action(o,'release',lines=[{'id':l['id'],'released':721}])
        o=self.action(o,'release',lines=[{'id':l['id'],'released':720}])
        for payload in [{'quantity':721,'expiration_date':'2090-01-01'},{'quantity':100,'expiration_date':'2000-01-01'}]:
            with self.assertRaises(app.ApiError):self.action(o,'receive',line_id=l['id'],lot_number='BAD',request_id='bad',**payload)
        self.assertEqual(self.stock(),280);self.assertEqual(len(self.s.detail(o['id'])['receipts']),0)

    def test_atomic_receipt_failure_cannot_change_stock(self):
        o,l=self.order();o=self.action(o,'finalize',lines=[]);o=self.action(o,'release',lines=[{'id':l['id'],'released':720}])
        with patch.object(self.h,'insert_movement',side_effect=RuntimeError('simulated')):
            with self.assertRaises(RuntimeError):self.action(o,'receive',line_id=l['id'],quantity=5,lot_number='ROLLBACK',expiration_date='2090-01-01',request_id='rollback')
        self.assertEqual(self.stock(),280);self.assertEqual(self.db.execute('SELECT count(*) FROM lots').fetchone()[0],1)

    def test_permissions_tenant_unit_cancel_and_numbers(self):
        o,l=self.order();query=feature.Service(self.h,self.db,{**self.user,'role':'Consulta'},1,app.DATA)
        with self.assertRaises(app.ApiError):query.post('/api/orders',{})
        employee=feature.Service(self.h,self.db,{**self.user,'role':'Funcionário da Farmácia'},1,app.DATA)
        with self.assertRaises(app.ApiError):employee.create({'month':9,'year':2026,'responsible':'Pessoa','item_type':'TODOS'})
        for org,unit in [(2,1),(1,2)]:
            other=feature.Service(self.h,self.db,{**self.user,'organization_id':org},unit,app.DATA)
            with self.assertRaises(app.ApiError):other.detail(o['id'])
        cancelled=self.action(o,'cancel',reason='Pedido de teste cancelado');self.assertEqual(cancelled['status'],'Cancelado');self.assertEqual(self.stock(),280)
        old_number=o['number'];new_number=feature.reserve_number(self.db,app.DATA,2026);self.assertNotEqual(new_number,old_number)
        self.assertEqual(len(self.s.get('/api/orders',{'status':['Cancelado'],'q':['Amoxicilina']})['items']),1)


if __name__=='__main__':unittest.main()
