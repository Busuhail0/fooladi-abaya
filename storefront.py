"""Public catalogue and unconfirmed enquiries; customer data stays private.

An enquiry neither charges a card nor reserves stock. Staff confirm it explicitly
before it becomes an unpaid order in the existing shop workflow.
"""
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS storefront_products (
 model_id INTEGER PRIMARY KEY REFERENCES models(id),
 published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
 custom_available INTEGER NOT NULL DEFAULT 1 CHECK(custom_available IN (0,1)),
 description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS customer_requests (
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, browser_key TEXT NOT NULL,
 ip_hash TEXT NOT NULL, created_epoch INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','contacted','confirmed','closed')),
 kind TEXT NOT NULL CHECK(kind IN ('custom','stock')),
 model_id INTEGER NOT NULL REFERENCES models(id), stock_id TEXT REFERENCES ready_stock(id),
 model_code TEXT NOT NULL, model_title TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '',
 unit_price INTEGER NOT NULL CHECK(unit_price>=0), quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 10),
 measurements TEXT NOT NULL DEFAULT '{}', size TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
 customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('pickup','delivery')),
 delivery_address TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', admin_note TEXT NOT NULL DEFAULT '',
 order_id INTEGER UNIQUE REFERENCES orders(id),
 CHECK((kind='custom' AND stock_id IS NULL) OR (kind='stock' AND stock_id IS NOT NULL AND quantity=1))
);
CREATE INDEX IF NOT EXISTS customer_requests_created ON customer_requests(created_epoch);
CREATE INDEX IF NOT EXISTS customer_requests_rate ON customer_requests(ip_hash,created_epoch);
CREATE INDEX IF NOT EXISTS customer_requests_status ON customer_requests(status,created_at);
CREATE TABLE IF NOT EXISTS storefront_tracking_attempts (
 ip_hash TEXT NOT NULL, lookup_hash TEXT NOT NULL, stamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS storefront_tracking_ip ON storefront_tracking_attempts(ip_hash,stamp);
CREATE INDEX IF NOT EXISTS storefront_tracking_lookup ON storefront_tracking_attempts(lookup_hash,stamp);
"""


def canonical_phone(value):
    """Match UAE local/international forms and Arabic digits, never suffixes."""
    number = str(value).translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
    number = re.sub(r'[\s()\-\u200e\u200f\u061c]', '', number)
    if number.startswith('00'): number = '+' + number[2:]
    if not re.fullmatch(r'\+?[0-9]{7,15}', number): return ''
    number = number.lstrip('+')
    if re.fullmatch(r'05[0-9]{8}', number): number = '971' + number[1:]
    return number


def register_storefront(app, *, db, transaction, setting, put_setting, t, now, today,
                        text_input, read_measures, money, event, ValidationError, photo,
                        get_order, stages):
    store = Blueprint('store', __name__, url_prefix='/shop')
    states = [('new','جديد','New'), ('contacted','تم التواصل','Contacted'),
              ('confirmed','مؤكد','Confirmed'), ('closed','مغلق','Closed')]

    def public_model(mid):
        row = db().execute('''SELECT m.id,m.code,m.title,m.price,m.photo,p.description,p.custom_available
            FROM models m JOIN storefront_products p ON p.model_id=m.id
            WHERE m.id=? AND m.active=1 AND p.published=1''', (mid,)).fetchone()
        if not row: abort(404)
        return dict(row)

    def selection(kind, identifier):
        if kind == 'custom':
            model = public_model(identifier)
            if not model['custom_available']: abort(404)
            return dict(model, model_id=model['id'], stock_id=None, size='', color='')
        row = db().execute("SELECT * FROM ready_stock WHERE id=? AND state='available'", (identifier,)).fetchone()
        if not row: abort(404)
        model = public_model(row['model_id'])
        return dict(id=row['id'], model_id=row['model_id'], stock_id=row['id'], code=row['model_code'],
                    title=row['model_title'], price=row['price'], photo=row['photo'],
                    size=row['size'], color=row['color'], description=model['description'])

    @store.context_processor
    def store_context():
        return {'store_open': setting('store_open', '1') == '1', 'request_states': states}

    @store.get('')
    @store.get('/')
    def catalogue():
        kind = request.args.get('kind', 'all')
        if kind not in ('all', 'custom', 'stock'): kind = 'all'
        search = request.args.get('q', '').strip()[:120]
        pattern = '%' + search + '%'
        models = db().execute('''SELECT m.id,m.code,m.title,m.price,m.photo,p.description
            FROM models m JOIN storefront_products p ON p.model_id=m.id
            WHERE m.active=1 AND p.published=1 AND p.custom_available=1
            AND (m.title LIKE ? OR m.code LIKE ?) ORDER BY m.id DESC LIMIT 100''', (pattern,pattern)).fetchall()
        stock = db().execute('''SELECT s.id,s.model_code AS code,s.model_title AS title,s.price,s.photo,s.size,s.color
            FROM ready_stock s JOIN models m ON m.id=s.model_id
            JOIN storefront_products p ON p.model_id=m.id WHERE m.active=1 AND p.published=1
            AND s.state='available' AND (s.model_title LIKE ? OR s.model_code LIKE ?)
            ORDER BY s.created_at DESC,s.id LIMIT 100''', (pattern,pattern)).fetchall()
        return render_template('store_catalogue.html', models=models, stock=stock, kind=kind, search=search)

    @store.get('/media/model/<int:mid>')
    def model_photo(mid):
        model = public_model(mid)
        if not model['photo']: abort(404)
        return photo(model['photo'])

    @store.get('/media/ready/<sid>')
    def ready_photo(sid):
        item = selection('stock', sid)
        if not item['photo']: abort(404)
        return photo(item['photo'])

    def tracking_digest(value):
        if app.config['CLOUD']:
            secret = str(getattr(request.environ['workers.env'], 'SESSION_SECRET', ''))
        else:
            secret = app.config['SECRET_KEY']
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def ip_fingerprint():
        address = (request.headers.get('CF-Connecting-IP') if app.config['CLOUD'] else None) or request.remote_addr or 'unknown'
        return tracking_digest(address)

    def tracking_grant(row):
        return tracking_digest('tracking-grant:' + row['id'] + ':' + canonical_phone(row['customer_phone']))

    @store.route('/track', methods=['GET','POST'])
    def track():
        error = ''
        status = 200
        form = request.form if request.method == 'POST' else {}
        if request.method == 'POST':
            reference = str(form.get('reference', ''))[:80].strip().upper()
            if reference.startswith('WEB-'): reference = reference[4:]
            phone_number = canonical_phone(str(form.get('phone', ''))[:80])
            fingerprint = ip_fingerprint()
            lookup_hash = tracking_digest('tracking-lookup:' + reference + ':' + phone_number)
            stamp = int(time.time())
            match = None
            with transaction():
                count = db().execute('SELECT COUNT(*) FROM storefront_tracking_attempts WHERE ip_hash=? AND stamp>?', (fingerprint,stamp-900)).fetchone()[0]
                targeted = db().execute('SELECT COUNT(*) FROM storefront_tracking_attempts WHERE lookup_hash=? AND stamp>?', (lookup_hash,stamp-900)).fetchone()[0]
                if count >= 20 or targeted >= 8:
                    abort(429, t('محاولات كثيرة. انتظر 15 دقيقة ثم حاول مجددًا أو تواصل مع المحل.', 'Too many attempts. Wait 15 minutes or contact the store.'))
                if re.fullmatch(r'[0-9A-F]{8}', reference) and phone_number:
                    # A random reference prefix, never a sequential order ID.
                    # Fail closed if both prefix and phone have a rare collision.
                    rows = db().execute('SELECT id,customer_phone FROM customer_requests WHERE id>=? AND id<?',
                                        (reference.lower(), reference.lower()+'g')).fetchall()
                    matches = [row for row in rows if secrets.compare_digest(canonical_phone(row['customer_phone']), phone_number)]
                    if len(matches) == 1: match = matches[0]
                db().execute('DELETE FROM storefront_tracking_attempts WHERE stamp<=?', (stamp-900,))
                db().execute('INSERT INTO storefront_tracking_attempts(ip_hash,lookup_hash,stamp) VALUES (?,?,?)', (fingerprint,lookup_hash,stamp))
            if match:
                grants = {key:value for key,value in session.get('store_tracking', {}).items() if value['expires'] > stamp}
                grants = dict(list(grants.items())[-4:])
                grants[match['id']] = {'expires':stamp+900, 'proof':tracking_grant(match)}
                session['store_tracking'] = grants
                return redirect(url_for('store.receipt',rid=match['id']),code=303)
            error = t('لم نتمكن من مطابقة رقم الطلب والهاتف. راجعهما أو تواصل مع المحل.', 'We could not match that reference and phone number. Check both or contact the store.')
            status = 400
        return render_template('store_track.html', form=form, error=error), status

    def public_progress(row):
        journey = [('new',t('استلام الطلب','Request received')), ('confirmed',t('تأكيد الطلب','Request confirmed'))]
        if row['kind'] == 'custom': journey += [(code,t(ar,en)) for code,ar,en in stages if code != 'received']
        else: journey += [('ready',t('جاهزة للتسليم','Ready for handover'))]
        code = row['status']
        due_date = ''
        mode = row['mode']
        if row['order_id']:
            # Reuse staff status logic, but pass only safe fields to the template.
            order = get_order(row['order_id'])
            code = 'confirmed' if order['status'] == 'received' else order['status']
            due_date = order['due_date']
            mode = order['mode']
        if mode == 'delivery': journey += [('out',t('خرجت للتوصيل','Out for delivery'))]
        journey += [('delivered',t('تم التسليم','Delivered'))]
        if code == 'contacted':
            current = 0
            title = t('تم التواصل — بانتظار التأكيد','Contacted — awaiting confirmation')
        elif code in ('closed','cancelled','returned'):
            current = -1
            title = {'closed':t('الطلب مغلق','Request closed'), 'cancelled':t('الطلب ملغي','Order cancelled'), 'returned':t('تم إرجاع الطلب','Order returned')}[code]
            journey = []
        else:
            current = next((i for i,(key,_) in enumerate(journey) if key == code),0)
            title = journey[current][1]
        return dict(code=code,title=title,due_date=due_date,
                    steps=[dict(title=label,state='done' if i<current or code=='delivered' else 'current' if i==current else 'next') for i,(_,label) in enumerate(journey)])

    def detail(kind, identifier):
        item = selection(kind, identifier)
        form = request.form if request.method == 'POST' else {}
        errors = []
        status = 200
        stamp = int(time.time())
        browser_key = session.setdefault('store_browser', secrets.token_hex(24))
        valid_keys = {k:v for k,v in session.get('store_forms', {}).items() if stamp-v < 7200}
        if request.method == 'GET':
            form_key = uuid.uuid4().hex
            valid_keys = dict(list(valid_keys.items())[-7:])
            valid_keys[form_key] = stamp
            session['store_forms'] = valid_keys
        else:
            form_key = form.get('request_key', '')
            try:
                if form_key not in valid_keys:
                    raise ValidationError(t('انتهت صلاحية الصفحة. حدّثها ثم أرسل الطلب.', 'This page expired. Refresh it before submitting.'))
                # A duplicate submission returns the same private receipt.
                old = db().execute('SELECT id,browser_key FROM customer_requests WHERE request_key=?', (form_key,)).fetchone()
                if old:
                    if not secrets.compare_digest(old['browser_key'], browser_key): abort(403)
                    return redirect(url_for('store.receipt', rid=old['id']), code=303)
                if setting('store_open', '1') != '1':
                    raise ValidationError(t('استقبال الطلبات متوقف مؤقتًا. تواصل مع المحل.', 'Requests are temporarily paused. Please contact the store.'))
                if form.get('website', ''): abort(400)
                name = text_input(form, 'name', 120, True)
                phone_number = text_input(form, 'phone', 40, True)
                normalized = re.sub(r'[\s()\-]', '', phone_number)
                if not re.fullmatch(r'\+?[0-9]{7,15}', normalized):
                    raise ValidationError(t('أدخل رقم هاتف صحيحًا مع رمز الدولة عند الحاجة.', 'Enter a valid phone number, including the country code if needed.'))
                mode = form.get('mode', 'pickup')
                if mode not in ('pickup','delivery'): abort(400)
                address = text_input(form, 'address', 600, mode == 'delivery') if mode == 'delivery' else ''
                notes = text_input(form, 'notes', 1200)
                color = text_input(form, 'color', 80) if kind == 'custom' else item['color']
                try: quantity = int(form.get('quantity','1')) if kind == 'custom' else 1
                except ValueError: raise ValidationError(t('تحقق من الكمية.', 'Check the quantity.'))
                if not 1 <= quantity <= 10: raise ValidationError(t('الكمية من 1 إلى 10.', 'Quantity must be between 1 and 10.'))
                measurements = read_measures(form) if kind == 'custom' else {}
                if kind == 'custom' and any(k not in measurements for k in ('length','shoulder','bust','sleeve')):
                    raise ValidationError(t('أدخل طول العباية وعرض الكتف ومحيط الصدر وطول الكم.', 'Enter abaya length, shoulder width, bust circumference and sleeve length.'))
                if form.get('consent') != 'yes':
                    raise ValidationError(t('أكد السماح للمحل بالتواصل معك بخصوص الطلب.', 'Please allow the store to contact you about this request.'))
                fingerprint = ip_fingerprint()
                with transaction():
                    # Recheck stock/publication and duplicates under the revision guard.
                    current = selection(kind, identifier)
                    old = db().execute('SELECT id,browser_key FROM customer_requests WHERE request_key=?', (form_key,)).fetchone()
                    if old:
                        if old['browser_key'] != browser_key: abort(403)
                        return redirect(url_for('store.receipt', rid=old['id']), code=303)
                    count = db().execute('SELECT COUNT(*) FROM customer_requests WHERE ip_hash=? AND created_epoch>?', (fingerprint, stamp-3600)).fetchone()[0]
                    if count >= 10:
                        abort(429, t('وصلت إلى الحد المسموح للطلبات. حاول لاحقًا أو اتصل بالمحل.', 'Request limit reached. Please try later or contact the store.'))
                    if str(current['price']) != form.get('quoted_price'):
                        raise ValidationError(t('تغيّر السعر. راجع السعر المعروض وأعد إرسال الطلب.', 'The price changed. Review the displayed price and submit again.'))
                    rid = uuid.uuid4().hex
                    moment = now()
                    db().execute('''INSERT INTO customer_requests(id,request_key,browser_key,ip_hash,created_epoch,created_at,updated_at,
                        kind,model_id,stock_id,model_code,model_title,photo,unit_price,quantity,measurements,size,color,
                        customer_name,customer_phone,mode,delivery_address,notes)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (rid,form_key,browser_key,fingerprint,stamp,moment,moment,kind,item['model_id'],item['stock_id'],
                         item['code'],item['title'],item['photo'],current['price'],quantity,json.dumps(measurements,ensure_ascii=False),
                         item['size'],color,name,normalized,mode,address,notes))
                return redirect(url_for('store.receipt',rid=rid),code=303)
            except ValidationError as error:
                errors.append(str(error)); status = 400
            except Exception as error:
                from cloud_storage import ConcurrentWrite
                if not isinstance(error, ConcurrentWrite): raise
                errors.append(t('يوجد تحديث آخر الآن. بياناتك ما زالت في النموذج؛ أعد الإرسال.', 'Another update is in progress. Your entries are preserved; please submit again.'))
                status = 409
        return render_template('store_product.html', item=item, kind=kind, form=form,
                               form_key=form_key, errors=errors), status

    @store.route('/custom/<int:mid>', methods=['GET','POST'])
    def custom(mid): return detail('custom',mid)

    @store.route('/ready/<sid>', methods=['GET','POST'])
    def ready(sid): return detail('stock',sid)

    @store.get('/request/<rid>')
    def receipt(rid):
        row = db().execute('SELECT * FROM customer_requests WHERE id=?', (rid,)).fetchone()
        if not row: abort(404)
        owner = secrets.compare_digest(row['browser_key'], session.get('store_browser',''))
        grant = session.get('store_tracking',{}).get(rid)
        if not owner:
            if not grant: abort(404)
            if grant['expires'] <= time.time() or not secrets.compare_digest(grant['proof'],tracking_grant(row)):
                return redirect(url_for('store.track',expired=1))
        enquiry = {key:row[key] for key in ('id','model_title','kind','quantity','unit_price','status')}
        return render_template('store_receipt.html', enquiry=enquiry, progress=public_progress(row))

    app.register_blueprint(store)

    @app.route('/storefront', methods=['GET','POST'])
    def storefront_settings():
        if request.method == 'POST':
            with transaction():
                if request.form.get('action') == 'availability':
                    put_setting('store_open', '1' if request.form.get('store_open') else '0')
                else:
                    mid = request.form.get('model_id')
                    model = db().execute('SELECT id FROM models WHERE id=?', (mid,)).fetchone()
                    if not model: abort(404)
                    description = text_input(request.form,'description',1600)
                    db().execute('''INSERT INTO storefront_products(model_id,published,custom_available,description)
                        VALUES (?,?,?,?) ON CONFLICT(model_id) DO UPDATE SET published=excluded.published,
                        custom_available=excluded.custom_available,description=excluded.description''',
                        (mid,1 if request.form.get('published') else 0,1 if request.form.get('custom_available') else 0,description))
            flash(t('تم حفظ إعدادات صفحة المتعاملين.', 'Customer page settings saved.'),'success')
            return redirect(url_for('storefront_settings'))
        rows = db().execute('''SELECT m.*,COALESCE(p.published,0) AS published,
            COALESCE(p.custom_available,1) AS custom_available,COALESCE(p.description,'') AS public_description
            FROM models m LEFT JOIN storefront_products p ON p.model_id=m.id ORDER BY m.id DESC''').fetchall()
        return render_template('store_admin.html', products=rows, store_open=setting('store_open','1')=='1')

    @app.get('/customer-requests')
    def customer_requests():
        state = request.args.get('status','new')
        if state not in {x[0] for x in states} | {'all'}: state='new'
        rows = db().execute('SELECT * FROM customer_requests WHERE (?=\'all\' OR status=?) ORDER BY created_epoch DESC LIMIT 200', (state,state)).fetchall()
        counts = {r['status']:r['n'] for r in db().execute('SELECT status,COUNT(*) AS n FROM customer_requests GROUP BY status')}
        return render_template('store_requests.html', enquiries=rows, states=states, state=state, counts=counts)

    @app.route('/customer-requests/<rid>', methods=['GET','POST'])
    def customer_request(rid):
        row = db().execute('SELECT * FROM customer_requests WHERE id=?', (rid,)).fetchone()
        if not row: abort(404)
        if request.method == 'POST':
            with transaction():
                row = db().execute('SELECT * FROM customer_requests WHERE id=?', (rid,)).fetchone()
                state = request.form.get('status')
                if row['order_id'] or state not in ('new','contacted','closed'): abort(409)
                db().execute('UPDATE customer_requests SET status=?,admin_note=?,updated_at=? WHERE id=?',
                             (state,text_input(request.form,'admin_note',2000),now(),rid))
            return redirect(url_for('customer_request',rid=rid))
        return render_template('store_request.html', enquiry=dict(row), states=states,
                               measurement=json.loads(row['measurements']))

    @app.post('/customer-requests/<rid>/confirm')
    def confirm_customer_request(rid):
        with transaction():
            row = db().execute('SELECT * FROM customer_requests WHERE id=?', (rid,)).fetchone()
            if not row: abort(404)
            if row['order_id']: return redirect(url_for('order_detail',oid=row['order_id']))
            if row['status']=='closed': raise ValidationError(t('أعد فتح الطلب أولًا.', 'Reopen the request first.'))
            try: due = datetime.strptime(request.form.get('due_date',''),'%Y-%m-%d').date().isoformat()
            except ValueError: raise ValidationError(t('حدد موعد التسليم المتفق عليه.', 'Choose the agreed due date.'))
            price = money(request.form.get('price'))
            fee = money(request.form.get('delivery_fee')) if row['mode']=='delivery' else 0
            if request.form.get('confirmed') != 'yes':
                raise ValidationError(t('أكد الاتفاق مع المتعامل على السعر والمقاسات وموعد التسليم.', 'Confirm the customer agreed to the price, measurements and due date.'))
            model = db().execute('SELECT active FROM models WHERE id=?', (row['model_id'],)).fetchone()
            if not model or not model['active']: raise ValidationError(t('الموديل غير نشط.', 'This model is inactive.'))
            stock = None
            if row['kind']=='stock':
                stock = db().execute('SELECT * FROM ready_stock WHERE id=?', (row['stock_id'],)).fetchone()
                if not stock or stock['state']!='available':
                    raise ValidationError(t('القطعة الجاهزة لم تعد متاحة. تواصل مع المتعامل.', 'This ready piece is no longer available. Contact the customer.'))
            # Reserve an explicit customer ID; cloud revision CAS serializes this
            # with all admin writes. Do not mutate an existing contact by phone.
            cid = db().execute('SELECT COALESCE(MAX(id),0)+1 FROM customers').fetchone()[0]
            moment = now()
            db().execute('INSERT INTO customers(id,name,phone,address,notes,measurements,created_at) VALUES (?,?,?,?,?,?,?)',
                         (cid,row['customer_name'],row['customer_phone'],row['delivery_address'],'طلب الموقع / Website request',row['measurements'],moment))
            cur = db().execute('''INSERT INTO orders(request_key,customer_id,customer_name,customer_phone,customer_address,
                created_at,due_date,mode,delivery_address,discount,delivery_fee,total,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('website-'+rid,cid,row['customer_name'],row['customer_phone'],row['delivery_address'],moment,due,row['mode'],
                 row['delivery_address'],0,fee,price*row['quantity']+fee,row['notes']))
            oid = cur.lastrowid
            db().execute('''INSERT INTO items(order_id,model_id,model_code,model_title,photo,quantity,price,kind,stage,measurements,color,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (oid,row['model_id'],row['model_code'],row['model_title'],row['photo'],row['quantity'],price,row['kind'],
                 'ready' if stock else 'received',row['measurements'],row['color'],row['notes'] + ('\nالمقاس / Size: '+row['size'] if stock else '')))
            if stock:
                db().execute("INSERT INTO ready_sales(order_id,stock_id,state,display_date) VALUES (?,?,'reserved',?)",(oid,stock['id'],stock['display_date']))
                db().execute("UPDATE ready_stock SET state='reserved',current_order_id=? WHERE id=?", (oid,stock['id']))
            db().execute("UPDATE customer_requests SET status='confirmed',order_id=?,updated_at=? WHERE id=?", (oid,moment,rid))
            event(oid,'created','طلب من صفحة المتعاملين / Customer website request: '+rid[:8].upper())
        return redirect(url_for('order_detail',oid=oid))
