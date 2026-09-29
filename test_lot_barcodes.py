import unittest
from unittest.mock import patch
import test_stock_improvements
from test_password_security import app


class LotBarcodeTests(unittest.TestCase):
    def setUp(self):
        test_stock_improvements.ImprovementsTests.setUp(self)
        self.lot = dict(self.db.execute("SELECT * FROM lots WHERE lot_number='primeiro'").fetchone())
        self.code = self.lot['internal_code']

    def exit(self, **values):
        return self.handler.quick_exit(self.db, self.user, 1, {'code': self.code, 'quantity': 2, 'reason': 'Dispensação', 'request_id': 'operation-1', **values})

    def test_entry_returns_stable_unique_code_for_same_lot(self):
        result = self.handler.create_entry(self.db, self.user, 1, {'medicine_id': self.medicine, 'quantity': 5, 'lot_number': 'primeiro', 'expiration_date': '2090-01-01'})
        self.assertEqual(result['internal_code'], self.code)
        codes = [r[0] for r in self.db.execute('SELECT internal_code FROM lots')]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertRegex(self.code, r'^UBS-\d{6,}-\d{6,}$')

    def test_exact_lot_and_retry_do_not_debit_twice(self):
        result = self.exit()
        self.assertEqual((result['stock_before'], result['stock_after']), (3, 1))
        self.assertEqual(self.exit(), result)
        self.assertEqual(self.db.execute("SELECT quantity FROM lots WHERE lot_number='segundo'").fetchone()[0], 8)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM movements WHERE kind='Saída'").fetchone()[0], 1)
        with self.assertRaises(app.ApiError):
            self.exit(quantity=1)
        self.db.rollback()

    def test_unknown_expired_zero_wrong_unit_and_excess_blocked(self):
        with self.assertRaises(app.ApiError):
            self.exit(code='UBS-999999-999999')
        self.db.rollback()
        with self.assertRaises(app.ApiError):
            self.handler.quick_exit(self.db, self.user, 2, {'code': self.code, 'quantity': 1, 'reason': 'Teste', 'request_id': 'other'})
        self.db.rollback()
        with self.assertRaises(app.ApiError):
            self.exit(quantity=4)
        self.db.rollback()
        for sql in ["UPDATE lots SET expiration_date='2000-01-01' WHERE lot_number='primeiro'", "UPDATE lots SET expiration_date='2090-01-01',quantity=0 WHERE lot_number='primeiro'"]:
            self.db.execute(sql)
            self.db.commit()
            with self.assertRaises(app.ApiError):
                self.exit()
            self.db.rollback()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM movements WHERE kind='Saída'").fetchone()[0], 0)

    def test_fefo_warns_but_never_changes_scanned_lot(self):
        second = self.db.execute("SELECT internal_code FROM lots WHERE lot_number='segundo'").fetchone()[0]
        scan = self.handler.scan_lot(self.db, 1, second)
        self.assertEqual(scan['recommended']['internal_code'], self.code)
        result = self.exit(code=second)
        self.assertEqual(result['stock_after'], 6)
        self.assertEqual(self.db.execute('SELECT quantity FROM lots WHERE internal_code=?', (self.code,)).fetchone()[0], 3)

    def test_code_not_reused_after_restore(self):
        name = app.backup_database()
        def entry(lot):
            return self.handler.create_entry(self.db, self.user, 1, {'medicine_id': self.medicine, 'quantity': 2, 'lot_number': lot, 'expiration_date': '2090-01-01'})
        previous = entry('após-backup')['internal_code']
        self.handler.restore_backup(self.user, {'name': name, 'password': 'Test-Password-123'})
        new = entry('outro-lote')['internal_code']
        self.assertNotEqual(previous, new)
        with self.assertRaises(app.ApiError):
            self.handler.scan_lot(self.db, 1, previous)

    def test_transaction_failure_does_not_debit_or_consume_request(self):
        with patch.object(self.handler, 'insert_movement', side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):
                self.exit()
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT quantity FROM lots WHERE internal_code=?', (self.code,)).fetchone()[0], 3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quick_exit_requests').fetchone()[0], 0)
        self.assertEqual(self.exit()['stock_after'], 1)

    def test_consultation_and_inactive_medicine_blocked(self):
        with self.assertRaises(app.ApiError):
            self.handler.quick_exit(self.db, {**self.user, 'role': 'Consulta'}, 1, {})
        self.db.execute('UPDATE medicines SET active=0 WHERE id=?', (self.medicine,))
        self.db.commit()
        with self.assertRaises(app.ApiError):
            self.exit()
        self.db.rollback()


if __name__ == '__main__':
    unittest.main()
