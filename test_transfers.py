import contextlib
import concurrent.futures
import json
import os
import unittest
from unittest.mock import patch

from test_password_security import app
import test_stock_improvements


class TransferTests(unittest.TestCase):
    def setUp(self):
        test_stock_improvements.ImprovementsTests.setUp(self)
        self.db.execute("UPDATE units SET name='USF Santa Maria do Bacuri' WHERE id=1")
        organization_id = self.db.execute('SELECT organization_id FROM units WHERE id=1').fetchone()[0]
        self.db.execute("INSERT INTO units(id,name,created_at,organization_id) VALUES(2,'PSF Olívia Soares',?,?)", (app.now_iso(), organization_id))
        self.db.execute("INSERT INTO units(id,name,created_at,organization_id) VALUES(3,'Terceira unidade',?,?)", (app.now_iso(), organization_id))
        self.db.execute("UPDATE lots SET quantity=CASE WHEN lot_number='primeiro' THEN 50 ELSE 0 END")
        self.db.commit()
        self.handler.create_entry(self.db, self.user, 2, {'medicine_id': self.medicine, 'quantity': 20, 'lot_number': 'primeiro', 'expiration_date': '2090-01-01'})
        self.origin_lot = self.db.execute("SELECT id FROM lots WHERE unit_id=1 AND lot_number='primeiro'").fetchone()[0]
        self.destination_lot = self.db.execute("SELECT id FROM lots WHERE unit_id=2 AND lot_number='primeiro'").fetchone()[0]
        uid = self.handler.create_user(self.db, self.user, {'username': 'atendente', 'full_name': 'Atendente do destino', 'email': 'destino@example.com', 'role': 'Funcionário da Farmácia', 'password': 'Test-Password-123', 'unit_ids': [2]})['id']
        self.receiver = dict(self.db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
        self.receiver['units'] = [{'id': 2}]

    def send(self, origin=1, destination=2, lot=None, **overrides):
        payload = {'origin_unit_id': origin, 'destination_unit_id': destination, 'medicine_id': self.medicine, 'lot_id': lot or self.origin_lot, 'quantity': 10, 'responsible': 'Responsável pelo envio', 'notes': 'Conferir embalagem', 'expiration_date': '2090-01-01', **overrides}
        with patch.object(self.handler, 'send_transfer_email', return_value='not_configured'):
            return self.handler.create_transfer(self.db, self.user, origin, payload)

    def action(self, tid, action, unit=2, actor=None, **payload):
        return self.handler.transfer_action(self.db, actor or self.user, unit, f'/api/transfers/{tid}', {'action': action, **payload})

    def balances(self):
        return tuple(self.db.execute('SELECT COALESCE(SUM(quantity),0) FROM lots WHERE unit_id=?', (uid,)).fetchone()[0] for uid in [1, 2])

    def test_send_pending_and_confirm_only_once(self):
        tid = self.send()['id']
        self.assertEqual(self.balances(), (40, 20))
        self.assertEqual(self.db.execute('SELECT status FROM transfers WHERE id=?', (tid,)).fetchone()[0], app.TRANSFER_PENDING)
        self.action(tid, 'receber', actor=self.receiver)
        self.assertEqual(self.balances(), (40, 30))
        row = self.db.execute('SELECT * FROM transfers WHERE id=?', (tid,)).fetchone()
        self.assertEqual((row['received_by'], row['status'], row['lot_number'], row['expiration_date']), (self.receiver['id'], app.TRANSFER_RECEIVED, 'primeiro', '2090-01-01'))
        self.assertTrue(row['received_at'])
        with self.assertRaises(app.ApiError):
            self.action(tid, 'receber', actor=self.receiver)
        self.db.rollback()
        self.assertEqual(self.balances(), (40, 30))
        movements = self.db.execute('SELECT kind,quantity FROM movements WHERE document=? ORDER BY id', (f'TRF-{tid:06d}',)).fetchall()
        self.assertEqual([tuple(x) for x in movements], [('Saída', 10), ('Entrada', 10)])

    def test_pending_notifications_only_for_destination(self):
        self.send()
        for unit, count in [(1, 0), (2, 1), (3, 0)]:
            result = self.handler.api_get('/api/notifications/transfers', self.db, self.user, unit)
            self.assertEqual(result['count'], count)
            if count:
                self.assertEqual(result['items'][0]['sender_name'], self.user['full_name'])
                self.assertEqual(result['items'][0]['destination_name'], 'PSF Olívia Soares')

    def test_reverse_direction(self):
        tid = self.send(origin=2, destination=1, lot=self.destination_lot)['id']
        self.assertEqual(self.balances(), (50, 10))
        self.action(tid, 'receber', unit=1)
        self.assertEqual(self.balances(), (60, 10))

    def test_discrepancy_requires_authorized_destination_confirmation(self):
        tid = self.send()['id']
        self.action(tid, 'divergencia', actor=self.receiver, quantity_received=8, reason='Faltaram duas unidades', notes='Embalagem aberta')
        self.assertEqual(self.balances(), (40, 20))
        self.assertEqual(self.handler.load_transfer(self.db, tid, self.user['organization_id'])['status'], app.TRANSFER_DIVERGENCE)
        with self.assertRaises(app.ApiError):
            self.action(tid, 'resolver', actor=self.receiver, decision='receber', reason='Conferido')
        with self.assertRaises(app.ApiError):
            self.action(tid, 'resolver', unit=1, decision='receber', reason='Conferido')
        self.db.rollback()
        self.action(tid, 'resolver', decision='receber', reason='Conferida a entrada de oito unidades; falta registrada')
        self.assertEqual(self.balances(), (40, 28))
        row = self.handler.load_transfer(self.db, tid, self.user['organization_id'])
        self.assertEqual(row['received_by'], self.user['id'])
        self.assertEqual(row['discrepancy_reported_by'], self.receiver['id'])
        self.assertEqual(row['discrepancy_notes'], 'Embalagem aberta')
        self.assertEqual(row['quantity_received'], 8)

    def test_refusal_requires_reason_and_refund_is_authorized_and_once(self):
        tid = self.send()['id']
        with self.assertRaises(app.ApiError):
            self.action(tid, 'recusar', reason='')
        self.action(tid, 'recusar', actor=self.receiver, reason='Embalagem danificada')
        self.assertEqual(self.balances(), (40, 20))
        with self.assertRaises(app.ApiError):
            self.action(tid, 'estornar', unit=1, actor=self.receiver, reason='Devolvido')
        self.action(tid, 'estornar', unit=1, reason='Devolução física conferida na origem')
        self.assertEqual(self.balances(), (50, 20))
        self.assertEqual(self.handler.load_transfer(self.db, tid, self.user['organization_id'])['status'], app.TRANSFER_REVERSED)
        with self.assertRaises(app.ApiError):
            self.action(tid, 'estornar', unit=1, reason='Repetido')
        self.db.rollback()
        self.assertEqual(self.balances(), (50, 20))

    def test_cancel_preserves_history_and_cannot_receive(self):
        tid = self.send()['id']
        self.action(tid, 'cancelar', unit=1, reason='Envio por engano; estoque conferido na origem')
        self.assertEqual(self.balances(), (50, 20))
        self.assertEqual(self.handler.load_transfer(self.db, tid, self.user['organization_id'])['status'], app.TRANSFER_CANCELLED)
        with self.assertRaises(app.ApiError):
            self.action(tid, 'receber')
        self.db.rollback()
        history = self.handler.list_transfers(self.db, 1)[0]['events']
        self.assertEqual(len(history), 2)
        self.assertIn('cancelada', history[-1]['event_type'])

    def test_invalid_send_leaves_balances_unchanged(self):
        for overrides in [{'quantity': 51}, {'quantity': 0}, {'destination': 1}, {'lot': self.destination_lot}, {'expiration_date': '2091-01-01'}]:
            with self.subTest(overrides=overrides), self.assertRaises(app.ApiError):
                self.send(**overrides)
            self.db.rollback()
            self.assertEqual(self.balances(), (50, 20))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM transfers').fetchone()[0], 0)

    def test_wrong_unit_consultation_and_unlinked_user_cannot_receive(self):
        tid = self.send()['id']
        for unit, actor in [(1, self.user), (3, self.user), (2, {**self.receiver, 'role': 'Consulta'}), (2, {**self.receiver, 'units': [{'id': 3}]})]:
            with self.assertRaises(app.ApiError):
                self.action(tid, 'receber', unit=unit, actor=actor)
            self.db.rollback()
        self.assertEqual(self.balances(), (40, 20))

    def test_expired_in_transit_and_conflicting_lot_rejected(self):
        tid = self.send()['id']
        self.db.execute("UPDATE lots SET expiration_date='2091-01-01' WHERE id=?", (self.destination_lot,))
        self.db.commit()
        with self.assertRaises(app.ApiError):
            self.action(tid, 'receber')
        self.db.rollback()
        self.db.execute("UPDATE transfers SET expiration_date='2000-01-01' WHERE id=?", (tid,))
        self.db.commit()
        with self.assertRaises(app.ApiError):
            self.action(tid, 'receber')
        self.db.rollback()
        self.assertEqual(self.balances(), (40, 20))

    def test_new_destination_lot_preserves_expiry(self):
        tid = self.send(destination=3)['id']
        self.action(tid, 'receber', unit=3)
        row = self.db.execute('SELECT * FROM lots WHERE unit_id=3').fetchone()
        self.assertEqual((row['lot_number'], row['expiration_date'], row['quantity']), ('primeiro', '2090-01-01', 10))

    def test_concurrent_confirmation_does_not_duplicate_credit(self):
        tid = self.send()['id']
        def receive():
            with contextlib.closing(app.connect_db()) as db:
                try:
                    self.handler.transfer_action(db, self.user, 2, f'/api/transfers/{tid}', {'action': 'receber'})
                    return 200
                except app.ApiError as error:
                    db.rollback()
                    return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: receive(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(self.balances(), (40, 30))

    def test_email_has_destination_and_actual_sender(self):
        tid = self.send()['id']
        with patch.dict(os.environ, {'FARMACIA_SMTP_HOST': 'smtp.test', 'FARMACIA_SMTP_FROM': 'sistema@example.com'}), patch.object(app.smtplib, 'SMTP') as smtp:
            result = self.handler.send_transfer_email(self.db, tid, self.user['organization_id'])
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(result, 'sent')
            self.assertIn('destino@example.com', message['To'])
            self.assertIn('PSF Olívia Soares', message.get_content())
            self.assertIn(self.user['full_name'], message.get_content())
        self.assertEqual(self.balances(), (40, 20))


if __name__ == '__main__':
    unittest.main()
