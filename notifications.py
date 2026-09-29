"""Persistent condition notifications, scoped to authorized units and per-user reads."""
from datetime import date, datetime, timedelta, timezone
import json
from catalog_orders import ApiError, integer


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,organization_id INTEGER NOT NULL REFERENCES organizations(id),
        unit_id INTEGER NOT NULL REFERENCES units(id),event_key TEXT NOT NULL,kind TEXT NOT NULL,
        title TEXT NOT NULL,description TEXT NOT NULL,priority INTEGER NOT NULL,level TEXT NOT NULL,
        event_at TEXT NOT NULL,detected_at TEXT NOT NULL,status TEXT NOT NULL,target_page TEXT NOT NULL,
        target_id INTEGER NOT NULL,signature TEXT NOT NULL,resolved_at TEXT)''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_active
        ON notifications(organization_id,unit_id,event_key) WHERE resolved_at IS NULL''')
    db.execute('''CREATE TABLE IF NOT EXISTS notification_reads(
        notification_id INTEGER NOT NULL REFERENCES notifications(id),user_id INTEGER NOT NULL REFERENCES users(id),
        signature TEXT NOT NULL,read_at TEXT NOT NULL,PRIMARY KEY(notification_id,user_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_notifications_unit ON notifications(organization_id,unit_id,resolved_at)')


def conditions(db, org, unit):
    result=[]
    def add(key,kind,title,description,priority,level,status,page,target,event_at=None):
        result.append(dict(event_key=key,kind=kind,title=title,description=description,priority=priority,level=level,
                           status=status,target_page=page,target_id=target,event_at=event_at or now()))
    for r in db.execute('''SELECT t.*,m.name,m.concentration,m.stock_unit,o.name AS origin_name,d.name AS destination_name
        FROM transfers t JOIN medicines m ON m.id=t.medicine_id JOIN units o ON o.id=t.origin_unit_id
        JOIN units d ON d.id=t.destination_unit_id WHERE t.organization_id=? AND
        (t.origin_unit_id=? OR t.destination_unit_id=?) AND t.status IN ('Pendente de Recebimento','Divergência','Recusada')''',(org,unit,unit)):
        status=r['status'];incoming=r['destination_unit_id']==unit
        if status=='Pendente de Recebimento' and not incoming:continue
        title={'Pendente de Recebimento':'Transferência pendente','Divergência':'Divergência de recebimento','Recusada':'Transferência recusada'}[status]
        action='Confirme o recebimento físico.' if status=='Pendente de Recebimento' else 'Confira o histórico e a ação disponível para seu perfil.'
        event_at=r['discrepancy_reported_at'] if status=='Divergência' else r['refused_at'] if status=='Recusada' else r['created_at']
        add(f'transfer:{r["id"]}:{status}','transfer',title,
            f'{r["origin_name"]} enviou {r["quantity_sent"]} {r["stock_unit"]} de {r["name"]} {r["concentration"]} para {r["destination_name"]}. {action}',
            1 if status=='Pendente de Recebimento' else 0,'Atenção' if status=='Pendente de Recebimento' else 'Crítico',status,'transfers',r['id'],event_at)
    today=date.today()
    for r in db.execute('''SELECT l.*,m.name,m.concentration FROM lots l JOIN medicines m ON m.id=l.medicine_id
        WHERE l.organization_id=? AND l.unit_id=? AND m.organization_id=? AND m.active=1 AND l.quantity>0 AND l.expiration_date<=?''',
        (org,unit,org,(today+timedelta(days=90)).isoformat())):
        days=(date.fromisoformat(r['expiration_date'])-today).days;expired=days<0
        tier='expired' if expired else 'near'
        add(f'lot:{r["id"]}:{tier}','expiry','Medicamento vencido' if expired else 'Medicamento próximo do vencimento',
            f'{r["name"]} {r["concentration"]} — lote {r["lot_number"]} '+(f'está vencido há {abs(days)} dia(s).' if expired else f'vence em {days} dia(s).'),
            2 if expired else 3,'Crítico' if expired else 'Atenção','Vencido' if expired else 'Próximo do vencimento','lots',r['id'])
    for r in db.execute('''SELECT m.*,COALESCE(SUM(l.quantity),0) AS stock FROM medicines m
        LEFT JOIN lots l ON l.medicine_id=m.id AND l.unit_id=? AND l.organization_id=m.organization_id
        WHERE m.organization_id=? AND m.active=1 AND (m.minimum_stock>0 OR l.id IS NOT NULL OR EXISTS(
        SELECT 1 FROM monthly_needs n WHERE n.organization_id=m.organization_id AND n.unit_id=? AND n.medicine_id=m.id AND n.quantity>0))
        GROUP BY m.id HAVING stock<=m.minimum_stock''',(unit,org,unit)):
        zero=r['stock']==0
        add(f'stock:{r["id"]}:{"zero" if zero else "minimum"}','stock','Estoque zerado' if zero else 'Estoque mínimo atingido',
            f'{r["name"]} {r["concentration"]} possui {r["stock"]} {r["stock_unit"]} nesta unidade. Mínimo cadastrado: {r["minimum_stock"]}.',
            4 if zero else 5,'Atenção','Sem estoque' if zero else 'No mínimo ou abaixo','lots',r['id'])
    return result


def allowed_units(db,user):
    org=user['organization_id']
    if user['role'] in ('Administrador','Administrador da Organização','SuperAdmin'):
        return [r[0] for r in db.execute('SELECT id FROM units WHERE organization_id=?',(org,))]
    return [r[0] for r in db.execute('''SELECT u.id FROM units u JOIN user_units uu ON uu.unit_id=u.id AND uu.organization_id=u.organization_id
        WHERE uu.user_id=? AND u.organization_id=?''',(user['id'],org))]


def sync(db,user):
    units=allowed_units(db,user);org=user['organization_id']
    for unit in units:
        current=conditions(db,org,unit);keys=set()
        for item in current:
            keys.add(item['event_key'])
            # Day countdown changes do not repeatedly mark an expiry notification unread.
            signature=json.dumps([item['status'],item['description'] if item['kind']=='stock' else item['event_key']],ensure_ascii=False)
            prior=db.execute('SELECT * FROM notifications WHERE organization_id=? AND unit_id=? AND event_key=? AND resolved_at IS NULL',(org,unit,item['event_key'])).fetchone()
            if prior:
                db.execute('UPDATE notifications SET description=?,signature=?,event_at=? WHERE id=?',
                           (item['description'],signature,now() if signature!=prior['signature'] else prior['event_at'],prior['id']))
            else:
                keys_list=tuple(item)
                db.execute(f'INSERT INTO notifications(organization_id,unit_id,{",".join(keys_list)},detected_at,signature) VALUES({",".join("?" for _ in range(len(keys_list)+4))})',
                           (org,unit,*item.values(),now(),signature))
        for row in db.execute('SELECT id,event_key FROM notifications WHERE organization_id=? AND unit_id=? AND resolved_at IS NULL',(org,unit)):
            if row['event_key'] not in keys:db.execute('UPDATE notifications SET resolved_at=? WHERE id=?',(now(),row['id']))
    db.commit()
    return units


def list_notifications(db,user):
    units=sync(db,user)
    if not units:return {'items':[],'unread':0,'pending':0}
    rows=[dict(r) for r in db.execute(f'''SELECT n.*,u.name AS unit_name,
        CASE WHEN r.signature=n.signature THEN r.read_at ELSE NULL END AS read_at
        FROM notifications n JOIN units u ON u.id=n.unit_id LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=?
        WHERE n.organization_id=? AND n.unit_id IN ({','.join('?' for _ in units)})
        AND (n.resolved_at IS NULL OR n.resolved_at>=?)
        ORDER BY (n.resolved_at IS NOT NULL),n.priority,n.event_at DESC,n.id DESC''',
        (user['id'],user['organization_id'],*units,(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()))]
    for row in rows:
        row.pop('signature');row.pop('event_key')
    return {'items':rows,'unread':sum(not r['read_at'] and not r['resolved_at'] for r in rows),'pending':sum(not r['resolved_at'] for r in rows)}


def mark_read(db,user,payload):
    units=allowed_units(db,user)
    ids=payload.get('ids')
    # The client submits exactly the visible snapshot, so unseen arrivals are not marked read.
    if not isinstance(ids,list) or len(ids)>10000:raise ApiError(400,'Lista de notificações inválida.')
    ids={integer(i,'Notificação',1) for i in ids}
    db.execute('BEGIN IMMEDIATE')
    try:
        for nid in ids:
            row=db.execute('SELECT * FROM notifications WHERE id=? AND organization_id=?',(nid,user['organization_id'])).fetchone()
            if not row or row['unit_id'] not in units:raise ApiError(404,'Notificação não encontrada.')
            db.execute('INSERT INTO notification_reads VALUES(?,?,?,?) ON CONFLICT(notification_id,user_id) DO UPDATE SET signature=excluded.signature,read_at=excluded.read_at',
                       (nid,user['id'],row['signature'],now()))
        db.commit()
    except Exception:db.rollback();raise
    return {'ok':True}
