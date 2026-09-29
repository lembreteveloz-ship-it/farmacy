"""Real Edge keyboard + axe checks. Isolated database; never uses production stock.

Test dependencies (not needed by the application): test_tools/playwright and axe.min.js.
This does not replace NVDA/VoiceOver and physical-device acceptance testing.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'test_tools'))
from playwright.sync_api import sync_playwright, expect
from browser_lot_check import app

checks = []


def audit(page, name):
    page.evaluate((ROOT / 'test_tools/axe.min.js').read_text(encoding='utf-8'))
    results = page.evaluate("""async()=>{const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}});return r.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}));}""")
    checks.append({'screen': name, 'violations': results})
    if results:
        print(json.dumps(checks[-1], ensure_ascii=True), flush=True)


def nav(page, name):
    group=page.evaluate('(name)=>navGroupFor(name)',name)
    area=page.evaluate('(name)=>navAreaFor(name)',name)
    if page.locator(f'#navigation [data-group="{group}"]').get_attribute('aria-expanded')=='false':
        activate(page,f'#navigation [data-group="{group}"]')
    activate(page,f'#navigation [data-page="{area}"]')
    page.wait_for_function("()=>!document.querySelector('#main .loading')")
    if name=='requisitions':activate(page,'.area-tools [data-page=requisitions]')
    elif name!=area:page.locator('#workspace-view').select_option(name)
    page.wait_for_function("()=>!document.querySelector('#main .loading')")
    expect(page.locator('#main h1')).to_be_visible()


def activate(page, selector):
    page.locator(selector).focus()
    page.keyboard.press('Enter')


def run(page):
    page.wait_for_function("()=>typeof accessibility!=='undefined'")
    password=page.locator('#login-form [name=password]')
    password.fill('Browser-Test-123')
    password.focus();page.keyboard.press('Tab')
    expect(page.locator(':focus')).to_have_attribute('aria-label','Mostrar senha')
    page.keyboard.press('Space');expect(password).to_have_attribute('type','text')
    page.keyboard.press('Enter');expect(password).to_have_attribute('type','password')
    expect(password).to_have_value('Browser-Test-123')
    await_error=page.locator('#login-form [name=username]')
    activate(page,'#login-form button[type=submit]')
    expect(await_error).to_be_focused()
    expect(await_error).to_have_attribute('aria-invalid','true')
    await_error.fill('admin')
    password.fill('Wrong-password')
    activate(page,'#login-form button[type=submit]')
    expect(page.locator('#login-error')).not_to_be_empty()
    await_error.fill('admin');password.fill('Browser-Test-123')
    await_error.focus()
    await_error.press('Tab')
    await_error.press('Shift+Tab')
    await_error.focus()
    audit(page,'login')
    for width in [320,390,640,853]:
        page.set_viewport_size({'width':width,'height':800})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
        audit(page,f'login-width-{width}')
    page.set_viewport_size({'width':1280,'height':800})
    # Reset password and all three password fields, keyboard only.
    activate(page,'#reset-password-button')
    expect(page.locator('#modal')).to_be_visible()
    audit(page,'reset-password')
    page.keyboard.press('Escape')
    expect(page.locator('#reset-password-button')).to_be_focused()
    activate(page,'#login-form button[type=submit]')
    expect(page.locator('#app')).to_be_visible()
    page.locator('#unit-select').select_option('1')
    page.wait_for_function("()=>!document.querySelector('#main .loading')")
    audit(page,'dashboard')
    activate(page,'#password-button')
    expect(page.locator('#modal-title')).to_be_focused()
    for _ in range(22):
        page.keyboard.press('Tab')
        assert page.evaluate("document.querySelector('#modal').contains(document.activeElement)")
    for _ in range(22):
        page.keyboard.press('Shift+Tab')
        assert page.evaluate("document.querySelector('#modal').contains(document.activeElement)")
    assert page.locator('#modal .password-toggle').count()==3
    for button in page.locator('#modal .password-toggle').all():
        button.focus();page.keyboard.press('Space')
        target=button.get_attribute('aria-controls')
        expect(page.locator('#'+target)).to_have_attribute('type','text')
        page.keyboard.press('Enter');expect(page.locator('#'+target)).to_have_attribute('type','password')
    audit(page,'change-password')
    page.keyboard.press('Escape');expect(page.locator('#password-button')).to_be_focused()
    nav(page,'users');activate(page,'[data-action=user]:not([data-id]), [data-action=user][data-id=""]')
    audit(page,'new-user');page.keyboard.press('Escape')
    for name in ['medicines','lots','movements','transfers','inventory','alerts','reports','requisitions','losses','replenishment','audit']:
        nav(page,name);audit(page,name)
    nav(page,'lots');activate(page,'[data-action=lot-label]')
    expect(page.locator('#print-medicine-label')).to_be_enabled(timeout=30000)
    audit(page,'qr-label-preview');page.keyboard.press('Escape')
    nav(page,'inventory');activate(page,'[data-action=inventory]')
    page.locator('[name=counted_quantity]').fill('79')
    page.locator('[name=reason]').fill('Contagem física conferida')
    audit(page,'inventory-count')
    activate(page,'#modal-submit');expect(page.locator('#modal')).not_to_be_visible()
    nav(page,'transfers');activate(page,'[data-action=transfer]')
    expect(page.locator('#modal-submit')).to_be_enabled()
    audit(page,'new-transfer')
    page.locator('[name=quantity]').fill('10');activate(page,'#modal-submit')
    expect(page.locator('#modal')).not_to_be_visible()
    expect(page.locator('#list-content')).to_contain_text('Pendente de Recebimento')
    page.locator('#unit-select').select_option('2')
    page.wait_for_function("()=>!document.querySelector('#main .loading') && document.querySelector('[data-action=receber]')")
    activate(page,'#list-content [data-action=receber]')
    audit(page,'receive-transfer');activate(page,'#modal-submit')
    expect(page.locator('#modal')).not_to_be_visible()
    expect(page.locator('#list-content')).to_contain_text('Recebida')
    page.locator('#unit-select').select_option('1')
    page.wait_for_function("()=>!document.querySelector('#main .loading')")
    nav(page,'quickexit')
    code=page.evaluate("async()=>(await api('lots')).items[0].internal_code")
    page.locator('#lot-code').fill('INVALID');page.keyboard.press('Enter')
    expect(page.locator('#lot-scan-error')).not_to_be_empty()
    # No physical camera claimed: this checks the unavailable/denied camera message.
    activate(page,'#lot-camera-button')
    expect(page.locator('#lot-scan-error')).not_to_be_empty()
    page.locator('#lot-code').fill(code);page.locator('#lot-code').press('Enter')
    expect(page.locator('#quick-exit-form')).to_be_visible()
    expect(page.locator('#scan-status')).to_contain_text('Código identificado')
    page.locator('#quick-exit-form [name=quantity]').fill('9999')
    activate(page,'#quick-exit-form button');expect(page.locator('#quick-exit-form [name=quantity]')).to_have_attribute('aria-invalid','true')
    audit(page,'quick-exit-error')
    page.locator('#quick-exit-form [name=quantity]').fill('2')
    activate(page,'#quick-exit-form button')
    expect(page.locator('#quick-exit-result')).to_contain_text('Saída confirmada')
    # Session warning reaches an open modal and renewal preserves its fields.
    activate(page,'#password-button');page.locator('[name=current_password]').fill('draft-value')
    page.evaluate("accessibility.session(new Date(Date.now()+60000).toISOString())")
    expect(page.locator('.session-dialog-warning')).to_be_visible()
    activate(page,'.session-dialog-warning button')
    expect(page.locator('.session-dialog-warning')).to_have_count(0)
    expect(page.locator('[name=current_password]')).to_have_value('draft-value')
    page.keyboard.press('Escape')
    # Reflow equivalent to 1280px browser at 100/150/200/400%, plus portrait/landscape.
    for width,height in [(1440,900),(1280,800),(853,600),(640,480),(320,640),(390,844),(844,390),(768,1024),(1024,768)]:
        page.set_viewport_size({'width':width,'height':height})
        for screen in ['dashboard','lots','reports','quickexit']:
            nav(page,screen)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'), (width,screen,'horizontal overflow')
        activate(page,'#password-button')
        assert page.evaluate("document.querySelector('#modal').scrollWidth <= document.querySelector('#modal').clientWidth+1"),(width,'dialog overflow')
        audit(page,f'password-{width}x{height}')
        if width in [320,1280]:page.screenshot(path=str(ROOT/f'accessibility-password-{width}.png'))
        page.keyboard.press('Escape')
    page.set_viewport_size({'width':1280,'height':800})
    # Inspect browser accessibility tree as support, not a claim of human screen-reader testing.
    activate(page,'#password-button')
    cdp=page.context.new_cdp_session(page)
    tree=cdp.send('Accessibility.getFullAXTree')['nodes']
    assert any(n.get('role',{}).get('value')=='dialog' and n.get('name',{}).get('value')=='Alterar senha' for n in tree)
    assert sum(n.get('name',{}).get('value')=='Mostrar senha' and n.get('role',{}).get('value')=='button' for n in tree)==3
    page.keyboard.press('Escape')
    nav(page,'reports')
    with page.expect_download() as download:
        activate(page,'[data-action=report-pdf]')
    assert download.value.suggested_filename.endswith('.pdf')


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'FARMACIA_ADMIN_PASSWORD':'Browser-Test-123'}):
        app.DATA=Path(directory)/'data';app.DATABASE=app.DATA/'test.db';app.BACKUPS=app.DATA/'backups'
        with contextlib.redirect_stderr(io.StringIO()):app.initialize_db()
        with contextlib.closing(app.connect_db()) as db:
            actor=dict(db.execute('SELECT * FROM users LIMIT 1').fetchone());actor['units']=[{'id':1},{'id':2}]
            db.execute("INSERT INTO units(id,name,created_at,organization_id) VALUES(2,'Destino',?,?)",(app.now_iso(),actor['organization_id']));db.commit()
            handler=object.__new__(app.PharmacyHandler)
            medicine=handler.create_medicine(db,actor,{'name':'Paracetamol','concentration':'500 mg','stock_unit':'Comprimido'})['id']
            handler.create_entry(db,actor,1,{'medicine_id':medicine,'lot_number':'A11Y-TEST','expiration_date':'2090-01-01','quantity':80})
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.PharmacyHandler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1280,'height':800},reduced_motion='reduce')
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                run(page)
                assert not errors,errors
                browser.close()
            assert not any(c['violations'] for c in checks),'axe found accessibility violations'
            print('ACCESSIBILITY_CHECK_PASS: keyboard, password, axe, responsive reflow, errors, dialogs, stock, transfer, inventory, reports, session, AX tree')
        finally:
            (ROOT/'accessibility-test-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
            server.shutdown();server.server_close()
