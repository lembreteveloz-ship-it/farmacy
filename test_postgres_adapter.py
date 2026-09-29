"""Dialect unit tests, complementary to check_postgres.py (not a server substitute)."""
from pathlib import Path
import sqlite3
import unittest
from postgres import Row, translate


class DialectTests(unittest.TestCase):
    def test_bound_parameters_and_literals(self):
        self.assertEqual(translate("SELECT '?' AS literal FROM users WHERE organization_id IS ? AND username LIKE ?"),
            "SELECT '?' AS literal FROM users WHERE organization_id IS NOT DISTINCT FROM %s AND username ILIKE %s")

    def test_aggregation_ordering_and_upsert(self):
        sql=translate("SELECT GROUP_CONCAT(un.id, ',') FROM units un GROUP BY un.id HAVING stock<=m.minimum_stock ORDER BY m.name COLLATE NOCASE")
        self.assertIn('STRING_AGG(CAST(un.id AS TEXT)',sql)
        self.assertIn('HAVING COALESCE(SUM(l.quantity),0)<=',sql)
        self.assertIn('ORDER BY lower(m.name)',sql)
        self.assertIn('ON CONFLICT(user_id) DO UPDATE',translate('INSERT OR REPLACE INTO password_resets VALUES(?)'))

    def test_row_index_and_mapping_contract(self):
        row=Row([('id',7),('name','Item')])
        self.assertEqual(row[0],row['id'])
        self.assertEqual(dict(row),{'id':7,'name':'Item'})

    def test_dry_run_schema_is_complete(self):
        db=sqlite3.connect(':memory:')
        try:
            db.executescript((Path(__file__).parent/'migrations/sqlite_reference.sql').read_text(encoding='utf-8'))
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'medicines','lots','orders','notifications','users','transfers','catalog_sources'}<=tables)
        finally:db.close()
