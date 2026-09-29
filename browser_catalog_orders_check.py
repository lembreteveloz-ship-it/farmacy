"""Complete CAF import -> entry -> order -> two receipts -> PDF in real Edge."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
from browser_accessibility_check import app, sync_playwright, expect, audit, checks, nav, activate
from scripts.import_catalog import run as import_run

ROOT=Path(__file__).resolve().parent


def run(page):
    page.goto(page.base_url)
    page.locator('#login-form [name=username]').fill('admin')
    page.locator('#login-form [name=password]').fill('Catalog-Browser-123')
    activate(page,'#login-form button[type=submit]');expect(page.locator('#app')).to_be_visible()
    nav(page,'catalog');expect(page.locator('#catalog-count')).to_contain_text('250')
    audit(page,'catalog-master')
    page.locator('#catalog-filters [name=q]').fill('AMOXI')
    expect(page.locator('#catalog-count')).to_contain_text('2 itens')
    mid=page.evaluate("async()=>(await api('catalog?q=amoxi')).items.find(x=>x.concentration==='500 MG').id")
    activate(page,f'[data-action=catalog-entry][data-id="{mid}"]')
    page.locator('#catalog-search').fill('amoxi');page.locator('#catalog-search').press('ArrowDown')
    expect(page.locator('#modal-form [name=medicine_id]')).to_be_focused()
    page.locator('#modal-form [name=medicine_id]').select_option(str(mid))
    expect(page.locator('#entry-item-details')).to_contain_text('500 MG')
    page.locator('#modal-form [name=lot_number]').fill('CAF-INITIAL')
    page.locator('#modal-form [name=expiration_date]').fill('2090-01-01')
    page.locator('#modal-form [name=quantity]').fill('280')
    audit(page,'entry-from-catalog')
    activate(page,'#modal-submit');expect(page.locator('#print-medicine-label')).to_be_enabled(timeout=30000)
    assert page.evaluate("async()=>(await api('catalog?q=amoxi')).items.find(x=>x.concentration==='500 MG').stock")==280
    page.keyboard.press('Escape')
    nav(page,'orders');activate(page,'[data-action=order-needs]')
    page.locator('#needs-search').fill('amoxi')
    page.locator(f'#grid-need-{mid}').fill('1000')
    audit(page,'monthly-needs')
    activate(page,'#needs-form button');expect(page.locator('#toast')).to_contain_text('Necessidades mensais salvas')
    activate(page,'[data-action=order-back]');activate(page,'[data-action=order-new]')
    audit(page,'new-monthly-order');activate(page,'#modal-submit')
    expect(page.locator('#order-document')).to_be_visible()
    line=page.evaluate('(mid)=>currentOrder.lines.find(x=>x.medicine_id===mid)',mid)
    assert (line['monthly_need'],line['stock_snapshot'],line['suggestion'])==(1000,280,720)
    lineid=line['id'];oid=page.evaluate('currentOrder.id')
    page.locator('#order-line-filters [name=q]').fill('amoxi')
    page.locator(f'#grid-requested-{lineid}').fill('800')
    page.locator(f'#grid-requested-{lineid}').focus();page.keyboard.press('Tab')
    assert page.locator(':focus').get_attribute('name').startswith('requested-')
    audit(page,'order-draft-spreadsheet')
    activate(page,'#order-sheet button[value=save]');expect(page.locator('#toast')).to_contain_text('Pedido atualizado')
    assert page.evaluate('(id)=>currentOrder.lines.find(x=>x.id===id).requested',lineid)==800
    activate(page,'#order-sheet button[value=finalize]')
    expect(page.locator('[data-action=order-send]')).to_be_visible()
    page.locator(f'#grid-released-{lineid}').fill('300');activate(page,'#order-sheet button[value=release]')
    expect(page.locator(f'[data-action=order-receive][data-id="{lineid}"]')).to_be_visible()
    assert page.evaluate('currentOrder.status')=='Parcialmente atendido'
    for index,qty in [(1,200),(2,600)]:
        if index==2:
            previous_version=page.evaluate('currentOrder.version')
            page.locator(f'#grid-released-{lineid}').fill('800');activate(page,'#order-sheet button[value=release]')
            page.wait_for_function('(version)=>currentOrder.version>version',arg=previous_version)
            expect(page.locator(f'[data-action=order-receive][data-id="{lineid}"]')).to_be_visible()
        activate(page,f'[data-action=order-receive][data-id="{lineid}"]')
        page.locator('#modal-form [name=quantity]').fill(str(qty))
        page.locator('#modal-form [name=lot_number]').fill(f'CAF-RECEIVED-{index}')
        page.locator('#modal-form [name=expiration_date]').fill('2091-01-01')
        page.locator('#modal-form [name=document]').fill('NF-TEST')
        audit(page,f'physical-receipt-{index}')
        activate(page,'#modal-submit')
        try:expect(page.locator('#print-medicine-label')).to_be_enabled(timeout=30000)
        except AssertionError:
            print('Receipt failure:',index,page.locator('#modal-error').text_content(),flush=True)
            raise
        expect(page.locator('.qr-a4-sheet')).to_be_visible();page.keyboard.press('Escape')
        expect(page.locator('#order-document')).to_be_visible()
    assert page.evaluate('currentOrder.status')=='Recebido'
    assert page.evaluate('currentOrder.receipts.length')==2
    assert page.evaluate('(id)=>currentOrder.lines.find(x=>x.id===id).stock_snapshot',lineid)==280
    assert page.evaluate("async()=>(await api('catalog?q=amoxi')).items.find(x=>x.concentration==='500 MG').stock")==1080
    audit(page,'order-received-desktop')
    for width,height in [(390,844),(844,390),(320,640),(640,480),(768,1024)]:
        page.set_viewport_size({'width':width,'height':height})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(width,'page overflow')
        assert page.locator('.order-grid tr').last.evaluate('(x)=>getComputedStyle(x).display')==('block' if width<=760 else 'table-row')
        audit(page,f'order-cards-{width}')
    page.set_viewport_size({'width':1280,'height':800})
    with page.expect_download() as pdf:activate(page,'[data-action=order-pdf]')
    assert Path(pdf.value.path()).read_bytes().startswith(b'%PDF-')
    with page.expect_download() as csv:activate(page,'[data-action=order-csv]')
    assert '800' in Path(csv.value.path()).read_text(encoding='utf-8-sig')
    page.evaluate('window.print=()=>{window.printRequested=true}')
    activate(page,'[data-action=order-print]');assert page.evaluate('window.printRequested')
    page.emulate_media(media='print');expect(page.locator('.no-order-print').first).not_to_be_visible()
    page.emulate_media(media='screen');page.evaluate("window.dispatchEvent(new Event('afterprint'))")
    activate(page,'[data-action=order-back]')
    page.locator('#order-history-filters [name=status]').select_option('Recebido')
    page.locator('#order-history-filters [name=q]').fill('Amoxicilina');activate(page,'#order-history-filters button')
    expect(page.locator(f'[data-action=order-open][data-id="{oid}"]')).to_be_visible()
    # Authenticated requests without CSRF cannot mutate the new resources.
    assert page.request.post(page.base_url+'/api/orders',data={}).status==403
    assert page.request.get(page.base_url+f'/api/orders/{oid}',headers={'X-Unit-ID':'999'}).status==404
    assert not any(x['violations'] for x in checks),'Accessibility violations'


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'FARMACIA_ADMIN_PASSWORD':'Catalog-Browser-123'}):
        app.DATA=Path(directory)/'data';app.DATA.mkdir();app.DATABASE=app.DATA/'test.db';app.BACKUPS=app.DATA/'backups'
        with contextlib.redirect_stderr(io.StringIO()):app.initialize_db()
        first=import_run(app.DATABASE,ROOT/'data/catalogo_caf_ipixuna_COMPLETO.json');second=import_run(app.DATABASE,ROOT/'data/catalogo_caf_ipixuna_COMPLETO.json')
        assert first['inserted']==250 and second['existing']==250 and second['inserted']==0
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.PharmacyHandler);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1280,'height':800},reduced_motion='reduce');page.base_url=f'http://127.0.0.1:{server.server_port}'
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                run(page);assert not errors,errors;browser.close()
            print('CATALOG_ORDERS_BROWSER_PASS: import twice, search, entry 280, need 1000, suggestion 720, request 800, release 300/800, receipts 200+600, stock 1080, QR, history, PDF, CSV, print, mobile cards, axe, CSRF')
        finally:
            (ROOT/'catalog-orders-browser-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
            server.shutdown();server.server_close()
