"""Tracking privacy and catalogue removal against native SQLite and the D1 adapter."""
import re
import sqlite3
import uuid

import pytest

from test_app import read, order_data
from test_storefront import shop_env, publish, form_data
from test_app import env
from test_cloud import cloud_env


def new_request(e, **values):
    publish(e)
    visitor=e[0].test_client()
    result=visitor.post('/shop/custom/1',data=form_data(visitor,**values))
    assert result.status_code==303
    return visitor,result.headers['Location'],result.headers['Location'].rsplit('/',1)[1]


def track(visitor,reference,phone='+971501234567',**overrides):
    page=visitor.get('/shop/track')
    csrf=re.search(r'name="csrf" value="([^"]+)"',page.get_data(as_text=True)).group(1)
    return visitor.post('/shop/track',data=dict(csrf=csrf,reference=reference,phone=phone)|overrides)


def test_tracking_on_new_device_and_all_live_production_stages(shop_env):
    e=shop_env
    visitor,url,rid=new_request(e,notes='Private customer note',name='Private customer name')
    other=e[0].test_client()
    assert other.get(url).status_code==404
    reference='WEB-'+rid[:8].upper()
    assert track(other,reference,'0509999999').status_code==400
    result=track(other,reference,'٠٥٠ ١٢٣ ٤٥٦٧')
    assert result.status_code==303 and result.headers['Location']==url
    receipt=other.get(url)
    assert receipt.headers['Cache-Control']=='no-store'
    assert 'data-progress="new"' in receipt.get_data(as_text=True)
    assert e[2]('/customer-requests/'+rid,status='contacted',admin_note='Private internal note').status_code==302
    assert 'data-progress="contacted"' in other.get(url).get_data(as_text=True)
    response=e[2]('/customer-requests/'+rid+'/confirm',price='450.25',due_date='2030-01-15',confirmed='yes')
    assert response.status_code==302
    oid=read(e,'SELECT order_id FROM customer_requests WHERE id=?',(rid,))[0][0]
    iid=read(e,'SELECT id FROM items WHERE order_id=?',(oid,))[0][0]
    assert 'data-progress="confirmed"' in other.get(url).get_data(as_text=True)
    for stage in ('cutting','sewing','finishing','quality','ready'):
        result=e[2](f'/orders/{oid}/items/{iid}',stage=stage,unit='inch',length='56',shoulder='16',bust='42',sleeve='23',notes='Private workshop note',tailor='Private tailor')
        assert result.status_code==302
        body=other.get(url).get_data(as_text=True)
        assert f'data-progress="{stage}"' in body
        assert '2030-01-15' in body
        for private in ('Private customer name','Private customer note','Private internal note','Private workshop note','Private tailor','+971501234567','name="bust"'):
            assert private not in body
    assert e[2](f'/orders/{oid}/delivery',mode='pickup',shipping_status='delivered',due_date='2030-01-15',credit_confirm='yes').status_code==302
    assert 'data-progress="delivered"' in other.get(url).get_data(as_text=True)
    assert other.get('/orders/'+str(oid)).status_code==302
    assert e[0].test_client().get(url).status_code==404
    assert visitor.get(url).status_code==200


def test_tracking_authentication_expiry_collision_and_revocation(shop_env):
    e=shop_env;_,url,rid=new_request(e)
    other=e[0].test_client();reference='WEB-'+rid[:8].upper()
    bad=track(other,reference,'0507654321').get_data(as_text=True)
    absent=track(other,'WEB-00000000','0507654321').get_data(as_text=True)
    assert 'لم نتمكن من مطابقة رقم الطلب والهاتف.' in bad and 'لم نتمكن من مطابقة رقم الطلب والهاتف.' in absent
    assert track(other,reference,csrf='invalid').status_code==400
    assert other.get(url).status_code==404
    assert track(other,reference,'00971 50 123 4567').status_code==303
    with other.session_transaction() as s:
        grants=dict(s['store_tracking']);grants[rid]['expires']=0;s['store_tracking']=grants
    assert other.get(url).headers['Location']=='/shop/track?expired=1'
    assert track(other,reference,'+971501234567').status_code==303
    with sqlite3.connect(e[3]/'fooladi.sqlite3') as conn:
        conn.execute('UPDATE customer_requests SET customer_phone=? WHERE id=?',('+971509999999',rid))
    assert other.get(url).headers['Location']=='/shop/track?expired=1'
    assert track(other,reference,'+971509999999').status_code==303
    # A prefix collision with the same phone must not choose either customer's row.
    with sqlite3.connect(e[3]/'fooladi.sqlite3') as conn:
        conn.row_factory=sqlite3.Row
        row=dict(conn.execute('SELECT * FROM customer_requests WHERE id=?',(rid,)).fetchone())
        row.update(id=rid[:8]+uuid.uuid4().hex[8:],request_key=uuid.uuid4().hex)
        columns=','.join(row);slots=','.join('?' for _ in row)
        conn.execute(f'INSERT INTO customer_requests({columns}) VALUES ({slots})',list(row.values()))
    fresh=e[0].test_client()
    assert track(fresh,reference,'+971509999999').status_code==400


def test_tracking_rate_limit_survives_new_browser_sessions(shop_env):
    e=shop_env;_,url,rid=new_request(e)
    reference='WEB-'+rid[:8].upper()
    for _ in range(8):
        assert track(e[0].test_client(),reference,'0509999999').status_code==400
    assert track(e[0].test_client(),reference,'0509999999').status_code==429
    # Failed attempts for an unrelated phone do not block the real customer.
    assert track(e[0].test_client(),reference).status_code==303
    for i in range(11):
        assert track(e[0].test_client(),f'WEB-{i:08X}','0509999999').status_code==400
    assert track(e[0].test_client(),'WEB-ABCDEF12').status_code==429
    rows=read(e,'SELECT * FROM storefront_tracking_attempts')
    assert len(rows)==20
    assert all(len(row['ip_hash'])==64 and len(row['lookup_hash'])==64 for row in rows)


def test_unused_model_delete_needs_confirmation_and_preserves_private_boundary(shop_env):
    e=shop_env;publish(e)
    assert e[0].test_client().get('/models/1/remove').status_code==302
    assert e[1].get('/models/1/remove').status_code==200
    assert len(read(e,'SELECT * FROM models'))==1
    assert e[2]('/models/1/remove',action='delete').status_code==400
    assert e[1].post('/models/1/remove',data=dict(action='delete',confirmed='yes')).status_code==400
    assert e[2]('/models/1/remove',action='delete',confirmed='yes').status_code==302
    assert not read(e,'SELECT * FROM models')
    assert not read(e,'SELECT * FROM storefront_products')
    assert e[0].test_client().get('/shop/custom/1').status_code==404


def test_used_model_archive_and_photo_edits_keep_order_snapshots(shop_env):
    e=shop_env;_,url,rid=new_request(e)
    assert e[2]('/customer-requests/'+rid+'/confirm',price='450.25',due_date='2030-01-15',confirmed='yes').status_code==302
    old=dict(read(e,'SELECT * FROM items')[0])
    assert e[2]('/models',id='1',code='NEW-CODE',title='New title',price='600',active='on',remove_photo='on').status_code==302
    model=read(e,'SELECT * FROM models')[0]
    assert model['title']=='New title' and model['photo']==''
    assert dict(read(e,'SELECT * FROM items')[0])==old
    assert e[2]('/models/1/remove',action='delete',confirmed='yes').status_code==400
    assert e[2]('/models/1/remove',action='archive',confirmed='yes').status_code==302
    assert read(e,'SELECT active FROM models')[0][0]==0
    assert read(e,'SELECT published FROM storefront_products')[0][0]==0
    assert len(read(e,'SELECT * FROM orders'))==1
    assert dict(read(e,'SELECT * FROM items')[0])==old
    assert e[0].test_client().get('/shop/custom/1').status_code==404
    other=e[0].test_client()
    assert track(other,'WEB-'+rid[:8].upper()).status_code==303
    assert 'data-progress="confirmed"' in other.get(url).get_data(as_text=True)


def test_ready_stock_prevents_hard_delete_and_closed_request_tracks(shop_env):
    e=shop_env;_,url,rid=new_request(e)
    assert e[2]('/customer-requests/'+rid,status='closed',admin_note='Private reason').status_code==302
    visitor=e[0].test_client()
    assert track(visitor,'WEB-'+rid[:8].upper()).status_code==303
    page=visitor.get(url).get_data(as_text=True)
    assert 'data-progress="closed"' in page and 'Private reason' not in page
    assert e[2]('/models',code='STOCK-2',title='Stock design',price='300').status_code==302
    assert e[2]('/ready-stock',request_key=uuid.uuid4().hex,code='STOCK-2',display_date='2026-09-23',price='300',size='56',color='Black').status_code==302
    assert e[2]('/models/2/remove',action='delete',confirmed='yes').status_code==400
    assert e[2]('/models/2/remove',action='archive',confirmed='yes').status_code==302
    assert len(read(e,'SELECT * FROM ready_stock'))==1
