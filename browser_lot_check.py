"""Integration check in installed headless Edge, using an isolated database."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pharmacy', Path(__file__).with_name('import hashlib.py'))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

SCRIPT = r'''
window.addEventListener('load',async()=>{
 const result=document.createElement('pre');result.id='browser-result';document.body.append(result);
 const wait=async predicate=>{for(let i=0;i<150;i++){if(predicate())return;await new Promise(r=>setTimeout(r,100));}throw new Error('Timeout');};
 let stage='login';try{
  await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'Browser-Test-123'})});await start();
  await openAction('entry','');const form=document.querySelector('#modal-form');
  for(const [name,value] of Object.entries({lot_number:'BROWSER-LOT',expiration_date:'2090-01-01',quantity:'80'}))form.elements[name].value=value;
  form.requestSubmit();await wait(()=>document.querySelector('.qr-a4-sheet')&&!document.querySelector('#print-medicine-label').disabled);
  const code=(await api('lots')).items.find(x=>x.lot_number==='BROWSER-LOT').internal_code;
  if(document.querySelector('.lot-barcode'))throw new Error('Linear barcode still present');
  stage='PDF preview';if(!document.querySelector('#preview-label-pdf'))throw new Error('PDF button missing');
  const generated=await api('reports/pdf',{method:'POST',body:{kind:'lot_label',code}});const response=await fetch(generated.download_url);const pdf=await response.blob();if(pdf.type!=='application/pdf'||pdf.size<1000||!(await pdf.slice(0,8).text()).startsWith('%PDF-'))throw new Error('Invalid PDF');
  const originalClick=HTMLAnchorElement.prototype.click,originalOpen=window.open,originalBlob=URL.createObjectURL,currentUrl=window.location.href;let clicked=false;
  HTMLAnchorElement.prototype.click=function(){if(!this.isConnected||!this.download||!this.href.startsWith(window.location.origin+'/api/files/reports/'))throw new Error('Invalid HTTP download');clicked=true;};
  window.open=()=>{throw new Error('Unexpected window navigation');};URL.createObjectURL=()=>{throw new Error('Unexpected Blob URL');};
  try{await downloadPdf('/api/reports/pdf',{kind:'lot_label',code});if(!clicked||window.location.href!==currentUrl)throw new Error('Download changed current page');}finally{HTMLAnchorElement.prototype.click=originalClick;window.open=originalOpen;URL.createObjectURL=originalBlob;}
  if(!/^UBS-\d+-\d+$/.test(code))throw new Error('Invalid lot code');
  const reader=document.createElement('div');reader.id='test-reader';document.body.append(reader);
  const decoder=new Html5Qrcode('test-reader',{formatsToSupport:[Html5QrcodeSupportedFormats.CODE_128,Html5QrcodeSupportedFormats.QR_CODE],verbose:false});
  const decode=async(canvas,name)=>{const blob=await new Promise(r=>canvas.toBlob(r));const actual=await decoder.scanFile(new File([blob],name,{type:'image/png'}),false);if(actual!==code)throw new Error('Decoded value differs: '+actual);};
  stage='QR sheet decode';const image=document.querySelector('.qr-a4-sheet');await image.decode();
  const preview=await api('reports/pdf',{method:'POST',body:{kind:'lot_label',code,copies:100,codes_per_sheet:30,preview:true}});
  if(preview.sheet_urls.length!==4||preview.layout.columns!==6||preview.layout.rows!==5)throw new Error('Wrong pagination');
  if(!image.src.startsWith(window.location.origin+'/api/files/label-previews/'))throw new Error('Preview must use authenticated HTTP URL');
  const layout=preview.layout,canvas=document.createElement('canvas');canvas.width=Math.ceil(layout.cell_width*4);canvas.height=Math.ceil(layout.cell_height*4);
  const scale=image.naturalWidth/layout.width;
  canvas.getContext('2d').drawImage(image,layout.margin*scale,layout.margin*scale,layout.cell_width*scale,layout.cell_height*scale,0,0,canvas.width,canvas.height);
  await decode(canvas,'qr-sheet.png');decoder.clear();document.querySelector('#modal').close();
  stage='quick exit';
  state.page='quickexit';await render();document.querySelector('#lot-code').value=code;document.querySelector('#lot-scan-form').requestSubmit();await wait(()=>document.querySelector('#quick-exit-form'));
  const exit=document.querySelector('#quick-exit-form');exit.elements.quantity.value='10';exit.elements.reason.value='Dispensação';exit.requestSubmit();await wait(()=>document.querySelector('#quick-exit-result').textContent.includes('Novo estoque: 70'));
  const scan=await api('lots/scan?code='+encodeURIComponent(code));if(scan.lot.quantity!==70)throw new Error('Wrong remaining stock');
  result.textContent='BROWSER_LOT_TEST_PASS: PDF generation, entry label, A4 landscape 100 labels/4 sheets, QR decode, HID Enter lookup, exact-lot exit 80 to 70';
 }catch(e){result.textContent='BROWSER_LOT_TEST_FAIL: '+stage+': '+e.message;}
});
'''


class Handler(app.PharmacyHandler):
    def do_GET(self):
        if self.path in ('/browser-test', '/browser-test.js'):
            if self.path.endswith('.js'):
                content = SCRIPT.encode()
                kind = 'text/javascript'
            else:
                content = (app.PUBLIC / 'index.html').read_text(encoding='utf-8').replace('</head>', '<script src="/browser-test.js" defer></script></head>').encode()
                kind = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            super().do_GET()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'FARMACIA_ADMIN_PASSWORD': 'Browser-Test-123'}):
        app.DATA = Path(directory) / 'data'
        app.DATABASE = app.DATA / 'test.db'
        app.BACKUPS = app.DATA / 'backups'
        with contextlib.redirect_stderr(io.StringIO()):
            app.initialize_db()
        with contextlib.closing(app.connect_db()) as db:
            actor = dict(db.execute('SELECT * FROM users LIMIT 1').fetchone())
            object.__new__(app.PharmacyHandler).create_medicine(db, actor, {'name': 'Paracetamol', 'concentration': '500 mg', 'stock_unit': 'Comprimido'})
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
            run = subprocess.run([str(edge), '--headless', '--disable-gpu', '--no-first-run', '--disable-extensions',
                '--user-data-dir=' + str(Path(directory) / 'edge'), '--dump-dom', '--virtual-time-budget=20000',
                f'http://127.0.0.1:{server.server_port}/browser-test'], capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            output = run.stdout.decode('utf-8', errors='replace')
            import re
            match = re.search(r'<pre id="browser-result">(.*?)</pre>', output, re.S)
            print(match.group(1) if match else 'Browser did not return test results: ' + run.stderr.decode(errors='replace')[:600])
            if not match or 'BROWSER_LOT_TEST_PASS' not in match.group(1):
                raise SystemExit(1)
        finally:
            server.shutdown()
            server.server_close()
