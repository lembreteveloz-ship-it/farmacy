import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from datetime import date,timedelta
from unittest.mock import patch
from test_password_security import app
import notifications as feature


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        app.DATA=Path(self.temp.name);app.DATABASE=app.DATA/'test.db';app.BACKUPS=app.DATA/'backups'
        with patch.dict(os.environ,{'FARMACIA_ADMIN_PASSWORD':'Notice-Test-123'}),contextlib.redirect_stderr(io.StringIO()):app.initialize_db()
        self.db=app.connect_db();self.addCleanup(self.db.close);self.h=object.__new__(app.PharmacyHandler)
        self.user=dict(self.db.execute('SELECT * FROM users LIMIT 1').fetchone());self.user['units']=[{'id':1},{'id':2}]
        self.db.execute("INSERT INTO units(id,name,created_at,organization_id) VALUES(2,'Destino',?,1)",(app.now_iso(),));self.db.commit()
        self.mid=self.h.create_medicine(self.db,self.user,{'name':'Medicamento teste','concentration':'500 mg','stock_unit':'Comprimido','minimum_stock':10})['id']
        self.entry=self.h.create_entry(self.db,self.user,1,{'medicine_id':self.mid,'quantity':10,'lot_number':'N-1','expiration_date':(date.today()+timedelta(days=15)).isoformat()})
        uid=self.h.create_user(self.db,self.user,{'username':'destino','full_name':'Destino','password':'Notice-User-123','role':'Funcionário da Farmácia','unit_ids':[2]})['id']
        self.receiver=dict(self.db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone());self.receiver['units']=[{'id':2}]

    def items(self,user=None):return feature.list_notifications(self.db,user or self.user)

    def send(self):
        with patch.object(self.h,'send_transfer_email',return_value='not_configured'):
            return self.h.create_transfer(self.db,self.user,1,{'origin_unit_id':1,'destination_unit_id':2,'medicine_id':self.mid,'lot_id':self.entry['lot_id'],'quantity':2,'responsible':'Teste'})['id']

    def test_scope_including_administrator_and_other_organization(self):
        all_items=self.items()['items'];self.assertEqual({x['unit_id'] for x in all_items},{1,2})
        receiver=self.items(self.receiver)['items'];self.assertEqual({x['unit_id'] for x in receiver},{2})
        other={**self.receiver,'organization_id':2};self.assertEqual(self.items(other)['items'],[])
        forbidden=next(x for x in all_items if x['unit_id']==1)
        with self.assertRaises(app.ApiError):feature.mark_read(self.db,self.receiver,{'ids':[forbidden['id']]})

    def test_read_does_not_resolve_or_change_transfer_stock(self):
        tid=self.send();result=self.items(self.receiver);n=next(x for x in result['items'] if x['kind']=='transfer')
        stock=[tuple(x) for x in self.db.execute('SELECT id,quantity FROM lots')]
        feature.mark_read(self.db,self.receiver,{'ids':[n['id']]})
        row=next(x for x in self.items(self.receiver)['items'] if x['id']==n['id']);self.assertTrue(row['read_at']);self.assertIsNone(row['resolved_at'])
        self.assertEqual(self.h.load_transfer(self.db,tid,1)['status'],'Pendente de Recebimento')
        self.assertEqual(stock,[tuple(x) for x in self.db.execute('SELECT id,quantity FROM lots')])
        self.assertIsNone(next(x for x in self.items()['items'] if x['id']==n['id'])['read_at'])

    def test_resolved_and_reappearing_stock_new_notification(self):
        old=next(x for x in self.items()['items'] if x['kind']=='stock' and x['unit_id']==1)
        feature.mark_read(self.db,self.user,{'ids':[old['id']]})
        self.h.create_entry(self.db,self.user,1,{'medicine_id':self.mid,'quantity':2,'lot_number':'N-1','expiration_date':(date.today()+timedelta(days=15)).isoformat()})
        self.assertTrue(next(x for x in self.items()['items'] if x['id']==old['id'])['resolved_at'])
        self.db.execute('UPDATE lots SET quantity=10');self.db.commit()
        new=next(x for x in self.items()['items'] if x['kind']=='stock' and x['unit_id']==1 and not x['resolved_at']);self.assertNotEqual(new['id'],old['id']);self.assertIsNone(new['read_at'])

    def test_expiry_escalation_and_priority(self):
        tid=self.send();self.h.report_transfer_discrepancy(self.db,self.receiver,2,tid,{'quantity_received':1,'reason':'Faltou uma unidade'})
        self.db.execute('UPDATE lots SET expiration_date=?',((date.today()-timedelta(days=1)).isoformat(),));self.db.commit()
        result=self.items()['items'];active=[x for x in result if not x['resolved_at']]
        self.assertEqual(active[0]['status'],'Divergência');self.assertEqual(active[0]['priority'],0)
        self.assertTrue(any(x['status']=='Vencido' for x in active));self.assertEqual([x['priority'] for x in active],sorted(x['priority'] for x in active))

    def test_refusal_both_units_and_pending_resolution(self):
        tid=self.send();before=self.items();pending=[x['id'] for x in before['items'] if x['status']=='Pendente de Recebimento']
        self.h.refuse_transfer(self.db,self.receiver,2,tid,{'reason':'Embalagem danificada'})
        after=self.items()['items'];self.assertEqual({x['unit_id'] for x in after if x['status']=='Recusada'},{1,2})
        self.assertTrue(all(x['resolved_at'] for x in after if x['id'] in pending))

    def test_mark_all_persistent_counter_and_no_duplicate_polling(self):
        data=self.items();self.assertGreater(data['unread'],0);ids=[x['id'] for x in data['items']]
        feature.mark_read(self.db,self.user,{'ids':ids});self.assertEqual(self.items()['unread'],0)
        self.assertEqual(ids,[x['id'] for x in self.items()['items']]);self.assertGreater(self.items()['pending'],0)

    def test_zero_stock_catalog_does_not_flood_unassigned_units(self):
        mid=self.h.create_medicine(self.db,self.user,{'name':'Sem histórico nem mínimo','stock_unit':'Unidade'})['id']
        self.assertFalse(any(x['kind']=='stock' and x['target_id']==mid for x in self.items()['items']))
