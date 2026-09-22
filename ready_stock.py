"""Ready-to-wear inventory and sales, separate from tailoring production."""
import csv
import io
import uuid
from datetime import datetime

from flask import abort, redirect, render_template, request, send_file, url_for

READY_SCHEMA = """
CREATE TABLE IF NOT EXISTS ready_stock (
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
 model_id INTEGER NOT NULL REFERENCES models(id),
 model_code TEXT NOT NULL, model_title TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '',
 display_date TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0),
 size TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','reserved','sold','returned')),
 current_order_id INTEGER UNIQUE REFERENCES orders(id), created_at TEXT NOT NULL,
 CHECK((state='available' AND current_order_id IS NULL) OR (state<>'available' AND current_order_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ready_stock_model ON ready_stock(model_code);
CREATE TABLE IF NOT EXISTS ready_sales (
 order_id INTEGER PRIMARY KEY REFERENCES orders(id), stock_id TEXT NOT NULL REFERENCES ready_stock(id),
 state TEXT NOT NULL CHECK(state IN ('reserved','sold','returned','released')),
 display_date TEXT NOT NULL, sold_at TEXT, closed_at TEXT, reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ready_sales_stock ON ready_sales(stock_id);
"""

def register_ready_routes(app, *, db, transaction, get_order, event, t, label, now, today, money, text_input, ValidationError, methods):
    def date_input(name):
        try:
            return datetime.strptime(request.form.get(name,''),'%Y-%m-%d').date().isoformat()
        except ValueError:
            raise ValidationError('أدخل تاريخاً صحيحاً. / Enter a valid date.')

    def stock_record(sid):
        row=db().execute('SELECT * FROM ready_stock WHERE id=?',(sid,)).fetchone()
        if not row:abort(404)
        return dict(row)

    def listing():
        rows=db().execute('''SELECT s.*,o.customer_name,o.customer_phone,o.total,o.cancelled,
            o.mode,o.shipping_status,o.due_date,COALESCE(p.paid,0) AS paid
            FROM ready_stock s LEFT JOIN orders o ON o.id=s.current_order_id
            LEFT JOIN (SELECT order_id,SUM(amount) AS paid FROM payments GROUP BY order_id) p ON p.order_id=o.id
            ORDER BY s.created_at DESC,s.id''').fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            item['balance']=item['total']-item['paid'] if item['current_order_id'] and not item['cancelled'] else 0
            item['refund_due']=item['paid'] if item['cancelled'] else 0
            result.append(item)
        return result

    @app.route('/ready-stock',methods=['GET','POST'])
    def ready_stock():
        if request.method=='POST':
            with transaction():
                token=text_input(request.form,'request_key',80,True)
                old=db().execute('SELECT id FROM ready_stock WHERE request_key=?',(token,)).fetchone()
                if old:return redirect(url_for('ready_detail',sid=old['id']))
                model=db().execute('SELECT * FROM models WHERE code=? AND active=1',(text_input(request.form,'code',60,True),)).fetchone()
                if not model:raise ValidationError('أضف الموديل وصورته في الكتالوج أولاً. / Add this model to the catalogue first.')
                displayed=date_input('display_date')
                if displayed>today():raise ValidationError('تاريخ العرض لا يكون في المستقبل. / Display date cannot be in the future.')
                price=money(request.form.get('price'))
                sid=uuid.uuid4().hex
                db().execute('INSERT INTO ready_stock(id,request_key,model_id,model_code,model_title,photo,display_date,price,size,color,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (sid,token,model['id'],model['code'],model['title'],model['photo'],displayed,price,text_input(request.form,'size',80),text_input(request.form,'color',80),text_input(request.form,'notes'),now()))
            return redirect(url_for('ready_detail',sid=sid))
        rows=listing()
        counts={state:sum(x['state']==state for x in rows) for state in ('available','reserved','sold','returned')}
        q=request.args.get('q','').strip().lower();state=request.args.get('state','')
        if q:rows=[x for x in rows if q in ' '.join(str(x.get(k) or '') for k in ('model_code','model_title','size','customer_name','customer_phone','id')).lower()]
        if state:rows=[x for x in rows if x['state']==state]
        return render_template('ready_stock.html',stock=rows,counts=counts,q=q,state=state,models=db().execute('SELECT * FROM models WHERE active=1 ORDER BY code').fetchall())

    @app.get('/ready-stock/<sid>')
    def ready_detail(sid):
        item=stock_record(sid)
        order=get_order(item['current_order_id']) if item['current_order_id'] else None
        history=db().execute('''SELECT r.*,o.created_at,o.customer_name,o.customer_phone,o.total,COALESCE(p.paid,0) AS paid
            FROM ready_sales r JOIN orders o ON o.id=r.order_id
            LEFT JOIN (SELECT order_id,SUM(amount) AS paid FROM payments GROUP BY order_id) p ON p.order_id=o.id
            WHERE r.stock_id=? ORDER BY r.order_id DESC''',(sid,)).fetchall()
        return render_template('ready_detail.html',stock=item,order=order,history=history,customers=db().execute('SELECT * FROM customers ORDER BY name').fetchall())

    @app.post('/ready-stock/<sid>/sale')
    def ready_sale(sid):
        with transaction():
            item=stock_record(sid)
            token=text_input(request.form,'request_key',80,True)
            old=db().execute('SELECT o.id,r.stock_id FROM orders o LEFT JOIN ready_sales r ON r.order_id=o.id WHERE o.request_key=?',(token,)).fetchone()
            if old:
                if old['stock_id']!=sid:abort(409)
                return redirect(url_for('order_detail',oid=old['id']))
            if item['state']!='available':raise ValidationError('هذه القطعة ليست متاحة للحجز أو البيع. / This piece is not available.')
            customer=db().execute('SELECT * FROM customers WHERE id=?',(request.form.get('customer_id'),)).fetchone()
            if not customer:raise ValidationError('اختر العميل أو أضفه أولاً. / Choose or add a customer first.')
            state=request.form.get('state')
            if state not in ('reserved','sold'):abort(400)
            due=date_input('due_date')
            mode=request.form.get('mode')
            if mode not in ('pickup','delivery'):abort(400)
            address=text_input(request.form,'delivery_address')
            if mode=='delivery' and not address:raise ValidationError('عنوان التوصيل مطلوب. / Delivery address is required.')
            discount=money(request.form.get('discount'))
            if discount>item['price']:raise ValidationError('الخصم يتجاوز قيمة العباية. / Discount exceeds the price.')
            fee=money(request.form.get('delivery_fee')) if mode=='delivery' else 0
            total=item['price']-discount+fee
            deposit=money(request.form.get('deposit'))
            if deposit>total:raise ValidationError('المدفوع يتجاوز قيمة الفاتورة. / Payment exceeds invoice total.')
            method=request.form.get('method')
            if method not in {c for c,_,_ in methods}:abort(400)
            notes=text_input(request.form,'notes')
            stamp=now()
            cur=db().execute('INSERT INTO orders(request_key,customer_id,customer_name,customer_phone,customer_address,created_at,due_date,mode,delivery_address,discount,delivery_fee,total,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (token,customer['id'],customer['name'],customer['phone'],customer['address'],stamp,due,mode,address,discount,fee,total,notes))
            oid=cur.lastrowid
            db().execute('INSERT INTO items(order_id,model_id,model_code,model_title,photo,quantity,price,kind,stage,measurements,color,notes) VALUES (?,?,?,?,?,1,?,?,?,?,?,?)',
                (oid,item['model_id'],item['model_code'],item['model_title'],item['photo'],item['price'],'stock','ready','{}',item['color'],'المقاس / Size: '+item['size']))
            db().execute('INSERT INTO ready_sales(order_id,stock_id,state,display_date,sold_at) VALUES (?,?,?,?,?)',(oid,sid,state,item['display_date'],stamp if state=='sold' else None))
            db().execute('UPDATE ready_stock SET state=?,current_order_id=? WHERE id=?',(state,oid,sid))
            if deposit:
                db().execute('INSERT INTO payments(order_id,request_key,amount,method,note,created_at) VALUES (?,?,?,?,?,?)',(oid,token+'-deposit',deposit,method,'دفعة أولى / Initial payment',stamp))
            event(oid,'ready_sale','جاهز / Ready-to-wear: '+state)
        return redirect(url_for('order_detail',oid=oid))

    @app.post('/ready-stock/<sid>/action')
    def ready_action(sid):
        with transaction():
            item=stock_record(sid)
            # Bind every action to the displayed sale. An old browser tab cannot
            # return or release a later customer's sale of the same piece.
            if str(item['current_order_id'] or '')!=request.form.get('order_id','') or not item['current_order_id']:
                raise ValidationError('تغير سجل القطعة؛ حدّث الصفحة قبل المتابعة. / This piece changed; refresh the page.')
            order=get_order(item['current_order_id'])
            action=request.form.get('action')
            reason=text_input(request.form,'reason',1000)
            stamp=now()
            if action=='sell':
                if item['state']=='sold':return redirect(url_for('ready_detail',sid=sid))
                if item['state']!='reserved' or order['cancelled']:raise ValidationError('القطعة ليست محجوزة. / The piece is not reserved.')
                db().execute("UPDATE ready_sales SET state='sold',sold_at=? WHERE order_id=?",(stamp,order['id']))
                db().execute("UPDATE ready_stock SET state='sold' WHERE id=?",(sid,))
                event(order['id'],'ready_sale','تأكيد البيع / Sale confirmed')
            elif action=='return':
                if item['state']=='returned':return redirect(url_for('ready_detail',sid=sid))
                if item['state']!='sold' or order['cancelled']:raise ValidationError('الإرجاع متاح لقطعة مباعة فقط. / Only a sold piece can be returned.')
                if not reason or request.form.get('received_back')!='yes':raise ValidationError('اذكر السبب وأكد استلام العباية فعلياً. / Give a reason and confirm the piece was physically received.')
                db().execute('UPDATE orders SET cancelled=1,cancel_reason=? WHERE id=?',(reason,order['id']))
                db().execute("UPDATE ready_sales SET state='returned',closed_at=?,reason=? WHERE order_id=?",(stamp,reason,order['id']))
                db().execute("UPDATE ready_stock SET state='returned' WHERE id=?",(sid,))
                event(order['id'],'ready_return','إرجاع كامل / Full return: '+reason)
            elif action=='relist':
                if item['state']!='returned':raise ValidationError('إعادة العرض متاحة للقطعة المرتجعة فقط. / Only a returned piece can be relisted.')
                displayed=date_input('display_date')
                if displayed<order['sale_closed_at'][:10] or displayed>today():raise ValidationError('تاريخ إعادة العرض يجب أن يكون من تاريخ الإرجاع حتى اليوم. / Relist date must be between the return date and today.')
                if request.form.get('inspected')!='yes':raise ValidationError('أكد صلاحية القطعة لإعادة البيع. / Confirm the piece is fit for resale.')
                price=money(request.form.get('price'))
                db().execute("UPDATE ready_stock SET state='available',current_order_id=NULL,display_date=?,price=? WHERE id=?",(displayed,price,sid))
                event(order['id'],'ready_relist','إعادة عرض القطعة / Piece relisted: '+displayed)
            else:abort(400)
        return redirect(url_for('ready_detail',sid=sid))

    @app.get('/export/ready-stock.csv')
    def export_ready():
        out=io.StringIO(newline='');writer=csv.writer(out)
        writer.writerow(['Piece','Model','Display date','Price AED','Sale status','Customer','Phone','Invoice total AED','Net paid AED','Balance AED','Refund due AED','Handover status','Due date'])
        for row in listing():
            values=[row['id'],row['model_code'],row['display_date'],f"{row['price']/100:.2f}",label(row['state']),row['customer_name'],row['customer_phone'],f"{(row['total'] or 0)/100:.2f}",f"{row['paid']/100:.2f}",f"{row['balance']/100:.2f}",f"{row['refund_due']/100:.2f}",label(row['shipping_status']) if row['shipping_status'] else '',row['due_date']]
            writer.writerow([("'"+str(v)) if str(v or '')[:1] in ('=','+','-','@','\t','\r','\n') else (v or '') for v in values])
        return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='fooladi-ready-stock-'+today()+'.csv')
