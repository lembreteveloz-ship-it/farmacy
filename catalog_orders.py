"""CAF catalog and monthly orders. Catalog rows never represent physical stock."""
import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


TYPES = ('MEDICAMENTO', 'MATERIAL', 'TESTE_RAPIDO')
STATUSES = ('Rascunho', 'Finalizado', 'Enviado', 'Parcialmente atendido', 'Recebido', 'Cancelado')
IDENTITY = ('name', 'concentration', 'dosage_form', 'presentation')
META = ('item_type', 'volume', 'controlled', 'control_class', 'review_required')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value or '')).casefold() if not unicodedata.combining(c))


def identity(item):
    return tuple(re.sub(r'\s+', '', normalize(item.get(k, ''))) for k in IDENTITY)


def integer(value, label, minimum=0, maximum=2_000_000_000):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)) or not minimum <= int(value) <= maximum:
        raise ApiError(400, f'{label}: informe um número inteiro entre {minimum} e {maximum}.')
    return int(value)


def text(value, label, limit=1000, required=False):
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ApiError(400, f'Confira {label} (até {limit} caracteres).')
    return value.strip()


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(medicines)')}
    additions = {'item_type': "TEXT NOT NULL DEFAULT 'MEDICAMENTO'", 'volume': "TEXT NOT NULL DEFAULT ''",
                 'controlled': 'INTEGER NOT NULL DEFAULT 0', 'control_class': "TEXT NOT NULL DEFAULT ''",
                 'review_required': 'INTEGER NOT NULL DEFAULT 0', 'catalog_source': "TEXT NOT NULL DEFAULT ''",
                 'catalog_key': "TEXT NOT NULL DEFAULT ''", 'original_description': "TEXT NOT NULL DEFAULT ''"}
    for key, declaration in additions.items():
        if key not in columns:
            db.execute(f'ALTER TABLE medicines ADD COLUMN {key} {declaration}')
    statements = [
        '''CREATE TABLE IF NOT EXISTS catalog_sources(organization_id INTEGER NOT NULL REFERENCES organizations(id),
           catalog_source TEXT NOT NULL,catalog_key TEXT NOT NULL,medicine_id INTEGER NOT NULL REFERENCES medicines(id),
           original_json TEXT NOT NULL,imported_at TEXT NOT NULL,PRIMARY KEY(organization_id,catalog_source,catalog_key))''',
        '''CREATE TABLE IF NOT EXISTS monthly_needs(organization_id INTEGER NOT NULL REFERENCES organizations(id),
           unit_id INTEGER NOT NULL REFERENCES units(id),medicine_id INTEGER NOT NULL REFERENCES medicines(id),
           quantity INTEGER NOT NULL CHECK(quantity>=0),updated_at TEXT NOT NULL,updated_by INTEGER NOT NULL REFERENCES users(id),
           PRIMARY KEY(organization_id,unit_id,medicine_id))''',
        '''CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,number TEXT NOT NULL UNIQUE,
           organization_id INTEGER NOT NULL REFERENCES organizations(id),unit_id INTEGER NOT NULL REFERENCES units(id),
           unit_name TEXT NOT NULL,month INTEGER NOT NULL,year INTEGER NOT NULL,responsible TEXT NOT NULL,item_type TEXT NOT NULL,
           status TEXT NOT NULL DEFAULT 'Rascunho',created_by INTEGER NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1)''',
        '''CREATE TABLE IF NOT EXISTS order_lines(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL REFERENCES orders(id),
           medicine_id INTEGER NOT NULL REFERENCES medicines(id),item_json TEXT NOT NULL,monthly_need INTEGER NOT NULL CHECK(monthly_need>=0),
           stock_snapshot INTEGER NOT NULL CHECK(stock_snapshot>=0),suggestion INTEGER NOT NULL CHECK(suggestion>=0),
           requested INTEGER NOT NULL CHECK(requested>=0),released INTEGER NOT NULL DEFAULT 0 CHECK(released>=0),
           received INTEGER NOT NULL DEFAULT 0 CHECK(received>=0),UNIQUE(order_id,medicine_id))''',
        '''CREATE TABLE IF NOT EXISTS order_events(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL REFERENCES orders(id),
           actor_id INTEGER NOT NULL REFERENCES users(id),actor_name TEXT NOT NULL,action TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS order_receipts(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL REFERENCES orders(id),
           line_id INTEGER NOT NULL REFERENCES order_lines(id),request_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,
           quantity INTEGER NOT NULL CHECK(quantity>0),lot_id INTEGER NOT NULL REFERENCES lots(id),
           movement_id INTEGER NOT NULL UNIQUE REFERENCES movements(id),created_by INTEGER NOT NULL REFERENCES users(id),created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS idx_orders_scope ON orders(organization_id,unit_id,year,month)',
        'CREATE INDEX IF NOT EXISTS idx_order_receipts ON order_receipts(order_id,line_id)'
    ]
    for sql in statements:
        db.execute(sql)


def validate_catalog_item(raw):
    if not isinstance(raw, dict):
        raise ValueError('Item não é um objeto.')
    item = dict(raw)
    for key, limit in {'name':180,'concentration':100,'dosage_form':120,'presentation':100,'volume':100,
                       'category':180,'control_class':80,'catalog_key':500,'catalog_source':180,'original_description':2000}.items():
        item[key] = text(item.get(key, ''), key, limit, key in ('name','catalog_key','catalog_source','original_description'))
    item['original_description']=raw['original_description']
    if 'stock_unit' in item:item['stock_unit']=text(item['stock_unit'],'stock_unit',40,True)
    if item.get('item_type') not in TYPES:
        raise ValueError('Tipo de item inválido.')
    for key in ('controlled','review_required','active'):
        if not isinstance(item.get(key), bool):
            raise ValueError(f'{key} deve ser booleano.')
    return item


def read_source(path):
    source=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(source,dict) or not isinstance(source.get('items'),list):
        raise ValueError('O JSON deve conter uma lista items.')
    return source


def import_items(db, source, organization_id):
    """Caller owns transaction. Invalid rows are reported; database errors abort everything."""
    if not db.execute('SELECT 1 FROM organizations WHERE id=? AND active=1',(organization_id,)).fetchone():
        raise ValueError('Organização inexistente ou inativa.')
    summary={'analyzed':len(source['items']),'inserted':0,'linked':0,'existing':0,'review_required':0,'invalid':0,'issues':[],'possible_duplicates':[],'actions':[]}
    candidates=[dict(r) for r in db.execute('SELECT * FROM medicines WHERE organization_id=?',(organization_id,))]
    for index,raw in enumerate(source['items'],1):
        try:
            item=validate_catalog_item(raw)
        except (ValueError,ApiError) as error:
            summary['invalid']+=1;summary['issues'].append({'index':index,'catalog_key':raw.get('catalog_key') if isinstance(raw,dict) else None,'error':getattr(error,'message',str(error))});continue
        summary['review_required']+=int(item['review_required'])
        linked=db.execute('SELECT medicine_id,original_json FROM catalog_sources WHERE organization_id=? AND catalog_source=? AND catalog_key=?',
                          (organization_id,item['catalog_source'],item['catalog_key'])).fetchone()
        if linked:
            original=json.loads(linked['original_json'])
            if identity(original)!=identity(item) or original['item_type']!=item['item_type']:
                summary['invalid']+=1;summary['issues'].append({'index':index,'catalog_key':item['catalog_key'],'error':'Chave já importada com outra identidade. Revise a fonte; registro anterior preservado.'});continue
            summary['existing']+=1;continue
        matches=[r for r in candidates if identity(r)==identity(item) and r['item_type']==item['item_type']]
        if len(matches)>1:
            summary['invalid']+=1;summary['issues'].append({'index':index,'catalog_key':item['catalog_key'],'error':'Vários cadastros compatíveis; vinculação requer revisão.','ids':[r['id'] for r in matches]});continue
        if matches:
            mid=matches[0]['id'];summary['linked']+=1;action='linked'
            # Only source metadata is filled. Existing identity, active flag and stock unit stay intact.
            db.execute('UPDATE medicines SET volume=?,controlled=?,control_class=?,review_required=?,category=?,catalog_source=?,catalog_key=?,original_description=? WHERE id=? AND organization_id=?',
                       tuple(item[k] for k in ('volume','controlled','control_class','review_required','category','catalog_source','catalog_key','original_description'))+(mid,organization_id))
        else:
            possible=[r['id'] for r in candidates if identity(r)[:2]==identity(item)[:2] and r['item_type']==item['item_type']]
            if possible:summary['possible_duplicates'].append({'index':index,'catalog_key':item['catalog_key'],'existing_ids':possible,'reason':'Nome e concentração iguais, forma ou apresentação diferente; cadastros mantidos separados.'})
            stock_unit=item.get('stock_unit') or (item['dosage_form'] if normalize(item['dosage_form']) in ('comprimido','capsula') else 'Unidade')
            keys=(*IDENTITY,'category',*META,'catalog_source','catalog_key','original_description','active')
            values=[item[k] for k in keys]
            mid=db.execute(f"INSERT INTO medicines({','.join(keys)},stock_unit,unit,code,created_at,organization_id) VALUES({','.join('?' for _ in range(len(keys)+5))})",
                           values+[stock_unit,stock_unit,'CAT-'+secrets.token_hex(8).upper(),now(),organization_id]).lastrowid
            candidates.append({**item,'id':mid});summary['inserted']+=1;action='inserted'
        db.execute('INSERT INTO catalog_sources VALUES(?,?,?,?,?,?)',(organization_id,item['catalog_source'],item['catalog_key'],mid,json.dumps(raw,ensure_ascii=False),now()))
        summary['actions'].append({'index':index,'catalog_key':item['catalog_key'],'medicine_id':mid,'action':action})
    return summary


def duplicate(db, organization_id, payload, exclude=None):
    for row in db.execute('SELECT * FROM medicines WHERE organization_id=?',(organization_id,)):
        if row['id']!=exclude and row['item_type']==payload.get('item_type','MEDICAMENTO') and identity(dict(row))==identity(payload):
            raise ApiError(409,f'Já existe um item com nome, concentração, forma e apresentação iguais (código {row["code"]}). Revise o catálogo antes de cadastrar.')


def update_metadata(db, organization_id, mid, payload):
    row=dict(db.execute('SELECT * FROM medicines WHERE id=? AND organization_id=?',(mid,organization_id)).fetchone())
    merged={**row,**{k:payload[k] for k in (*META,'category') if k in payload}}
    if merged['item_type'] not in TYPES:raise ApiError(400,'Tipo de item inválido.')
    for key in ('controlled','review_required'):
        if merged[key] not in (True,False,0,1):raise ApiError(400,f'Confira {key}.')
    db.execute('UPDATE medicines SET item_type=?,volume=?,controlled=?,control_class=?,review_required=?,category=? WHERE id=? AND organization_id=?',
               (merged['item_type'],text(merged['volume'],'volume',100),int(merged['controlled']),text(merged['control_class'],'classe',80),int(merged['review_required']),text(merged['category'],'categoria',180),mid,organization_id))


def reserve_number(db, directory, year):
    if getattr(db, 'dialect', '') == 'postgresql':
        return f"PED-{year}-{db.next_number('order_number_sequence'):06d}"
    # External ledger survives restoration, just like the existing lot-code ledger.
    ledger=sqlite3.connect(Path(directory)/'order_numbers.sqlite3',timeout=10)
    try:
        ledger.execute('CREATE TABLE IF NOT EXISTS numbers(id INTEGER PRIMARY KEY AUTOINCREMENT,number TEXT UNIQUE)')
        for row in db.execute('SELECT number FROM orders'):
            ledger.execute('INSERT OR IGNORE INTO numbers(number) VALUES(?)',(row[0],))
        while True:
            sequence=ledger.execute('INSERT INTO numbers(number) VALUES(NULL)').lastrowid
            number=f'PED-{year}-{sequence:06d}'
            if not ledger.execute('SELECT 1 FROM numbers WHERE number=?',(number,)).fetchone():
                ledger.execute('UPDATE numbers SET number=? WHERE id=?',(number,sequence));ledger.commit();return number
    finally:ledger.close()


class Service:
    def __init__(self,handler,db,user,unit,directory):
        self.h,self.db,self.user,self.unit,self.directory=handler,db,user,unit,directory
        self.org=user['organization_id']
        if not self.org:raise ApiError(403,'Selecione uma organização.')

    def manager(self,admin=False):
        allowed=('Administrador','Administrador da Organização','SuperAdmin','Enfermeiro') if admin else ('Administrador','Administrador da Organização','SuperAdmin','Enfermeiro','Funcionário da Farmácia')
        if self.user['role'] not in allowed:raise ApiError(403,'Seu perfil não pode realizar esta operação.')

    def catalog(self,query=None):
        query=query or {};q=normalize(query.get('q',[''])[0]);kind=query.get('item_type',[''])[0];category=query.get('category',[''])[0]
        rows=[dict(r) for r in self.db.execute('''SELECT m.*,COALESCE(n.quantity,0) AS monthly_need,
            COALESCE((SELECT SUM(l.quantity) FROM lots l WHERE l.medicine_id=m.id AND l.unit_id=? AND l.organization_id=m.organization_id),0) AS stock
            FROM medicines m LEFT JOIN monthly_needs n ON n.medicine_id=m.id AND n.unit_id=? AND n.organization_id=m.organization_id
            WHERE m.organization_id=? AND m.active=1 ORDER BY m.name,m.concentration,m.id''',(self.unit,self.unit,self.org))]
        return {'items':[r for r in rows if (not q or all(term in normalize(' '.join(str(r[k]) for k in (*IDENTITY,'original_description','volume'))) for term in q.split())) and (not kind or r['item_type']==kind) and (not category or r['category']==category)],
                'categories':sorted({r['category'] for r in rows})}

    def load(self,oid):
        order=self.db.execute('SELECT * FROM orders WHERE id=? AND organization_id=? AND unit_id=?',(integer(oid,'Pedido',1),self.org,self.unit)).fetchone()
        if not order:raise ApiError(404,'Pedido não encontrado nesta unidade.')
        return dict(order)

    def detail(self,oid):
        order=self.load(oid)
        lines=[]
        for row in self.db.execute('SELECT * FROM order_lines WHERE order_id=? ORDER BY id',(order['id'],)):
            line=dict(row);line['item']=json.loads(line.pop('item_json'));line['balance']=line['requested']-line['received'];line['unreleased']=line['requested']-line['released'];lines.append(line)
        return {**order,'lines':lines,'events':[dict(r) for r in self.db.execute('SELECT * FROM order_events WHERE order_id=? ORDER BY id',(order['id'],))],
                'receipts':[dict(r) for r in self.db.execute('SELECT r.*,l.lot_number,l.expiration_date,l.internal_code,m.document,m.notes FROM order_receipts r JOIN lots l ON l.id=r.lot_id JOIN movements m ON m.id=r.movement_id WHERE r.order_id=? ORDER BY r.id',(order['id'],))]}

    def event(self,oid,action,details):
        name=self.db.execute('SELECT full_name FROM users WHERE id=?',(self.user['id'],)).fetchone()[0]
        self.db.execute('INSERT INTO order_events(order_id,actor_id,actor_name,action,details,created_at) VALUES(?,?,?,?,?,?)',
                        (oid,self.user['id'],name,action,json.dumps(details,ensure_ascii=False),now()))

    def get(self,path,query):
        if path=='/api/catalog':return self.catalog(query)
        if path=='/api/orders':
            rows=[dict(r) for r in self.db.execute('SELECT * FROM orders WHERE organization_id=? AND unit_id=? ORDER BY id DESC',(self.org,self.unit))]
            for key in ('status','month','year','responsible'):
                value=query.get(key,[''])[0]
                if value:rows=[r for r in rows if (normalize(value) in normalize(r[key]) if key=='responsible' else str(r[key])==value)]
            q=normalize(query.get('q',[''])[0])
            if q:rows=[r for r in rows if any(q in normalize(x[0]) for x in self.db.execute('SELECT item_json FROM order_lines WHERE order_id=?',(r['id'],)))]
            return {'items':rows}
        if path.startswith('/api/orders/'):return self.detail(path.rsplit('/',1)[1])
        raise ApiError(404,'Rota não encontrada.')

    def post(self,path,payload):
        self.manager()
        if path=='/api/monthly-needs':
            self.manager(True);self.db.execute('BEGIN IMMEDIATE')
            entries=payload.get('items')
            if not isinstance(entries,list) or len(entries)>5000:raise ApiError(400,'Lista de necessidades inválida.')
            for item in entries:
                mid=integer(item.get('medicine_id'),'Item',1);qty=integer(item.get('quantity'),'Necessidade mensal')
                if not self.db.execute('SELECT 1 FROM medicines WHERE id=? AND organization_id=? AND active=1',(mid,self.org)).fetchone():raise ApiError(404,'Item não encontrado.')
                self.db.execute('INSERT INTO monthly_needs VALUES(?,?,?,?,?,?) ON CONFLICT(organization_id,unit_id,medicine_id) DO UPDATE SET quantity=excluded.quantity,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                                (self.org,self.unit,mid,qty,now(),self.user['id']))
            self.db.commit();return {'ok':True}
        if path=='/api/orders':return self.create(payload)
        if not path.startswith('/api/orders/'):raise ApiError(404,'Rota não encontrada.')
        self.db.execute('BEGIN IMMEDIATE');order=self.load(path.rsplit('/',1)[1]);oid=order['id'];action=payload.get('action')
        if action=='receive':
            key=text(payload.get('request_id'),'identificador do recebimento',100,True)
            prior=self.db.execute('SELECT * FROM order_receipts WHERE request_id=?',(key,)).fetchone()
            if prior:
                if prior['order_id']!=oid or prior['payload']!=json.dumps(payload,sort_keys=True,ensure_ascii=False):raise ApiError(409,'Identificador já utilizado em outro recebimento.')
                self.db.rollback();return {**self.detail(oid),'label_code':self.db.execute('SELECT internal_code FROM lots WHERE id=?',(prior['lot_id'],)).fetchone()[0]}
        if integer(payload.get('version'),'Versão',1)!=order['version']:raise ApiError(409,'O pedido foi alterado por outro usuário. Reabra antes de salvar.')
        result={}
        if action=='save':
            if order['status']!='Rascunho':raise ApiError(409,'Apenas rascunhos podem ser editados.')
            self.update_lines(oid,payload,'requested')
        elif action=='finalize':
            if order['status']!='Rascunho':raise ApiError(409,'Pedido não está em rascunho.')
            self.update_lines(oid,payload,'requested')
            if not self.db.execute('SELECT 1 FROM order_lines WHERE order_id=? AND requested>0',(oid,)).fetchone():raise ApiError(400,'Informe ao menos uma quantidade a pedir.')
            order['status']='Finalizado'
        elif action=='send':
            if order['status']!='Finalizado':raise ApiError(409,'Finalize o pedido antes de marcar como enviado.')
            order['status']='Enviado'
        elif action=='release':
            self.manager(True)
            if order['status'] not in ('Finalizado','Enviado','Parcialmente atendido'):raise ApiError(409,'Pedido não permite liberação.')
            self.update_lines(oid,payload,'released')
            order['status']=self.fulfillment(oid)
        elif action=='receive':
            if order['status'] not in ('Enviado','Parcialmente atendido'):raise ApiError(409,'Registre a liberação antes do recebimento.')
            line=self.db.execute('SELECT * FROM order_lines WHERE id=? AND order_id=?',(integer(payload.get('line_id'),'Item',1),oid)).fetchone()
            if not line:raise ApiError(404,'Item não pertence ao pedido.')
            qty=integer(payload.get('quantity'),'Quantidade recebida',1)
            if qty>line['released']-line['received']:raise ApiError(409,'Recebimento maior que o saldo liberado. Confira a liberação da CAF.')
            entry=self.h.create_entry(self.db,self.user,self.unit,{**payload,'medicine_id':line['medicine_id'],'source':'CAF / '+order['number'],'responsible':self.user['full_name'],'reason':'Recebimento de pedido '+order['number']},commit=False)
            self.db.execute('INSERT INTO order_receipts(order_id,line_id,request_id,payload,quantity,lot_id,movement_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (oid,line['id'],key,json.dumps(payload,sort_keys=True,ensure_ascii=False),qty,entry['lot_id'],entry['movement_id'],self.user['id'],now()))
            self.db.execute('UPDATE order_lines SET received=received+? WHERE id=?',(qty,line['id']))
            order['status']=self.fulfillment(oid);result['label_code']=entry['internal_code']
        elif action=='cancel':
            self.manager(True)
            if order['status'] in ('Recebido','Cancelado'):raise ApiError(409,'Pedido já encerrado.')
            text(payload.get('reason'),'justificativa do cancelamento',1000,True);order['status']='Cancelado'
        else:raise ApiError(400,'Ação inválida.')
        self.db.execute('UPDATE orders SET status=?,version=version+1,updated_at=? WHERE id=?',(order['status'],now(),oid))
        self.event(oid,action,payload);self.db.commit();return {**self.detail(oid),**result}

    def update_lines(self,oid,payload,field):
        entries=payload.get('lines')
        if not isinstance(entries,list) or len(entries)>5000:raise ApiError(400,'Linhas do pedido inválidas.')
        seen=set()
        for item in entries:
            lid=integer(item.get('id'),'Item',1)
            if lid in seen:raise ApiError(400,'Item repetido no pedido.')
            seen.add(lid);row=self.db.execute('SELECT * FROM order_lines WHERE id=? AND order_id=?',(lid,oid)).fetchone()
            if not row:raise ApiError(404,'Item não pertence ao pedido.')
            qty=integer(item.get(field),'Quantidade')
            if field=='released' and not row['received']<=qty<=row['requested']:raise ApiError(400,'Liberado deve ficar entre o recebido e o solicitado.')
            self.db.execute(f'UPDATE order_lines SET {field}=? WHERE id=?',(qty,lid))

    def fulfillment(self,oid):
        lines=self.db.execute('SELECT requested,released,received FROM order_lines WHERE order_id=? AND requested>0',(oid,)).fetchall()
        if lines and all(r['received']==r['requested'] for r in lines):return 'Recebido'
        if any(r['received']>0 or r['released']<r['requested'] for r in lines):return 'Parcialmente atendido'
        return 'Enviado'

    def create(self,payload):
        if 'unit_id' in payload and integer(payload['unit_id'],'Unidade',1)!=self.unit:raise ApiError(403,'Selecione a unidade do pedido no cabeçalho.')
        kind=payload.get('item_type');year=integer(payload.get('year'),'Ano',2000,2100);month=integer(payload.get('month'),'Mês',1,12)
        if kind not in (*TYPES,'TODOS'):raise ApiError(400,'Tipo do pedido inválido.')
        if kind=='TODOS':self.manager(True)
        responsible=text(payload.get('responsible'),'responsável',180,True)
        self.db.execute('BEGIN IMMEDIATE')
        unit=self.db.execute('SELECT name FROM units WHERE id=? AND organization_id=?',(self.unit,self.org)).fetchone()
        if not unit:raise ApiError(404,'Unidade não encontrada.')
        number=reserve_number(self.db,self.directory,year)
        oid=self.db.execute('INSERT INTO orders(number,organization_id,unit_id,unit_name,month,year,responsible,item_type,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (number,self.org,self.unit,unit[0],month,year,responsible,kind,self.user['id'],now(),now())).lastrowid
        rows=self.catalog({'item_type':['' if kind=='TODOS' else kind]})['items']
        for item in rows:
            need,stock=item['monthly_need'],item['stock'];suggestion=max(0,need-stock)
            snapshot={k:item[k] for k in (*IDENTITY,*META,'stock_unit','category','original_description','catalog_key','catalog_source')}
            self.db.execute('INSERT INTO order_lines(order_id,medicine_id,item_json,monthly_need,stock_snapshot,suggestion,requested) VALUES(?,?,?,?,?,?,?)',
                           (oid,item['id'],json.dumps(snapshot,ensure_ascii=False),need,stock,suggestion,suggestion))
        if not rows:raise ApiError(400,'Nenhum item ativo para este tipo de pedido.')
        self.event(oid,'created',{'number':number,'items':len(rows)});self.db.commit();return self.detail(oid)

    def pdf(self,oid):
        from pdf_reports import render_pdf
        order=self.detail(oid)
        rows=[[description(l['item']),l['monthly_need'],l['stock_snapshot'],l['suggestion'],l['requested'],l['released'],l['received'],l['balance']] for l in order['lines'] if l['requested']>0]
        return render_pdf(order['number'],order['unit_name'],[(f"{order['month']:02d}/{order['year']} · {order['responsible']} · {order['status']} · Criado em {order['created_at']}",
            ['Item','Necessidade','Estoque na criação','Sugestão','Pedido','Liberado','Recebido','Saldo'],rows)])


def description(item):
    return ' — '.join(str(item.get(k,'')) for k in (*IDENTITY,'volume') if item.get(k))
