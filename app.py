"""Fooladi Abaya — local SQLite or Cloudflare D1/R2. Money is integer fils."""
import csv
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
import warnings
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash
from cloud_storage import CloudBackendError, ConcurrentWrite, D1Connection, R2Photos, WorkerSessionInterface

ROOT = Path(__file__).resolve().parent
STAGES = [('received', 'استلام الطلب', 'Received'), ('cutting', 'القص', 'Cutting'), ('sewing', 'الخياطة', 'Sewing'), ('finishing', 'التطريز والتشطيب', 'Finishing'), ('quality', 'فحص الجودة', 'Quality check'), ('ready', 'جاهزة للتسليم', 'Ready')]
MEASURES = [('length', 'طول العباية', 'Abaya length'), ('shoulder', 'عرض الكتف', 'Shoulder width'), ('bust', 'محيط الصدر', 'Bust circumference'), ('waist', 'محيط الخصر', 'Waist circumference'), ('hip', 'محيط الأرداف', 'Hip circumference'), ('sleeve', 'طول الكم', 'Sleeve length'), ('arm', 'محيط الذراع', 'Arm circumference'), ('cuff', 'محيط فتحة الكم', 'Cuff circumference')]
METHODS = [('cash', 'نقداً', 'Cash'), ('card', 'بطاقة', 'Card'), ('transfer', 'تحويل بنكي', 'Bank transfer'), ('cod', 'تحصيل شركة التوصيل', 'Courier collection')]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', measurements TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS customer_phone ON customers(phone);
CREATE TABLE IF NOT EXISTS models (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE, title TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0), photo TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL REFERENCES customers(id), customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, customer_address TEXT NOT NULL, created_at TEXT NOT NULL, due_date TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('pickup','delivery')), delivery_address TEXT NOT NULL DEFAULT '', courier TEXT NOT NULL DEFAULT '', tracking TEXT NOT NULL DEFAULT '', shipping_status TEXT NOT NULL DEFAULT 'pending' CHECK(shipping_status IN ('pending','out','delivered')), delivered_at TEXT, discount INTEGER NOT NULL CHECK(discount>=0), delivery_fee INTEGER NOT NULL CHECK(delivery_fee>=0), total INTEGER NOT NULL CHECK(total>=0), notes TEXT NOT NULL DEFAULT '', cancelled INTEGER NOT NULL DEFAULT 0, cancel_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), model_id INTEGER NOT NULL REFERENCES models(id), model_code TEXT NOT NULL, model_title TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100), price INTEGER NOT NULL CHECK(price>=0), kind TEXT NOT NULL CHECK(kind IN ('custom','stock')), stage TEXT NOT NULL CHECK(stage IN ('received','cutting','sewing','finishing','quality','ready')), measurements TEXT NOT NULL, tailor TEXT NOT NULL DEFAULT '', fabric TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS item_order ON items(order_id);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), request_key TEXT NOT NULL UNIQUE, amount INTEGER NOT NULL CHECK(amount<>0), method TEXT NOT NULL CHECK(method IN ('cash','card','transfer','cod')), reference TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS payment_order ON payments(order_id);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), item_id INTEGER REFERENCES items(id), kind TEXT NOT NULL, detail TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, stamp REAL NOT NULL);
"""

class ValidationError(Exception):
    pass

def now():
    return datetime.now(ZoneInfo('Asia/Dubai')).isoformat(timespec='seconds')

def today():
    return now()[:10]

def money(value, *, positive=False):
    try:
        val = Decimal(str(value or '0'))
        if not val.is_finite() or val < 0 or val > Decimal('10000000'):
            raise ValueError()
        cents = int((val * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if positive and cents <= 0:
            raise ValueError()
        return cents
    except (ValueError, InvalidOperation, OverflowError):
        raise ValidationError('أدخل مبلغاً صحيحاً ضمن النطاق المسموح. / Enter a valid amount.')

def text_input(form, name, limit=2000, required=False):
    value = str(form.get(name, '')).strip()
    if len(value) > limit or (required and not value):
        raise ValidationError('تحقق من الحقول المطلوبة وطول النص. / Check required fields and text length.')
    return value

def read_measures(form, prefix=''):
    unit = form.get(prefix+'unit', 'inch')
    if unit not in ('inch', 'cm'):
        raise ValidationError('اختر وحدة القياس. / Choose a measurement unit.')
    result = {'unit': unit}
    for key, _, _ in MEASURES:
        raw = form.get(prefix+key, '')
        if str(raw).strip():
            try:
                number = Decimal(str(raw))
                if not number.is_finite() or not 0 < number <= 400:
                    raise ValueError()
                result[key] = str(number)
            except (ValueError, InvalidOperation):
                raise ValidationError('المقاسات يجب أن تكون أرقاماً موجبة حتى 400. / Measurements must be between 0 and 400.')
    return result

def create_app(data_dir=None, test_config=None, cloud=False):
    app = Flask(__name__)
    data = None
    signing_key = None
    if cloud:
        app.session_interface = WorkerSessionInterface()
    else:
        data = Path(data_dir or os.environ.get('FOOLADI_DATA_DIR', ROOT/'data')).resolve()
        data.mkdir(parents=True, exist_ok=True)
        (data/'uploads').mkdir(exist_ok=True)
        secret = data/'secret.key'
        try:
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        signing_key = secret.read_text().strip()
    app.config.update(SECRET_KEY=signing_key, CLOUD=cloud, DATA_DIR=data, MAX_CONTENT_LENGTH=10*1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=cloud, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    if test_config:
        app.config.update(test_config)

    def db():
        if 'db' not in g:
            if cloud:
                g.db = D1Connection(request.environ['workers.env'].DB)
            else:
                g.db = sqlite3.connect(data/'fooladi.sqlite3', timeout=30)
                g.db.row_factory = sqlite3.Row
                g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(_error=None):
        conn = g.pop('db', None)
        if conn:
            conn.close()

    if not cloud:
        with app.app_context():
            db().execute('PRAGMA journal_mode=WAL')
            db().executescript(SCHEMA)
            db().commit()

    @contextmanager
    def transaction():
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        try:
            g.pop('settings_cache',None)
            if cloud and session.get('user') and session.get('auth_version')!=setting('auth_version'):
                abort(403)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def setting(key, default=''):
        if 'settings_cache' not in g:
            g.settings_cache={row['key']:row['value'] for row in db().execute('SELECT key,value FROM settings')}
        return g.settings_cache.get(key,default)

    def put_setting(key, value):
        db().execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        if 'settings_cache' in g:g.settings_cache[key]=value

    def t(ar, en):
        return en if session.get('lang') == 'en' else ar

    def label(code):
        entries = STAGES + METHODS + [('pickup','استلام من المحل','Store pickup'),('delivery','توصيل','Delivery'),('pending','بانتظار التسليم','Awaiting handover'),('out','خرجت للتوصيل','Out for delivery'),('delivered','تم التسليم','Delivered'),('cancelled','ملغي','Cancelled'),('custom','تفصيل','Made to measure'),('stock','جاهز','Ready to wear')]
        return next((t(a, e) for c, a, e in entries if c == code), code)

    def event(order_id, kind, detail, item_id=None):
        db().execute('INSERT INTO events(order_id,item_id,kind,detail,actor,created_at) VALUES (?,?,?,?,?,?)', (order_id,item_id,kind,detail,session.get('user','system'),now()))

    def decorate_order(row,items,paid):
        order=dict(row)
        order['items']=items
        order['paid']=paid
        order['balance'] = 0 if order['cancelled'] else order['total'] - order['paid']
        order['refund_due'] = order['paid'] if order['cancelled'] else 0
        order['ready'] = bool(items) and all(x['stage']=='ready' for x in items)
        if order['cancelled']:
            order['status']='cancelled'
        elif order['shipping_status']!='pending':
            order['status']=order['shipping_status']
        else:
            ranks={c:i for i,(c,_,_) in enumerate(STAGES)}
            order['status']=min((x['stage'] for x in items),key=lambda c:ranks[c],default='received')
        order['overdue'] = order['due_date'] < today() and order['status'] not in ('delivered','cancelled')
        return order

    def get_order(oid):
        row = db().execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        if not row:
            abort(404)
        items = [dict(x) for x in db().execute('SELECT * FROM items WHERE order_id=? ORDER BY id',(oid,))]
        paid = db().execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE order_id=?',(oid,)).fetchone()[0]
        return decorate_order(row,items,paid)

    def all_orders():
        rows=db().execute('SELECT o.*,COALESCE(p.paid,0) AS paid FROM orders o LEFT JOIN (SELECT order_id,SUM(amount) AS paid FROM payments GROUP BY order_id) p ON p.order_id=o.id ORDER BY o.id DESC').fetchall()
        grouped={r['id']:[] for r in rows}
        for item in db().execute('SELECT * FROM items ORDER BY id'):
            if item['order_id'] in grouped:grouped[item['order_id']].append(dict(item))
        return [decorate_order(row,grouped[row['id']],row['paid']) for row in rows]

    @app.before_request
    def security():
        if cloud and str(getattr(request.environ['workers.env'],'MAINTENANCE','0'))=='1':
            return Response('Maintenance / صيانة مؤقتة',503,content_type='text/plain; charset=utf-8')
        session.setdefault('csrf', secrets.token_hex(24))
        if request.method == 'POST' and not secrets.compare_digest(str(request.form.get('csrf','')),session['csrf']):
            abort(400, 'انتهت صلاحية النموذج، حدّث الصفحة. / Refresh the page and try again.')
        if request.endpoint in ('static','language'):
            return
        configured=bool(setting('password_hash'))
        if not configured and request.endpoint != 'setup':
            return redirect(url_for('setup'))
        if configured and request.endpoint == 'setup':
            return redirect(url_for('dashboard'))
        if configured and request.endpoint != 'login':
            if not session.get('user') or session.get('auth_version')!=setting('auth_version'):
                session.pop('user',None)
                session.pop('auth_version',None)
                return redirect(url_for('login'))

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' blob: data:; script-src 'self'; style-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        if request.endpoint!='static':
            response.headers['Cache-Control']='no-store'
        return response

    @app.context_processor
    def context():
        return dict(t=t,label=label,lang=session.get('lang','ar'),stages=STAGES,measures=MEASURES,methods=METHODS,csrf=session.get('csrf'),new_key=lambda:uuid.uuid4().hex,shop=setting('shop_name','فولادي للعباية'),shop_phone=setting('shop_phone'),shop_address=setting('shop_address'),footer=setting('invoice_footer'),today=today(),json_loads=json.loads,cloud=cloud)

    app.jinja_env.filters['money']=lambda v:f'{v/100:,.2f}'
    app.jinja_env.filters['date']=lambda v:(v or '').replace('T',' ')[:16]
    app.jinja_env.filters['order_no']=lambda v:f'FL-{v:05d}'

    @app.errorhandler(ValidationError)
    def invalid(error):
        return render_template('error.html',message=str(error)),400

    @app.errorhandler(ConcurrentWrite)
    def concurrent_write(_error):
        # No writes from the failed batch were committed. Preserve form state via Back.
        g.pop('settings_cache',None)
        return render_template('error.html',message=t('تغيرت البيانات أثناء الحفظ من جلسة أخرى. لم يُحفظ هذا التعديل؛ ارجع وحدّث البيانات ثم أعد المحاولة.','Another session updated the records. This change was not saved; go back, refresh the data and retry.')),409

    @app.errorhandler(CloudBackendError)
    def cloud_failure(_error):
        # Avoid template context (it itself needs the database), and do not expose SQL.
        return Response('Storage temporarily unavailable / تعذر الوصول إلى التخزين مؤقتاً',503,content_type='text/plain; charset=utf-8')

    @app.errorhandler(413)
    def too_big(error):
        return render_template('error.html',message=t('حجم الملف كبير. الحد الأقصى 10 ميجابايت.','File too large. Maximum 10 MB.')),413

    @app.errorhandler(400)
    @app.errorhandler(404)
    def http_error(error):
        return render_template('error.html',message=error.description),error.code

    @app.route('/language/<lang>',methods=['POST'])
    def language(lang):
        if lang in ('ar','en'):
            session['lang']=lang
        target=request.form.get('next','/')
        return redirect(target if target.startswith('/') and not target.startswith('//') and '\\' not in target else '/')

    @app.route('/setup',methods=['GET','POST'])
    def setup():
        if request.method=='POST':
            if cloud:
                expected=str(getattr(request.environ['workers.env'],'SETUP_TOKEN',''))
                if len(expected)<32 or not secrets.compare_digest(expected,request.form.get('setup_token','')):
                    raise ValidationError('رمز تهيئة المالك غير صحيح. / Invalid owner setup token.')
            username=text_input(request.form,'username',80,True)
            password=text_input(request.form,'password',256,True)
            if len(password)<10 or password!=request.form.get('confirm'):
                raise ValidationError('كلمة المرور 10 أحرف على الأقل، ويجب تطابق التأكيد. / Use 10+ characters and matching confirmation.')
            with transaction():
                if setting('password_hash'):
                    abort(409)
                put_setting('username',username)
                put_setting('password_hash',generate_password_hash(password,method='pbkdf2:sha256:600000' if cloud else 'scrypt'))
                put_setting('auth_version',uuid.uuid4().hex)
                put_setting('shop_name',text_input(request.form,'shop_name',120,True))
            session['user']=username
            session['auth_version']=setting('auth_version')
            session.permanent=True
            return redirect(url_for('dashboard'))
        return render_template('auth.html',setup=True)

    @app.route('/login',methods=['GET','POST'])
    def login():
        if request.method=='POST':
            ip=(request.headers.get('CF-Connecting-IP') if cloud else request.remote_addr) or 'local'
            with transaction():
                attempts=db().execute('SELECT COUNT(*) FROM login_attempts WHERE ip=? AND stamp>=?',(ip,time.time()-900)).fetchone()[0]
                if attempts>=10:
                    raise ValidationError('محاولات كثيرة. حاول بعد 15 دقيقة. / Too many attempts. Try in 15 minutes.')
                correct=check_password_hash(setting('password_hash'),request.form.get('password','')) and request.form.get('username')==setting('username')
                db().execute('DELETE FROM login_attempts WHERE stamp<?',(time.time()-900,))
                if not correct:
                    db().execute('INSERT INTO login_attempts VALUES (?,?)',(ip,time.time()))
                else:
                    db().execute('DELETE FROM login_attempts WHERE ip=?',(ip,))
            if correct:
                lang=session.get('lang','ar')
                session.clear()
                session.update(user=setting('username'),lang=lang,csrf=secrets.token_hex(24),auth_version=setting('auth_version'))
                session.permanent=True
                return redirect(url_for('dashboard'))
            flash(t('بيانات الدخول غير صحيحة.','Incorrect login details.'),'error')
        return render_template('auth.html',setup=False)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.get('/')
    def dashboard():
        orders=all_orders()
        active=[o for o in orders if o['status'] not in ('delivered','cancelled')]
        stats=dict(active=len(active),ready=sum(o['ready'] and o['shipping_status']=='pending' for o in active),late=sum(o['overdue'] for o in active),balance=sum(o['balance'] for o in orders),collected=sum(o['paid'] for o in orders),refund_due=sum(o['refund_due'] for o in orders))
        return render_template('dashboard.html',orders=sorted(active,key=lambda o:o['due_date'])[:8],stats=stats,counts={c:sum(o['status']==c for o in active) for c,_,_ in STAGES})

    @app.route('/customers',methods=['GET','POST'])
    def customers():
        if request.method=='POST':
            values=(text_input(request.form,'name',120,True),text_input(request.form,'phone',40,True),text_input(request.form,'address'),text_input(request.form,'notes'),json.dumps(read_measures(request.form),ensure_ascii=False))
            with transaction():
                cid=request.form.get('id')
                if cid:
                    db().execute('UPDATE customers SET name=?,phone=?,address=?,notes=?,measurements=? WHERE id=?',values+(cid,))
                else:
                    db().execute('INSERT INTO customers(name,phone,address,notes,measurements,created_at) VALUES (?,?,?,?,?,?)',values+(now(),))
            flash(t('تم حفظ بيانات العميل.','Customer saved.'),'success')
            return redirect(url_for('customers'))
        q=request.args.get('q','').strip()
        rows=db().execute('SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY id DESC',('%'+q+'%','%'+q+'%')).fetchall()
        edit=db().execute('SELECT * FROM customers WHERE id=?',(request.args.get('edit'),)).fetchone()
        return render_template('customers.html',customers=rows,edit=edit,q=q,measurement=json.loads(edit['measurements']) if edit else {})

    def save_photo(file):
        if not file or not file.filename:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(file.stream) as original:
                    if original.format not in ('PNG','JPEG','WEBP'):
                        raise ValueError()
                    if original.width*original.height>25000000:
                        raise ValueError()
                    photo=ImageOps.exif_transpose(original).convert('RGB')
                    photo.thumbnail((1800,1800))
                    filename=uuid.uuid4().hex+'.jpg'
                    if cloud:
                        payload=io.BytesIO();photo.save(payload,format='JPEG',quality=88)
                        R2Photos(request.environ['workers.env'].PHOTOS).put(filename,payload.getvalue())
                    else:
                        photo.save(data/'uploads'/filename,quality=88)
                    return filename
        except (UnidentifiedImageError,ValueError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):
            raise ValidationError('ارفع صورة JPG أو PNG أو WEBP صالحة حتى 25 ميجابكسل. / Upload a valid JPG, PNG or WEBP, up to 25 megapixels.')

    @app.route('/models',methods=['GET','POST'])
    def models():
        if request.method=='POST':
            code=text_input(request.form,'code',60,True)
            title=text_input(request.form,'title',160,True)
            price=money(request.form.get('price'))
            description=text_input(request.form,'description')
            photo=save_photo(request.files.get('photo'))
            try:
                with transaction():
                    mid=request.form.get('id')
                    if mid:
                        old=db().execute('SELECT * FROM models WHERE id=?',(mid,)).fetchone()
                        if not old: abort(404)
                        db().execute('UPDATE models SET code=?,title=?,price=?,photo=?,description=?,active=? WHERE id=?',(code,title,price,photo or old['photo'],description,1 if request.form.get('active') else 0,mid))
                    else:
                        db().execute('INSERT INTO models(code,title,price,photo,description) VALUES (?,?,?,?,?)',(code,title,price,photo or '',description))
            except sqlite3.IntegrityError:
                raise ValidationError('رقم الموديل موجود مسبقاً. / Model code already exists.')
            flash(t('تم حفظ الموديل.','Model saved.'),'success')
            return redirect(url_for('models'))
        edit=db().execute('SELECT * FROM models WHERE id=?',(request.args.get('edit'),)).fetchone()
        return render_template('models.html',models=db().execute('SELECT * FROM models ORDER BY id DESC').fetchall(),edit=edit)

    @app.get('/photos/<name>')
    def photo(name):
        if cloud:
            payload=R2Photos(request.environ['workers.env'].PHOTOS).get(name)
            if payload is None:abort(404)
            return Response(payload,mimetype='image/jpeg')
        return send_from_directory(data/'uploads',name)

    @app.get('/api/model')
    def model_lookup():
        row=db().execute('SELECT * FROM models WHERE code=? AND active=1',(request.args.get('code',''),)).fetchone()
        if not row:
            return jsonify(error='not_found'),404
        result=dict(row)
        result['photo_url']=url_for('photo',name=row['photo']) if row['photo'] else ''
        return jsonify(result)

    @app.get('/orders')
    def orders():
        q=request.args.get('q','').strip().lower()
        state=request.args.get('status','')
        payment=request.args.get('payment','')
        result=all_orders()
        if q:
            result=[o for o in result if q in ' '.join([f"FL-{o['id']:05d}",o['customer_name'],o['customer_phone']]+[x['model_code'] for x in o['items']]).lower()]
        if state=='late':result=[o for o in result if o['overdue']]
        elif state:result=[o for o in result if o['status']==state]
        if payment=='unpaid':result=[o for o in result if o['balance']>0]
        if payment=='paid':result=[o for o in result if o['balance']==0 and not o['cancelled']]
        return render_template('orders.html',orders=result,q=q,state=state,payment=payment)

    @app.route('/orders/new',methods=['GET','POST'])
    def new_order():
        if request.method=='POST':
            with transaction():
                token=text_input(request.form,'request_key',80,True)
                existing=db().execute('SELECT id FROM orders WHERE request_key=?',(token,)).fetchone()
                if existing:return redirect(url_for('order_detail',oid=existing['id']))
                customer=db().execute('SELECT * FROM customers WHERE id=?',(request.form.get('customer_id'),)).fetchone()
                if not customer:raise ValidationError('اختر العميل أو أضفه أولاً. / Choose or create a customer first.')
                try:due=datetime.strptime(request.form.get('due_date',''),'%Y-%m-%d').date().isoformat()
                except ValueError:raise ValidationError('أدخل موعد تسليم صحيحاً. / Enter a valid due date.')
                mode=request.form.get('mode')
                if mode not in ('pickup','delivery'):abort(400)
                address=text_input(request.form,'delivery_address')
                if mode=='delivery' and not address:raise ValidationError('عنوان التوصيل مطلوب. / Delivery address is required.')
                try:count=int(request.form.get('item_count','0'))
                except ValueError:abort(400)
                if not 1<=count<=30:raise ValidationError('أضف من 1 إلى 30 بنداً. / Add 1 to 30 items.')
                lines=[]
                catalogue={m['code'].lower():m for m in db().execute('SELECT * FROM models WHERE active=1')}
                for i in range(count):
                    p=f'i{i}_'
                    model=catalogue.get(request.form.get(p+'code','').strip().lower())
                    if not model:raise ValidationError('رقم الموديل غير موجود أو غير نشط. / Model not found or inactive.')
                    try:qty=int(request.form.get(p+'quantity','1'))
                    except ValueError:raise ValidationError('الكمية غير صحيحة. / Invalid quantity.')
                    if not 1<=qty<=100:raise ValidationError('الكمية من 1 إلى 100. / Quantity must be 1 to 100.')
                    kind=request.form.get(p+'kind')
                    if kind not in ('custom','stock'):abort(400)
                    measures=read_measures(request.form,p)
                    if kind=='custom' and not any(k in measures for k,_,_ in MEASURES):
                        raise ValidationError('أدخل مقاساً واحداً على الأقل لكل عباية تفصيل. / Enter measurements for made-to-measure items.')
                    lines.append((model,qty,money(request.form.get(p+'price')),kind,measures,text_input(request.form,p+'tailor',120),text_input(request.form,p+'fabric',120),text_input(request.form,p+'color',80),text_input(request.form,p+'notes')))
                subtotal=sum(qty*price for _,qty,price,*_ in lines)
                discount=money(request.form.get('discount'))
                if discount>subtotal:raise ValidationError('الخصم يتجاوز قيمة العبايات. / Discount exceeds item subtotal.')
                delivery_fee=money(request.form.get('delivery_fee')) if mode=='delivery' else 0
                total=subtotal-discount+delivery_fee
                deposit=money(request.form.get('deposit'))
                if deposit>total:raise ValidationError('الدفعة أكبر من قيمة الطلب. / Payment exceeds order total.')
                method=request.form.get('method')
                if method not in {c for c,_,_ in METHODS}:abort(400)
                cur=db().execute('INSERT INTO orders(request_key,customer_id,customer_name,customer_phone,customer_address,created_at,due_date,mode,delivery_address,discount,delivery_fee,total,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(token,customer['id'],customer['name'],customer['phone'],customer['address'],now(),due,mode,address,discount,delivery_fee,total,text_input(request.form,'notes')))
                oid=cur.lastrowid
                for model,qty,price,kind,measurement,tailor,fabric,color,notes in lines:
                    db().execute('INSERT INTO items(order_id,model_id,model_code,model_title,photo,quantity,price,kind,stage,measurements,tailor,fabric,color,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,model['id'],model['code'],model['title'],model['photo'],qty,price,kind,'ready' if kind=='stock' else 'received',json.dumps(measurement,ensure_ascii=False),tailor,fabric,color,notes))
                if deposit:
                    db().execute('INSERT INTO payments(order_id,request_key,amount,method,note,created_at) VALUES (?,?,?,?,?,?)',(oid,token+'-deposit',deposit,method,'دفعة أولى / Initial payment',now()))
                event(oid,'created','إنشاء الطلب / Order created')
            return redirect(url_for('order_detail',oid=oid))
        customer_rows=[dict(x) for x in db().execute('SELECT * FROM customers ORDER BY name')]
        return render_template('new_order.html',customers=customer_rows,models=db().execute('SELECT * FROM models WHERE active=1 ORDER BY code').fetchall())

    @app.get('/orders/<int:oid>')
    def order_detail(oid):
        return render_template('order_detail.html',order=get_order(oid),payments=db().execute('SELECT * FROM payments WHERE order_id=? ORDER BY id DESC',(oid,)).fetchall(),events=db().execute('SELECT * FROM events WHERE order_id=? ORDER BY id DESC',(oid,)).fetchall())

    @app.post('/orders/<int:oid>/payment')
    def payment(oid):
        with transaction():
            order=get_order(oid)
            token=text_input(request.form,'request_key',80,True)
            existing=db().execute('SELECT order_id FROM payments WHERE request_key=?',(token,)).fetchone()
            if existing:
                if existing['order_id']!=oid:abort(409)
                return redirect(url_for('order_detail',oid=oid))
            amount=money(request.form.get('amount'),positive=True)
            refund=request.form.get('type')=='refund'
            if refund:
                if amount>order['paid']:raise ValidationError('الاسترداد يتجاوز صافي المدفوع. / Refund exceeds net payments.')
                amount=-amount
            elif order['cancelled'] or amount>order['balance']:
                raise ValidationError('الدفعة تتجاوز المتبقي أو أن الطلب ملغي. / Payment exceeds balance or order is cancelled.')
            method=request.form.get('method')
            if method not in {c for c,_,_ in METHODS}:abort(400)
            note=text_input(request.form,'note',1000,refund)
            db().execute('INSERT INTO payments(order_id,request_key,amount,method,reference,note,created_at) VALUES (?,?,?,?,?,?,?)',(oid,token,amount,method,text_input(request.form,'reference',120),note,now()))
            event(oid,'payment',f"{'استرداد / Refund' if refund else 'دفعة / Payment'}: {abs(amount)/100:.2f} AED")
        return redirect(url_for('order_detail',oid=oid))

    @app.post('/orders/<int:oid>/items/<int:iid>')
    def update_item(oid,iid):
        with transaction():
            order=get_order(oid)
            if order['cancelled'] or order['shipping_status']!='pending':raise ValidationError('لا يمكن تعديل الإنتاج بعد خروج الطلب أو إلغائه. / Production cannot be edited after dispatch or cancellation.')
            item=next((x for x in order['items'] if x['id']==iid),None)
            if not item:abort(404)
            stage=request.form.get('stage')
            valid=[x[0] for x in STAGES]
            if stage not in valid:abort(400)
            old_index=valid.index(item['stage']);new_index=valid.index(stage)
            if item['kind']=='custom' and new_index>old_index+1:raise ValidationError('انتقل إلى المرحلة التالية بالتسلسل. / Advance one production stage at a time.')
            reason=text_input(request.form,'reason',1000)
            if new_index<old_index and not reason:raise ValidationError('اذكر سبب إعادة العباية إلى مرحلة سابقة. / Give a reason for moving back.')
            measures=read_measures(request.form)
            if item['kind']=='custom' and not any(k in measures for k,_,_ in MEASURES):raise ValidationError('لا يمكن حذف جميع مقاسات التفصيل. / Keep made-to-measure measurements.')
            db().execute('UPDATE items SET stage=?,tailor=?,fabric=?,color=?,notes=?,measurements=? WHERE id=?',(stage,text_input(request.form,'tailor',120),text_input(request.form,'fabric',120),text_input(request.form,'color',80),text_input(request.form,'notes'),json.dumps(measures,ensure_ascii=False),iid))
            detail=f"{item['model_code']}: {item['stage']} → {stage}. {reason}"
            old_measures=json.loads(item['measurements'])
            if old_measures!=measures:
                detail+=' | المقاسات / Measurements: '+json.dumps(old_measures,ensure_ascii=False)+' → '+json.dumps(measures,ensure_ascii=False)
            event(oid,'production',detail,iid)
        return redirect(url_for('order_detail',oid=oid))

    @app.post('/orders/<int:oid>/delivery')
    def delivery(oid):
        with transaction():
            order=get_order(oid)
            if order['cancelled'] or order['shipping_status']=='delivered':raise ValidationError('الطلب مغلق. / Order is closed.')
            target=request.form.get('shipping_status')
            if target not in ('pending','out','delivered'):abort(400)
            mode=request.form.get('mode')
            if mode not in ('pickup','delivery'):abort(400)
            if mode!=order['mode'] and (order['delivery_fee'] or order['shipping_status']!='pending'):
                raise ValidationError('لا يمكن تغيير طريقة التسليم لطلب برسوم توصيل أو بعد خروجه. / Mode cannot change for a charged or dispatched delivery.')
            address=text_input(request.form,'delivery_address')
            courier=text_input(request.form,'courier',120)
            if mode=='delivery' and not address:raise ValidationError('عنوان التوصيل مطلوب. / Delivery address is required.')
            if target!='pending' and not order['ready']:raise ValidationError('يجب تجهيز جميع العبايات أولاً. / All items must be ready first.')
            if target=='out' and (mode!='delivery' or not courier):raise ValidationError('أدخل اسم شركة التوصيل واختر التوصيل. / Choose delivery and enter courier name.')
            if order['shipping_status']=='out' and target=='pending' and not request.form.get('return_reason'):
                raise ValidationError('اذكر سبب إعادة الطلب من التوصيل. / Enter the delivery return reason.')
            if target=='delivered' and order['balance']>0 and not request.form.get('credit_confirm'):
                raise ValidationError('يوجد مبلغ متبقٍ؛ سجل الدفع أو أكد التسليم مع بقاء المبلغ مستحقاً. / Record payment or confirm handover with an outstanding balance.')
            try:due=datetime.strptime(request.form.get('due_date',''),'%Y-%m-%d').date().isoformat()
            except ValueError:raise ValidationError('موعد التسليم غير صحيح. / Invalid due date.')
            db().execute('UPDATE orders SET mode=?,delivery_address=?,courier=?,tracking=?,shipping_status=?,delivered_at=?,due_date=?,notes=? WHERE id=?',(mode,address,courier,text_input(request.form,'tracking',200),target,now() if target=='delivered' else None,due,text_input(request.form,'notes'),oid))
            event(oid,'delivery',f"{order['shipping_status']} → {target}; {courier}; {text_input(request.form,'return_reason',1000)}; due {order['due_date']} → {due}")
        return redirect(url_for('order_detail',oid=oid))

    @app.post('/orders/<int:oid>/cancel')
    def cancel(oid):
        with transaction():
            order=get_order(oid)
            if order['shipping_status']!='pending' or order['cancelled']:raise ValidationError('الإلغاء متاح قبل التسليم أو الخروج للتوصيل فقط. / Cancel only before dispatch or handover.')
            reason=text_input(request.form,'reason',1000,True)
            db().execute('UPDATE orders SET cancelled=1,cancel_reason=? WHERE id=?',(reason,oid))
            event(oid,'cancelled',reason)
        return redirect(url_for('order_detail',oid=oid))

    @app.get('/orders/<int:oid>/invoice')
    def invoice(oid):
        return render_template('invoice.html',order=get_order(oid),payments=db().execute('SELECT * FROM payments WHERE order_id=? ORDER BY id',(oid,)).fetchall(),workshop=request.args.get('workshop')=='1')

    @app.get('/workshop')
    def workshop():
        orders=[o for o in all_orders() if o['status'] not in ('delivered','cancelled','out')]
        return render_template('workshop.html',orders=orders)

    @app.route('/settings',methods=['GET','POST'])
    def settings():
        if request.method=='POST':
            with transaction():
                for key in ('shop_name','shop_phone','shop_address','invoice_footer'):
                    put_setting(key,text_input(request.form,key,2000,key=='shop_name'))
            flash(t('تم حفظ الإعدادات.','Settings saved.'),'success')
            return redirect(url_for('settings'))
        return render_template('settings.html')

    @app.post('/settings/password')
    def password():
        new=text_input(request.form,'password',256,True)
        if len(new)<10 or new!=request.form.get('confirm'):raise ValidationError('استخدم 10 أحرف على الأقل وتأكيداً مطابقاً. / Use 10+ characters and matching confirmation.')
        with transaction():
            if not check_password_hash(setting('password_hash'),request.form.get('current','')):raise ValidationError('كلمة المرور الحالية غير صحيحة. / Incorrect current password.')
            put_setting('password_hash',generate_password_hash(new,method='pbkdf2:sha256:600000' if cloud else 'scrypt'))
            put_setting('auth_version',uuid.uuid4().hex)
        session.clear()
        return redirect(url_for('login'))

    @app.get('/export/orders.csv')
    def export():
        out=io.StringIO(newline='')
        writer=csv.writer(out)
        writer.writerow(['Order','Customer','Phone','Due date','Status','Delivery','Total AED','Paid AED','Outstanding AED','Refund due AED'])
        def safe(val):
            s=str(val)
            return "'"+s if s[:1] in '=+-@\t\r\n' else s
        for o in all_orders():
            writer.writerow([safe(x) for x in [f"FL-{o['id']:05d}",o['customer_name'],o['customer_phone'],o['due_date'],label(o['status']),label(o['mode']),f"{o['total']/100:.2f}",f"{o['paid']/100:.2f}",f"{o['balance']/100:.2f}",f"{o['refund_due']/100:.2f}"]])
        return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name=f'fooladi-orders-{today()}.csv')

    @app.get('/backup')
    def backup():
        if cloud:
            from cloud_storage import build_backup
            archive=build_backup(db(),R2Photos(request.environ['workers.env'].PHOTOS))
            return send_file(io.BytesIO(archive),mimetype='application/zip',as_attachment=True,download_name=f'fooladi-cloud-backup-{today()}.zip')
        # SQLite backup API makes a consistent snapshot even when WAL is active.
        snapshot=data/(uuid.uuid4().hex+'.backup')
        try:
            dest=sqlite3.connect(snapshot)
            try:db().backup(dest)
            finally:dest.close()
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
                z.write(snapshot,'data/fooladi.sqlite3')
                for f in (data/'uploads').glob('*.jpg'):z.write(f,'data/uploads/'+f.name)
                z.writestr('RESTORE.txt','Stop Fooladi first. Keep a copy of your current data folder. Extract data from this backup into the application folder. Keep your current secret.key, or a new one will be created. Restart the application. This archive contains private customer data.\n')
            out.seek(0)
            return send_file(out,mimetype='application/zip',as_attachment=True,download_name=f'fooladi-backup-{today()}.zip')
        finally:
            snapshot.unlink(missing_ok=True)

    return app

if __name__=='__main__':
    import argparse
    import threading
    import webbrowser
    from waitress import serve
    parser=argparse.ArgumentParser(description='Fooladi Abaya management')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=5050)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    application=create_app()
    address=f'http://127.0.0.1:{args.port}'
    print(f'Fooladi Abaya: {address}\nPress Ctrl+C to stop. Data: {application.config["DATA_DIR"]}',flush=True)
    if not args.no_browser:threading.Timer(1.0,lambda:webbrowser.open(address)).start()
    serve(application,host=args.host,port=args.port,threads=4)
