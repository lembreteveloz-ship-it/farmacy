"""Keyboard navigation and notification center, isolated data and real Edge."""
import json
from pathlib import Path
import threading
from browser_accessibility_check import app,sync_playwright,expect,audit,checks,nav,activate
from test_notifications import NotificationTests, app

ROOT=Path(__file__).resolve().parent


def login(page,url,username,password):
    page.goto(url)
    page.locator('#login-form [name=username]').fill(username)
    page.locator('#login-form [name=password]').fill(password)
    activate(page,'#login-form button[type=submit]');expect(page.locator('#app')).to_be_visible()
    expect(page.locator('#notification-bell')).to_be_visible()
    page.wait_for_function("()=>document.querySelector('#notification-bell').getAttribute('aria-label').includes('pendentes')")


def run(page,fixture,url,browser):
    login(page,url,'admin','Notice-Test-123')
    assert page.locator('#navigation [data-group] > span:first-child').all_text_contents()==['Início','Estoque','Movimentações','Gestão']
    activate(page,'#navigation [data-group=stock]')
    assert page.evaluate('state.page')=='dashboard'
    expect(page.locator('#navigation [data-page=lots]')).to_be_visible()
    page.keyboard.press('Space')
    expect(page.locator('#navigation [data-page=lots]')).not_to_be_visible()
    expect(page.locator('#navigation [data-group=stock]')).to_be_focused()
    activate(page,'#navigation [data-group=home]')
    expect(page.locator('#navigation [data-page=dashboard]')).to_be_visible()
    for target in ['dashboard','medicines','lots','inventory','alerts','replenishment','movements','quickexit','losses','transfers','orders','requisitions','reports','users','catalog','audit']:
        nav(page,target)
        assert page.evaluate('state.page')==target
        assert page.locator('#navigation [data-group]').count()==4
        assert page.locator('#navigation [data-page]').count()==8
        assert page.locator('#navigation [aria-current=page]').count()==1
        assert page.locator('#section-navigation').count()==0
    nav(page,'movements');page.locator('#workspace-view').select_option('adjustments')
    page.wait_for_function('()=>state.onlyAdjustments')
    assert page.locator('#navigation [data-group=movement]').get_attribute('aria-expanded')=='true'
    audit(page,'consolidated-navigation')
    activate(page,'#notification-bell');expect(page.locator('#notification-title')).to_be_focused()
    for _ in range(30):
        page.keyboard.press('Tab');assert page.evaluate("document.querySelector('#notification-panel').contains(document.activeElement)")
    audit(page,'notification-panel-desktop')
    notifications=page.evaluate("async()=>(await api('notifications')).items")
    expiry=next(n for n in notifications if n['kind']=='expiry')
    activate(page,f'#notification-open-{expiry["id"]}')
    expect(page.locator('#notification-panel')).not_to_be_visible()
    expect(page.locator('.notification-target')).to_be_visible()
    expect(page.locator('#list-content')).to_contain_text('N-1')
    assert page.evaluate('state.page')=='lots'
    activate(page,'#notification-bell');activate(page,'#notification-read-all')
    expect(page.locator('#notification-summary')).to_contain_text('0 não lidas')
    expect(page.locator('#notification-bell .notification-count')).to_be_visible()
    assert page.evaluate("async()=>(await api('notifications')).pending")>0
    page.keyboard.press('Escape');expect(page.locator('#notification-bell')).to_be_focused()
    page.reload();expect(page.locator('#app')).to_be_visible()
    page.wait_for_function("()=>document.querySelector('#notification-bell').dataset.ready==='true'")
    assert page.evaluate("async()=>(await api('notifications')).unread")==0
    expect(page.locator('#notification-bell .notification-count')).to_be_visible()
    fixture.send()
    # Wait for the actual 30-second polling interval, without manually refreshing the page.
    expect(page.locator('#notification-banner')).to_contain_text('novo(s) aviso(s)',timeout=40000)
    expect(page.locator('#notification-bell .notification-count')).to_be_visible()
    incoming=page.evaluate("async()=>(await api('notifications')).items.find(x=>x.kind==='transfer'&&!x.resolved_at)")
    activate(page,'#notification-bell');activate(page,f'#notification-open-{incoming["id"]}')
    expect(page.locator('.notification-target')).to_be_visible()
    assert page.evaluate('state.unit')==2
    expect(page.locator('.notification-target [data-action=receber]')).to_be_visible()
    for width,height in [(1440,900),(390,844),(844,390),(320,640),(640,480)]:
        page.set_viewport_size({'width':width,'height':height})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        activate(page,'#notification-bell')
        assert page.locator('#notification-panel').evaluate('(x)=>x.scrollWidth<=x.clientWidth+1'),width
        audit(page,f'notification-panel-{width}')
        page.screenshot(path=str(ROOT/f'notification-{width}.png'))
        page.keyboard.press('Escape');expect(page.locator('#notification-bell')).to_be_focused()
    # Independent login verifies read state and that the receiver never sees origin-only lots.
    other=browser.new_context(viewport={'width':390,'height':844});receiver=other.new_page()
    login(receiver,url,'destino','Notice-User-123')
    assert receiver.locator('#navigation [data-page=users]').count()==0
    nav(receiver,'catalog')
    assert receiver.evaluate('state.page')=='catalog'
    assert receiver.locator('#navigation [data-page=medicines]').get_attribute('aria-current')=='page'
    data=receiver.evaluate("async()=>await api('notifications')")
    assert all(x['unit_id']==2 for x in data['items'])
    assert any(x['kind']=='transfer' and not x['read_at'] for x in data['items'])
    denied=receiver.evaluate("async id=>{const r=await fetch('/api/notifications/read',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.session.csrfToken},body:JSON.stringify({ids:[id]})});return r.status}",expiry['id'])
    assert denied==404
    assert receiver.request.post(url+'/api/notifications/read',data={'ids':[]}).status==403
    activate(receiver,'#notification-bell');audit(receiver,'notifications-restricted-user')
    other.close()
    assert not any(c['violations'] for c in checks),'axe violations'


if __name__=='__main__':
    fixture=NotificationTests('test_scope_including_administrator_and_other_organization');fixture.setUp()
    server=app.ThreadingHTTPServer(('127.0.0.1',0),app.PharmacyHandler);threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':800},reduced_motion='reduce')
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            run(page,fixture,f'http://127.0.0.1:{server.server_port}',browser)
            assert not errors,errors;browser.close()
        print('NAVIGATION_NOTIFICATIONS_PASS: 4 groups, all destinations, keyboard, persisted reads, counters, real polling, direct links, unit permissions, responsive panel, axe')
    finally:
        (ROOT/'navigation-notifications-test-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
        server.shutdown();server.server_close();fixture.doCleanups()
