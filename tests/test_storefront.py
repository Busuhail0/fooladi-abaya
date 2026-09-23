"""Exercise public/private boundaries and the real enquiry-to-order workflow."""
import io
import json
import re
import sqlite3
import uuid
import zipfile

import pytest

from test_app import env, read
from test_cloud import cloud_env

@pytest.fixture(params=['local','cloud'])
def shop_env(request):
    return request.getfixturevalue('env') if request.param=='local' else request.getfixturevalue('cloud_env')[0]


def publish(e,**changes):
    data=dict(model_id='1',published='on',custom_available='on',description='Customer description')
    data.update(changes)
    assert e[2]('/storefront',**data).status_code==302


def form_data(visitor,path='/shop/custom/1',**changes):
    page=visitor.get(path)
    assert page.status_code==200
    body=page.get_data(as_text=True)
    def field(name):return re.search(r'name="'+name+r'" value="([^"]*)"',body).group(1)
    data=dict(csrf=field('csrf'),request_key=field('request_key'),quoted_price=field('quoted_price'),
              name='Online customer',phone='+971 50 123 4567',quantity='2',color='Black',unit='inch',
              length='56',shoulder='16',bust='42',sleeve='23',mode='pickup',consent='yes')
    data.update(changes)
    return data


def test_publishing_and_private_admin_boundaries(shop_env):
    e=shop_env;visitor=e[0].test_client()
    assert visitor.get('/shop').status_code in (200,308)
    assert visitor.get('/shop/custom/1').status_code==404
    assert visitor.get('/shop/media/model/1').status_code==404
    for url in ['/customer-requests','/storefront','/customers','/orders','/api/model?code=FL-101','/backup']:
        assert visitor.get(url).status_code==302
    publish(e)
    page=visitor.get('/shop/').get_data(as_text=True)
    assert 'Customer description' in visitor.get('/shop/custom/1').get_data(as_text=True)
    assert 'FL-101' in page and 'عميلة تجريبية' not in page
    assert visitor.get('/shop/media/model/1').status_code==200
    # Public media does not make arbitrary private photos accessible.
    name=read(e,'SELECT photo FROM models')[0][0]
    assert visitor.get('/photos/'+name).status_code==302
    publish(e,published='')
    assert visitor.get('/shop/media/model/1').status_code==404


def test_customer_form_receipt_confirmation_and_no_fake_payment(shop_env):
    e=shop_env;publish(e);visitor=e[0].test_client()
    data=form_data(visitor,mode='delivery',address='Dubai, Mirdif',notes='Please call first')
    submitted=visitor.post('/shop/custom/1',data=data)
    assert submitted.status_code==303
    receipt=submitted.headers['Location'];rid=receipt.rsplit('/',1)[1]
    assert visitor.get(receipt).status_code==200
    assert e[0].test_client().get(receipt).status_code==404
    assert visitor.post('/shop/custom/1',data=data).headers['Location']==receipt
    assert len(read(e,'SELECT * FROM customer_requests'))==1
    assert not read(e,'SELECT * FROM orders')
    assert not read(e,'SELECT * FROM payments')
    request_record=read(e,'SELECT * FROM customer_requests')[0]
    assert request_record['unit_price']==45025 and request_record['customer_phone']=='+971501234567'
    assert e[1].get('/customer-requests').status_code==200
    assert e[1].get('/customer-requests/'+rid).status_code==200
    confirm=dict(price='450.25',delivery_fee='20',due_date='2030-01-15',confirmed='yes')
    result=e[2]('/customer-requests/'+rid+'/confirm',**confirm)
    assert result.status_code==302
    order=read(e,'SELECT * FROM orders')[0]
    assert order['total']==92050 and order['mode']=='delivery'
    assert order['customer_name']=='Online customer'
    assert read(e,'SELECT name FROM customers WHERE id=1')[0][0]=='عميلة تجريبية'
    assert json.loads(read(e,'SELECT measurements FROM items')[0][0])['bust']=='42'
    assert not read(e,'SELECT * FROM payments')
    assert e[2]('/customer-requests/'+rid+'/confirm',**confirm).headers['Location']==result.headers['Location']
    assert len(read(e,'SELECT * FROM orders'))==1
    assert e[1].get(result.headers['Location']).status_code==200
    assert visitor.get(result.headers['Location']).status_code==302
    assert visitor.get(receipt).status_code==200


def test_invalid_form_tampering_and_preserved_input(shop_env):
    e=shop_env;publish(e);visitor=e[0].test_client()
    base=form_data(visitor)
    for override in [dict(phone='bad'),dict(quantity='0'),dict(quantity='11'),dict(length='NaN'),dict(bust=''),
                     dict(consent=''),dict(unit='other'),dict(quoted_price='1'),dict(mode='delivery',address='')]:
        result=visitor.post('/shop/custom/1',data=base|override)
        assert result.status_code==400
        assert 'Online customer' in result.get_data(as_text=True)
    assert visitor.post('/shop/custom/1',data=base|dict(csrf='wrong')).status_code==400
    assert visitor.post('/shop/custom/1',data=base|dict(request_key=uuid.uuid4().hex)).status_code==400
    assert not read(e,'SELECT * FROM customer_requests')
    assert e[2]('/storefront',action='availability',store_open='').status_code==302
    assert visitor.post('/shop/custom/1',data=base).status_code==400
    assert not read(e,'SELECT * FROM customer_requests')


def test_shared_rate_limit_and_receipt_ownership(shop_env):
    e=shop_env;publish(e);first=e[0].test_client()
    for i in range(10):
        d=form_data(first)
        assert first.post('/shop/custom/1',data=d).status_code==303
    second=e[0].test_client();d=form_data(second)
    assert second.post('/shop/custom/1',data=d).status_code==429
    assert len(read(e,'SELECT * FROM customer_requests'))==10
    rid=read(e,'SELECT id FROM customer_requests')[0][0]
    assert second.get('/shop/request/'+rid).status_code==404
    assert first.get('/shop/request/'+rid).status_code==200
    assert 'Online customer' not in second.get('/shop/').get_data(as_text=True)


def test_ready_stock_is_only_reserved_on_staff_confirmation(shop_env):
    e=shop_env;publish(e)
    res=e[2]('/ready-stock',request_key=uuid.uuid4().hex,code='FL-101',display_date='2026-01-01',price='380',size='56',color='Black')
    sid=res.headers['Location'].rsplit('/',1)[1]
    guest=e[0].test_client();path='/shop/ready/'+sid
    data=form_data(guest,path,quantity='999',price='1')
    response=guest.post(path,data=data);assert response.status_code==303
    rid=response.headers['Location'].rsplit('/',1)[1]
    stock=read(e,'SELECT * FROM ready_stock')[0]
    assert stock['state']=='available' and stock['current_order_id'] is None
    assert read(e,'SELECT quantity,unit_price FROM customer_requests')[0][:]==(1,38000)
    other=e[0].test_client();second=other.post(path,data=form_data(other,path))
    second_rid=second.headers['Location'].rsplit('/',1)[1]
    args=dict(price='380',due_date='2030-01-15',confirmed='yes')
    assert e[2]('/customer-requests/'+rid+'/confirm',**args).status_code==302
    assert read(e,'SELECT state FROM ready_stock')[0][0]=='reserved'
    assert e[2]('/customer-requests/'+second_rid+'/confirm',**args).status_code==400
    assert len(read(e,'SELECT * FROM orders'))==1
    assert guest.get(path).status_code==404
    assert sid not in guest.get('/shop/?kind=stock').get_data(as_text=True)


def test_new_request_text_is_escaped_and_backup_contains_requests(shop_env):
    e=shop_env;publish(e,description='<script>alert(1)</script>')
    visitor=e[0].test_client()
    page=visitor.get('/shop/custom/1').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in page and '&lt;script&gt;' in page
    result=visitor.post('/shop/custom/1',data=form_data(visitor,name='<img src=x onerror=alert(1)>'))
    rid=result.headers['Location'].rsplit('/',1)[1]
    admin=e[1].get('/customer-requests/'+rid).get_data(as_text=True)
    assert '<img src=x onerror=alert(1)>' not in admin
    if e[0].config['CLOUD']:
        with zipfile.ZipFile(io.BytesIO(e[1].get('/backup').data)) as z:
            archive=json.loads(z.read('database.json'))
            assert archive['tables']['customer_requests'][0]['id']==rid
            assert archive['tables']['storefront_products'][0]['published']==1
