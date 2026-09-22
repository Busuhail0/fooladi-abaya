"""Business acceptance tests for distinct stock, tailoring, money and handover."""
import io
import json
import sqlite3
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app import today
from test_app import env, read, order_data
from test_cloud import cloud_env

@pytest.fixture(params=['local','cloud'])
def store_env(request):
    return request.getfixturevalue('env') if request.param=='local' else request.getfixturevalue('cloud_env')[0]

def add_stock(e,**overrides):
    data=dict(request_key=uuid.uuid4().hex,code='fl-101',display_date=today(),price='450.25',size='56',color='Black')
    data.update(overrides)
    response=e[2]('/ready-stock',**data)
    assert response.status_code==302
    return response.headers['Location'].rsplit('/',1)[-1]

def sale_data(**overrides):
    values=dict(request_key=uuid.uuid4().hex,customer_id='1',state='reserved',due_date='2030-01-20',mode='pickup',discount='25.25',delivery_fee='0',deposit='100',method='cash')
    values.update(overrides)
    return values

def test_stock_reservation_sale_delivery_return_and_relist(store_env):
    e=store_env;_,c,post,_=e
    sid=add_stock(e)
    data=sale_data()
    for _ in range(2):assert post('/ready-stock/'+sid+'/sale',**data).status_code==302
    assert len(read(e,'SELECT * FROM orders'))==1
    assert read(e,'SELECT total FROM orders')[0][0]==42500
    assert read(e,'SELECT state FROM ready_stock')[0][0]=='reserved'
    assert post('/ready-stock/'+sid+'/sale',**sale_data()).status_code==400
    delivery=dict(mode='pickup',shipping_status='delivered',due_date='2030-01-20',credit_confirm='on')
    assert post('/orders/1/delivery',**delivery).status_code==400
    assert post('/ready-stock/'+sid+'/action',order_id='1',action='sell').status_code==302
    assert post('/orders/1/delivery',**delivery).status_code==302
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,amount='325',method='card').status_code==302
    # Changing inventory state never silently invents a cash refund.
    assert post('/ready-stock/'+sid+'/action',order_id='1',action='return',reason='Wrong fit').status_code==400
    assert post('/ready-stock/'+sid+'/action',order_id='1',action='return',reason='Wrong fit',received_back='yes').status_code==302
    assert read(e,'SELECT SUM(amount) FROM payments')[0][0]==42500
    assert read(e,'SELECT cancelled FROM orders')[0][0]==1
    returned=c.get('/orders/1').get_data(as_text=True)
    assert 'إرجاع كامل' in returned and '425.00' in returned
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,amount='1',method='cash').status_code==400
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,type='refund',amount='426',method='cash',note='Return').status_code==400
    assert post('/orders/1/payment',request_key=uuid.uuid4().hex,type='refund',amount='425',method='cash',note='Return').status_code==302
    assert post('/ready-stock/'+sid+'/action',order_id='1',action='relist',display_date=today(),price='460',inspected='yes').status_code==302
    assert post('/ready-stock/'+sid+'/sale',**sale_data(state='sold',deposit='0',discount='0')).status_code==302
    assert len(read(e,'SELECT * FROM ready_sales'))==2
    assert read(e,'SELECT total FROM orders WHERE id=1')[0][0]==42500
    assert read(e,'SELECT total FROM orders WHERE id=2')[0][0]==46000
    # A stale tab for the old customer cannot affect the new sale.
    assert post('/ready-stock/'+sid+'/action',order_id='1',action='return',reason='Old form',received_back='yes').status_code==400
    assert read(e,'SELECT state FROM ready_stock')[0][0]=='sold'
    for path in ['/ready-stock','/ready-stock/'+sid,'/orders/1/invoice','/orders/2','/orders?kind=stock','/export/ready-stock.csv']:
        assert c.get(path).status_code==200,path

def test_release_reservation_preserves_refund_and_allows_next_customer(store_env):
    e=store_env;_,c,post,_=e
    sid=add_stock(e)
    post('/ready-stock/'+sid+'/sale',**sale_data())
    assert post('/orders/1/cancel',reason='Customer changed mind').status_code==302
    assert read(e,'SELECT state,current_order_id FROM ready_stock')[0][:]==('available',None)
    assert read(e,'SELECT state FROM ready_sales')[0][0]=='released'
    assert read(e,'SELECT SUM(amount) FROM payments')[0][0]==10000
    assert '100.00' in c.get('/orders/1').get_data(as_text=True)
    assert post('/ready-stock/'+sid+'/sale',**sale_data()).status_code==302
    assert len(read(e,'SELECT * FROM orders'))==2

def test_separate_paths_images_search_and_fulfilment(store_env):
    e=store_env;_,c,post,_=e
    sid=add_stock(e)
    post('/orders/new',**order_data())
    response=post('/ready-stock/'+sid+'/sale',**sale_data(state='sold',mode='delivery',delivery_address='Dubai',delivery_fee='20'))
    assert response.status_code==302
    assert read(e,'SELECT total FROM orders WHERE id=2')[0][0]==44500
    for path in ['/orders?kind=custom','/tailoring','/workshop']:
        body=c.get(path).get_data(as_text=True)
        assert 'FL-00001' in body and 'FL-00002' not in body
    assert 'FL-00001' not in c.get('/orders?kind=stock').get_data(as_text=True)
    assert 'FL-00002' in c.get('/orders?kind=stock').get_data(as_text=True)
    assert post('/orders/new',**order_data(i0_kind='stock')).status_code==400
    assert post('/orders/2/items/2',stage='cutting').status_code==400
    assert post('/orders/2/cancel',reason='Use return instead').status_code==400
    # Editing a model cannot change a stock piece already on display.
    original=read(e,'SELECT * FROM ready_stock')[0]
    post('/models',id='1',code='FL-999',title='Renamed',price='900',active='on')
    assert read(e,'SELECT model_code,price,photo FROM ready_stock')[0][:]==('FL-101',45025,original['photo'])
    assert c.get('/photos/'+original['photo']).status_code==200
    assert 'عميلة تجريبية' in c.get('/ready-stock?q=0500000001&state=sold').get_data(as_text=True)
    assert c.get('/export/ready-stock.csv').data.startswith(b'\xef\xbb\xbf')
    for language in ['en','ar']:
        post('/language/'+language,next='/ready-stock')
        for path in ['/','/ready-stock','/ready-stock/'+sid,'/orders/2','/orders/2/invoice']:
            assert c.get(path).status_code==200

def test_stock_validation_and_duplicate_intake(store_env):
    e=store_env;_,_,post,_=e
    token=uuid.uuid4().hex
    sid=add_stock(e,request_key=token)
    assert add_stock(e,request_key=token)==sid
    for bad in [dict(code='missing'),dict(price='NaN'),dict(display_date='invalid'),dict(display_date='2999-01-01')]:
        data=dict(request_key=uuid.uuid4().hex,code='FL-101',price='450.25',display_date=today());data.update(bad)
        assert post('/ready-stock',**data).status_code==400
    for bad in [dict(deposit='1000'),dict(customer_id='999'),dict(mode='delivery',delivery_address=''),dict(discount='500'),dict(state='returned')]:
        assert post('/ready-stock/'+sid+'/sale',**sale_data(**bad)).status_code==400
    assert not read(e,'SELECT * FROM orders')
    assert read(e,'SELECT state FROM ready_stock')[0][0]=='available'

def test_two_employees_cannot_reserve_one_piece_twice(env):
    app,c,_,_=env
    sid=add_stock(env)
    with c.session_transaction() as session:copied=dict(session)
    def reserve(_):
        other=app.test_client()
        with other.session_transaction() as session:session.update(copied)
        return other.post('/ready-stock/'+sid+'/sale',data=dict(csrf=copied['csrf'],**sale_data())).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(reserve,range(2)))
    assert sorted(codes)==[302,400]
    assert len(read(env,'SELECT * FROM ready_sales'))==1
    assert len(read(env,'SELECT * FROM payments'))==1

def test_ready_cloud_backup_restoration_includes_stock_and_history(cloud_env,tmp_path):
    import subprocess
    import sys
    e,_=cloud_env
    sid=add_stock(e)
    e[2]('/ready-stock/'+sid+'/sale',**sale_data(state='sold'))
    e[2]('/ready-stock/'+sid+'/action',order_id='1',action='return',reason='Return',received_back='yes')
    payload=e[1].get('/backup').data
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        tables=json.loads(z.read('database.json'))['tables']
        assert tables['ready_stock'][0]['state']=='returned'
        assert tables['ready_sales'][0]['state']=='returned'
    source=tmp_path/'cloud.zip';source.write_bytes(payload)
    output=tmp_path/'import';root=Path(__file__).parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts/prepare_restore.py'),str(source),'--output',str(output)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    with sqlite3.connect(':memory:') as db:
        for m in sorted((root/'migrations').glob('*.sql')):db.executescript(m.read_text())
        db.executescript((output/'database.sql').read_text())
        assert db.execute('SELECT state FROM ready_stock').fetchone()[0]=='returned'
        assert db.execute('SELECT SUM(amount) FROM payments').fetchone()[0]==10000
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
