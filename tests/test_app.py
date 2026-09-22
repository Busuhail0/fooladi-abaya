import io
import json
import sqlite3
import sys
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app, money, ValidationError

@pytest.fixture
def env(tmp_path):
    app=create_app(tmp_path,{'TESTING':True})
    client=app.test_client()
    client.get('/setup')
    with client.session_transaction() as session:csrf=session['csrf']
    response=client.post('/setup',data=dict(csrf=csrf,username='owner',password='TestPassword123',confirm='TestPassword123',shop_name='فولادي للعباية'))
    assert response.status_code==302
    def post(path,**data):
        with client.session_transaction() as session:data['csrf']=session['csrf']
        return client.post(path,data=data)
    post('/customers',name='عميلة تجريبية',phone='0500000001',address='Dubai',unit='inch',length='56',bust='42')
    image=io.BytesIO();Image.new('RGB',(30,60),'black').save(image,'PNG');image.seek(0)
    post('/models',code='FL-101',title='عباية تجريبية',price='450.25',photo=(image,'model.png'))
    return app,client,post,tmp_path

def order_data(**overrides):
    data=dict(request_key=uuid.uuid4().hex,customer_id='1',due_date='2030-01-20',mode='pickup',delivery_address='',discount='25.00',delivery_fee='0',deposit='200',method='cash',item_count='1',i0_code='fl-101',i0_quantity='2',i0_price='450.25',i0_kind='custom',i0_unit='inch',i0_length='56',i0_bust='42',i0_tailor='Tailor A')
    data.update(overrides)
    return data

def read(env,sql,args=()):
    with sqlite3.connect(env[3]/'fooladi.sqlite3') as conn:
        conn.row_factory=sqlite3.Row
        return conn.execute(sql,args).fetchall()

def item_data(stage='cutting',**extra):
    data=dict(stage=stage,unit='inch',length='56',bust='42',tailor='Tailor A')
    data.update(extra)
    return data

def test_complete_pickup_lifecycle(env):
    app,c,post,_=env
    response=post('/orders/new',**order_data())
    assert response.status_code==302
    row=read(env,'SELECT * FROM orders')[0]
    assert row['total']==87550
    assert read(env,'SELECT SUM(amount) AS paid FROM payments')[0]['paid']==20000
    # No premature handover.
    response=post('/orders/1/delivery',mode='pickup',shipping_status='delivered',due_date='2030-01-20',credit_confirm='on')
    assert response.status_code==400
    # No skipped custom production stages.
    assert post('/orders/1/items/1',**item_data('ready')).status_code==400
    for stage in ['cutting','sewing','finishing','quality','ready']:
        assert post('/orders/1/items/1',**item_data(stage)).status_code==302
    # Handover with a balance needs explicit confirmation.
    assert post('/orders/1/delivery',mode='pickup',shipping_status='delivered',due_date='2030-01-20').status_code==400
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,type='payment',amount='675.50',method='card').status_code==302
    assert post('/orders/1/delivery',mode='pickup',shipping_status='delivered',due_date='2030-01-20').status_code==302
    assert read(env,'SELECT shipping_status FROM orders')[0][0]=='delivered'
    assert post('/orders/1/items/1',**item_data('quality',reason='Return')).status_code==400
    for path in ['/','/orders','/orders/1','/orders/1/invoice','/orders/1/invoice?workshop=1','/workshop','/export/orders.csv']:
        assert c.get(path).status_code==200,path

def test_courier_and_multiple_items(env):
    _,c,post,_=env
    data=order_data(mode='delivery',delivery_fee='20',delivery_address='Dubai, unit 1',item_count='2',i0_kind='custom',i1_code='FL-101',i1_quantity='1',i1_price='300.10',i1_kind='custom',i1_unit='cm',i1_length='145')
    assert post('/orders/new',**data).status_code==302
    assert read(env,'SELECT total FROM orders')[0][0]==119560
    assert len(read(env,'SELECT * FROM items'))==2
    assert post('/orders/1/delivery',mode='delivery',shipping_status='out',courier='Courier',delivery_address='Dubai',due_date='2030-01-20').status_code==400
    for stage in ['cutting','sewing','finishing','quality','ready']:
        assert post('/orders/1/items/1',**item_data(stage)).status_code==302
        assert post('/orders/1/items/2',**item_data(stage,unit='cm',length='145')).status_code==302
    assert post('/orders/1/delivery',mode='delivery',shipping_status='out',courier='',delivery_address='Dubai',due_date='2030-01-20').status_code==400
    assert post('/orders/1/delivery',mode='delivery',shipping_status='out',courier='Courier',tracking='TRACK-1',delivery_address='Dubai',due_date='2030-01-20').status_code==302
    assert post('/orders/1/delivery',mode='delivery',shipping_status='delivered',courier='Courier',delivery_address='Dubai',due_date='2030-01-20',credit_confirm='on').status_code==302
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,type='payment',amount='995.60',method='cod').status_code==302
    assert c.get('/orders/1').status_code==200

def test_duplicate_posts_and_overpayment(env):
    _,c,post,_=env
    data=order_data()
    assert post('/orders/new',**data).status_code==302
    assert post('/orders/new',**data).status_code==302
    assert len(read(env,'SELECT * FROM orders'))==1
    assert len(read(env,'SELECT * FROM payments'))==1
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,amount='700',method='cash').status_code==400
    token=uuid.uuid4().hex
    for _ in range(2):assert post('/orders/1/payment',request_key=token,amount='10.01',method='cash').status_code==302
    assert len(read(env,'SELECT * FROM payments'))==2
    assert read(env,'SELECT SUM(amount) FROM payments')[0][0]==21001

def test_cancel_refund_preserves_history(env):
    _,c,post,_=env
    post('/orders/new',**order_data())
    assert post('/orders/1/cancel',reason='Customer cancellation').status_code==302
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,amount='1',method='cash').status_code==400
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,type='refund',amount='201',method='cash',note='Refund').status_code==400
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,type='refund',amount='200',method='cash',note='Cancellation').status_code==302
    assert read(env,'SELECT SUM(amount) FROM payments')[0][0]==0
    assert len(read(env,'SELECT * FROM orders'))==1
    assert c.get('/orders/1/invoice').status_code==200

def test_snapshots_photos_and_customer_edits(env):
    _,c,post,_=env
    post('/orders/new',**order_data())
    before=read(env,'SELECT * FROM items')[0]
    assert c.get('/api/model?code=FL-101').json['photo_url']
    post('/models',id='1',code='FL-102',title='Changed title',price='900',active='on')
    post('/customers',id='1',name='Changed name',phone='0500000002',unit='cm',length='150')
    after=read(env,'SELECT * FROM items')[0]
    assert after['model_code']==before['model_code']=='FL-101'
    assert after['price']==45025
    assert json.loads(after['measurements'])['unit']=='inch'
    assert read(env,'SELECT customer_name FROM orders')[0][0]=='عميلة تجريبية'
    assert c.get('/photos/'+after['photo']).status_code==200
    assert post('/orders/1/items/1',**item_data('cutting',length='57')).status_code==302
    assert '56' in read(env,"SELECT detail FROM events WHERE kind='production'")[0][0]

def test_backup_restore_and_export(env,tmp_path):
    app,c,post,data=env
    post('/orders/new',**order_data())
    backup=c.get('/backup')
    assert backup.status_code==200
    with zipfile.ZipFile(io.BytesIO(backup.data)) as z:
        assert 'data/fooladi.sqlite3' in z.namelist()
        assert any(x.startswith('data/uploads/') for x in z.namelist())
        target=tmp_path/'restored';z.extractall(target)
    restored=create_app(target/'data',{'TESTING':True})
    client=restored.test_client();client.get('/login')
    with client.session_transaction() as s:csrf=s['csrf']
    assert client.post('/login',data=dict(csrf=csrf,username='owner',password='TestPassword123')).status_code==302
    assert client.get('/orders/1').status_code==200
    assert c.get('/export/orders.csv').data.startswith(b'\xef\xbb\xbf')

@pytest.mark.parametrize('bad',['NaN','Infinity','-1','10000001','abc'])
def test_invalid_money(bad):
    with pytest.raises(ValidationError):money(bad)

def test_authentication_csrf_and_password_rotation(env):
    app,c,post,_=env
    assert c.post('/customers',data=dict(name='Bad',phone='1')).status_code==400
    stranger=app.test_client()
    assert stranger.get('/orders').status_code==302
    assert stranger.get('/api/model?code=FL-101').status_code==302
    assert post('/settings/password',current='TestPassword123',password='NewPassword123',confirm='NewPassword123').status_code==302
    assert c.get('/orders').status_code==302

def test_parallel_payments_cannot_overpay(env):
    app,c,post,_=env
    post('/orders/new',**order_data(i0_quantity='1',i0_price='100',discount='0',deposit='0'))
    with c.session_transaction() as s:copied=dict(s)
    def pay():
        other=app.test_client()
        with other.session_transaction() as session:session.update(copied)
        return other.post('/orders/1/payment',data=dict(csrf=copied['csrf'],request_key=uuid.uuid4().hex,amount='70',method='cash')).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:pay(),range(2)))
    assert sorted(results)==[302,400]
    assert read(env,'SELECT SUM(amount) FROM payments')[0][0]==7000

def test_invalid_order_rolls_back_all_records(env):
    _,_,post,_=env
    assert post('/orders/new',**order_data(i0_code='NOT-A-MODEL')).status_code==400
    assert not read(env,'SELECT * FROM orders')
    assert post('/orders/new',**order_data(deposit='99999')).status_code==400
    assert not read(env,'SELECT * FROM orders')
    assert not read(env,'SELECT * FROM items')

def test_measurement_validation_and_stage_return(env):
    _,_,post,_=env
    assert post('/orders/new',**order_data(i0_length='',i0_bust='')).status_code==400
    assert post('/orders/new',**order_data(i0_length='NaN')).status_code==400
    post('/orders/new',**order_data())
    assert post('/orders/1/items/1',**item_data('cutting')).status_code==302
    assert post('/orders/1/items/1',**item_data('received')).status_code==400
    assert post('/orders/1/items/1',**item_data('received',reason='Measurement revision')).status_code==302

def test_language_and_invoice_without_workshop_prices(env):
    _,c,post,_=env
    post('/orders/new',**order_data())
    post('/language/en',next='/')
    assert b'Every order, in view.' in c.get('/').data
    for path in ['/customers','/models','/orders/new','/orders/1','/orders/1/invoice','/orders/1/invoice?workshop=1','/workshop','/settings']:
        assert c.get(path).status_code==200
    assert b'450.25' not in c.get('/orders/1/invoice?workshop=1').data
