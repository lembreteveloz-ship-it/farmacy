import http.client
import json
import threading
import unittest
from unittest.mock import patch
import test_stock_improvements
from test_password_security import app


class PdfReportsTests(unittest.TestCase):
    def setUp(self):
        test_stock_improvements.ImprovementsTests.setUp(self)
        self.handler.path = '/api/reports/pdf'

    def test_all_available_reports_and_labels_are_pdf(self):
        code = self.db.execute('SELECT internal_code FROM lots LIMIT 1').fetchone()[0]
        kinds = ['weekly','monthly','entries','exits','stock','expiring','losses','inventory','transfers','consolidated','replenishment','lot_label']
        for kind in kinds:
            with self.subTest(kind=kind):
                result=self.handler.generate_pdf_file(self.db,self.user,1,{'kind':kind,'code':code,'medicine_id':self.medicine})
                self.assertTrue(result['download_url'].startswith('/api/files/reports/'))
                row=self.db.execute('SELECT content FROM generated_pdf_files ORDER BY rowid DESC LIMIT 1').fetchone()
                self.assertTrue(row[0].startswith(b'%PDF-'))
                self.assertGreater(len(row[0]),1000)

    def test_generation_rejects_other_units_and_unsupported_requests(self):
        for payload in [{'kind':'stock','unit_id':2},{'kind':'requisitions'}]:
            with self.assertRaises(app.ApiError):
                self.handler.generate_pdf_file(self.db,self.user,1,payload)
        denied={**self.user,'role':'Consulta','units':[{'id':2}]}
        with self.assertRaises(app.ApiError):
            self.handler.generate_pdf_file(self.db,denied,1,{'kind':'stock'})

    def test_lot_label_is_minimal_and_technical_only(self):
        code = self.db.execute('SELECT internal_code FROM lots LIMIT 1').fetchone()[0]
        from pdf_reports import render_label_sheet_pdf
        with patch('pdf_reports.render_label_sheet_pdf', wraps=render_label_sheet_pdf) as renderer:
            self.handler.generate_pdf_file(self.db,self.user,1,{'kind':'lot_label','code':code})
        label = renderer.call_args.args[0][0]
        self.assertEqual(label['code'], code)
        self.assertEqual(set(label), {'code', 'medicine_name', 'concentration'})
        row = self.db.execute('SELECT content FROM generated_pdf_files ORDER BY rowid DESC LIMIT 1').fetchone()
        pdf = row[0]
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertIn(b'/Count 1', pdf)

    def test_lot_label_qr_and_copies_create_multiple_a4_pages(self):
        code = self.db.execute('SELECT internal_code FROM lots LIMIT 1').fetchone()[0]
        self.handler.generate_pdf_file(self.db,self.user,1,{
            'kind':'lot_label','code':code,'copies':100,'codes_per_sheet':30})
        row = self.db.execute('SELECT content FROM generated_pdf_files ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertTrue(row[0].startswith(b'%PDF-'))
        self.assertIn(b'/Count 4', row[0])

    def test_label_grid_uses_columns_and_fills_left_to_right(self):
        from pdf_reports import label_grid_dimensions
        self.assertEqual(label_grid_dimensions(30), (6, 5))
        self.assertEqual(label_grid_dimensions(35), (7, 5))
        self.assertEqual(label_grid_dimensions(40), (8, 5))

    def test_qr_sheet_preview_matches_pdf_geometry_and_partial_page(self):
        from pdf_reports import label_layout, label_sheet_drawing, render_label_preview, render_label_sheet_pdf
        from reportlab.lib.units import mm
        label={'code':'UBS-000003-000025','medicine_name':'Paracetamol','concentration':'500 mg'}
        for capacity in (30,35,40):
            layout=label_layout(capacity)
            self.assertGreaterEqual(layout['qr_size']/mm,25)
            self.assertAlmostEqual(layout['width']/mm,297)
            self.assertAlmostEqual(layout['height']/mm,210)
        full=label_sheet_drawing([label]*30)
        partial=label_sheet_drawing([label]*10)
        # First QR remains in exactly the same position on the final partial page.
        self.assertEqual(full.contents[1].transform,partial.contents[1].transform)
        # Groups are QR, followed by text. Compare columns and next row explicitly.
        groups=[node for node in full.contents if type(node).__name__=='Group']
        self.assertGreater(groups[1].transform[4],groups[0].transform[4])
        self.assertEqual(groups[1].transform[5],groups[0].transform[5])
        self.assertEqual(groups[6].transform[4],groups[0].transform[4])
        self.assertLess(groups[6].transform[5],groups[0].transform[5])
        preview=render_label_preview([label]*31)
        self.assertEqual(len(preview['sheets']),2)
        self.assertEqual(preview['sheets'][1].count('Paracetamol'),1)
        pdf=render_label_sheet_pdf([label]*31)
        self.assertIn(b'/PrintScaling /None',pdf)
        self.assertRegex(pdf,rb'/MediaBox\s*\[\s*0\s+0\s+841\.\d+\s+595\.\d+\s*\]')

    def test_reject_barcode_and_unsafe_sheet_sizes(self):
        code=self.db.execute('SELECT internal_code FROM lots LIMIT 1').fetchone()[0]
        for extra in [{'variant':'barcode'},{'variant':'both'},{'codes_per_sheet':20},{'copies':1001},{'copies':0}]:
            with self.subTest(extra=extra),self.assertRaises(app.ApiError):
                self.handler.generate_pdf_file(self.db,self.user,1,{'kind':'lot_label','code':code,**extra})

    def test_http_csrf_headers_ownership_expiry_and_unit_isolation(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.PharmacyHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        connection=http.client.HTTPConnection(*server.server_address)
        def request(method,path,body=None,headers=None):
            connection.request(method,path,json.dumps(body) if body is not None else None,headers or {})
            response=connection.getresponse();return response.status,dict(response.getheaders()),response.read()
        try:
            self.assertEqual(request('POST','/api/reports/pdf',{'kind':'stock'},{'Content-Type':'application/json'})[0],401)
            status,headers,_=request('POST','/api/login',{'username':'admin','password':'Test-Password-123'},{'Content-Type':'application/json'})
            cookie=headers['Set-Cookie'].split(';')[0]
            headers={'Cookie':cookie,'Content-Type':'application/json','X-Unit-ID':'1'}
            status,_,data=request('GET','/api/session',headers=headers)
            csrf=json.loads(data)['csrfToken']
            self.assertEqual(request('POST','/api/reports/pdf',{'kind':'stock'},headers)[0],403)
            headers['X-CSRF-Token']=csrf
            status,_,data=request('POST','/api/reports/pdf',{'kind':'stock'},headers)
            self.assertEqual(status,201)
            url=json.loads(data)['download_url']
            status,pdf_headers,content=request('GET',url,headers=headers)
            self.assertEqual(status,200)
            self.assertEqual(pdf_headers['Content-Type'],'application/pdf')
            self.assertIn('attachment;',pdf_headers['Content-Disposition'])
            self.assertEqual(pdf_headers['Cache-Control'],'no-store')
            self.assertTrue(content.startswith(b'%PDF-'))
            code=self.db.execute('SELECT internal_code FROM lots LIMIT 1').fetchone()[0]
            status,_,preview_data=request('POST','/api/reports/pdf',{'kind':'lot_label','code':code,'preview':True,'copies':31},headers)
            self.assertEqual(status,201)
            preview=json.loads(preview_data)
            self.assertEqual(len(preview['sheet_urls']),2)
            preview_url=preview['sheet_urls'][0]
            status,svg_headers,svg=request('GET',preview_url,headers=headers)
            self.assertEqual(status,200)
            self.assertEqual(svg_headers['Content-Type'],'image/svg+xml; charset=utf-8')
            self.assertEqual(svg_headers['Cache-Control'],'no-store')
            self.assertIn(b'<svg',svg)
            self.assertEqual(request('GET',preview_url)[0],401)
            self.assertEqual(request('GET',preview_url.replace('unit_id=1','unit_id=2'),headers=headers)[0],403)
            mistaken_pdf=preview_url.replace('/label-previews/','/reports/').replace('.svg?','.pdf?')
            self.assertEqual(request('GET',mistaken_pdf,headers=headers)[0],404)
            self.assertEqual(request('GET',url.replace('unit_id=1','unit_id=2'),headers=headers)[0],403)
            self.assertEqual(request('GET',url)[0],401)
            self.db.execute('UPDATE generated_pdf_files SET user_id=999');self.db.commit()
            self.assertEqual(request('GET',url,headers=headers)[0],404)
            self.db.execute("UPDATE generated_pdf_files SET user_id=?,expires_at='2000-01-01'",(self.user['id'],));self.db.commit()
            self.assertEqual(request('GET',url,headers=headers)[0],404)
        finally:
            connection.close();server.shutdown();server.server_close();thread.join()

    def test_stock_pdf_never_contains_other_unit(self):
        organization_id = self.db.execute('SELECT organization_id FROM units WHERE id=1').fetchone()[0]
        self.db.execute("INSERT INTO units(id,name,created_at,organization_id) VALUES(2,'Unidade secreta',?,?)",(app.now_iso(),organization_id));self.db.commit()
        self.handler.create_entry(self.db,self.user,2,{'medicine_id':self.medicine,'lot_number':'SECRET-LOT','expiration_date':'2099-01-01','quantity':123})
        from unittest.mock import patch
        with patch('pdf_reports.render_pdf',return_value=b'%PDF-test') as renderer:
            self.handler.generate_pdf_file(self.db,self.user,1,{'kind':'stock'})
            self.assertNotIn('SECRET-LOT',str(renderer.call_args))


if __name__=='__main__':
    unittest.main()
