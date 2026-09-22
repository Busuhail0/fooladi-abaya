"""Run the actual Flask routes and D1 adapter against SQLite-backed binding fakes.

These validate SQL atomicity and storage boundaries, not the Workers runtime.
"""
import io
import json
import sqlite3
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient
from PIL import Image

import app as application
import cloud_storage
import test_app

class Statement:
    def __init__(self,session,sql,params=()):self.session=session;self.sql=sql;self.params=params
    def bind(self,*params):return Statement(self.session,self.sql,params)
    def all(self):
        self.session.owner.queries+=1
        cursor=self.session.conn.execute(self.sql,self.params)
        return {'results':[dict(row) for row in cursor.fetchall()],'meta':{'last_row_id':cursor.lastrowid}}

class D1Session:
    def __init__(self,owner):
        self.owner=owner
        self.conn=sqlite3.connect(owner.path,isolation_level=None,timeout=15,check_same_thread=False)
        self.conn.row_factory=sqlite3.Row;self.conn.execute('PRAGMA foreign_keys=ON')
    def prepare(self,sql):return Statement(self,sql)
    def batch(self,statements):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            result=[statement.all() for statement in statements]
            self.conn.commit();return result
        except Exception:
            self.conn.rollback();raise

class D1:
    def __init__(self,path):self.path=path;self.queries=0
    def withSession(self,mode):
        assert mode=='first-primary'
        return D1Session(self)

class R2:
    def __init__(self):self.files={}
    def put(self,key,data):self.files[key]=bytes(data)
    def get(self,key):
        content=self.files.get(key)
        return SimpleNamespace(arrayBuffer=lambda:content) if content is not None else None

@pytest.fixture
def cloud_env(tmp_path,monkeypatch):
    path=tmp_path/'fooladi.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript('\n'.join(m.read_text() for m in sorted((Path(__file__).parents[1]/'migrations').glob('*.sql'))))
    d1=D1(path);r2=R2()
    env=SimpleNamespace(DB=d1,PHOTOS=r2,SESSION_SECRET='s'*64,SETUP_TOKEN='t'*64,MAINTENANCE='0')
    monkeypatch.setattr(application,'D1Connection',lambda binding:cloud_storage.D1Connection(binding,sync=lambda x:x,array=lambda x:x))
    monkeypatch.setattr(application,'R2Photos',lambda binding:cloud_storage.R2Photos(binding,sync=lambda x:x,array=lambda x:x))
    app=application.create_app(test_config={'TESTING':True},cloud=True)
    class BoundClient(FlaskClient):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);self.environ_base['workers.env']=env
        def session_transaction(self,*args,**kwargs):
            kwargs.setdefault('environ_overrides',{})['workers.env']=env
            return super().session_transaction(*args,**kwargs)
    app.test_client_class=BoundClient
    client=app.test_client();client.get('/setup')
    def post(path,**data):
        with client.session_transaction() as s:data['csrf']=s['csrf']
        return client.post(path,data=data)
    response=post('/setup',setup_token=env.SETUP_TOKEN,username='owner',password='TestPassword123',confirm='TestPassword123',shop_name='فولادي للعباية')
    assert response.status_code==302
    post('/customers',name='عميلة تجريبية',phone='0500000001',address='Dubai',unit='inch',length='56',bust='42')
    image=io.BytesIO();Image.new('RGB',(30,60),'black').save(image,'PNG');image.seek(0)
    assert post('/models',code='FL-101',title='عباية تجريبية',price='450.25',photo=(image,'model.png')).status_code==302
    return (app,client,post,tmp_path),env

@pytest.mark.parametrize('scenario',[
    test_app.test_complete_pickup_lifecycle,
    test_app.test_courier_and_multiple_items,
    test_app.test_duplicate_posts_and_overpayment,
    test_app.test_cancel_refund_preserves_history,
    test_app.test_snapshots_photos_and_customer_edits,
    test_app.test_authentication_csrf_and_password_rotation,
    test_app.test_invalid_order_rolls_back_all_records,
    test_app.test_measurement_validation_and_stage_return,
    test_app.test_language_and_invoice_without_workshop_prices,
])
def test_cloud_business_workflows(cloud_env,scenario):
    scenario(cloud_env[0])

def test_d1_concurrent_update_is_all_or_nothing(cloud_env):
    _,env=cloud_env
    a=cloud_storage.D1Connection(env.DB,sync=lambda x:x,array=lambda x:x)
    b=cloud_storage.D1Connection(env.DB,sync=lambda x:x,array=lambda x:x)
    a.execute('BEGIN IMMEDIATE');b.execute('BEGIN IMMEDIATE')
    a.execute('UPDATE customers SET name=? WHERE id=1',('first writer',))
    b.execute('UPDATE customers SET name=? WHERE id=1',('second writer',))
    b.execute('INSERT INTO settings(key,value) VALUES (?,?)',('must_rollback','yes'))
    a.commit()
    with pytest.raises(cloud_storage.ConcurrentWrite):b.commit()
    assert a.execute('SELECT name FROM customers WHERE id=1').fetchone()[0]=='first writer'
    assert a.execute("SELECT value FROM settings WHERE key='must_rollback'").fetchone() is None

def test_cloud_backup_has_consistent_records_and_private_photos(cloud_env):
    local,env=cloud_env
    _,client,post,_=local
    post('/orders/new',**test_app.order_data())
    response=client.get('/backup')
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.data)) as z:
        result=json.loads(z.read('database.json'))
        assert result['format']=='fooladi-cloud-v2'
        assert result['tables']['orders'][0]['total']==87550
        image=result['tables']['models'][0]['photo']
        assert z.read('photos/'+image)==env.PHOTOS.files['models/'+image]
    stranger=local[0].test_client()
    assert stranger.get('/photos/'+image).status_code==302
    assert not (local[3]/'uploads').exists()

def test_cloud_dashboard_queries_do_not_grow_per_order(cloud_env):
    local,env=cloud_env
    _,client,post,_=local
    for _ in range(5):post('/orders/new',**test_app.order_data())
    env.DB.queries=0
    assert client.get('/').status_code==200
    assert env.DB.queries<=4

def test_claiming_admin_requires_owner_token(tmp_path,monkeypatch):
    with sqlite3.connect(tmp_path/'empty.db') as db:db.executescript('\n'.join(m.read_text() for m in sorted((Path(__file__).parents[1]/'migrations').glob('*.sql'))))
    binding=SimpleNamespace(DB=D1(tmp_path/'empty.db'),SESSION_SECRET='s'*64,SETUP_TOKEN='t'*64,MAINTENANCE='0')
    monkeypatch.setattr(application,'D1Connection',lambda b:cloud_storage.D1Connection(b,sync=lambda x:x,array=lambda x:x))
    app=application.create_app(test_config={'TESTING':True},cloud=True)
    c=app.test_client();c.environ_base['workers.env']=binding
    first=c.get('/setup')
    assert 'Secure' in first.headers['Set-Cookie']
    with c.session_transaction(environ_overrides={'workers.env':binding}) as s:csrf=s['csrf']
    assert c.post('/setup',data=dict(csrf=csrf,username='uninvited',password='LongPassword123',confirm='LongPassword123',shop_name='Wrong shop')).status_code==400
    with sqlite3.connect(tmp_path/'empty.db') as db:assert db.execute('SELECT COUNT(*) FROM settings').fetchone()[0]==0

def test_r2_path_validation(cloud_env):
    _,env=cloud_env
    photos=cloud_storage.R2Photos(env.PHOTOS,sync=lambda x:x,array=lambda x:x)
    assert photos.get('../../private') is None
    with pytest.raises(ValueError):photos.put('../bad.jpg',b'data')

def test_no_read_after_buffered_write(cloud_env):
    _,env=cloud_env
    connection=cloud_storage.D1Connection(env.DB,sync=lambda x:x,array=lambda x:x)
    connection.execute('BEGIN IMMEDIATE')
    connection.execute('UPDATE customers SET name=? WHERE id=1',('not saved',))
    with pytest.raises(RuntimeError):connection.execute('SELECT * FROM customers')
    connection.rollback()
    assert connection.execute('SELECT name FROM customers WHERE id=1').fetchone()[0]=='عميلة تجريبية'

def test_cloud_login_uses_shared_rate_limit_and_password(cloud_env):
    local,_=cloud_env
    _,client,post,_=local
    post('/logout');client.get('/login')
    for _ in range(2):
        assert post('/login',username='owner',password='incorrect').status_code==200
    assert post('/login',username='owner',password='TestPassword123').status_code==302
    assert client.get('/').status_code==200

def test_null_binding_and_result_normalization(cloud_env):
    _,env=cloud_env
    connection=cloud_storage.D1Connection(env.DB,sync=lambda x:x,array=lambda x:x)
    marker=object();connection.null=marker
    prepared=connection.statement('SELECT ?',(None,))
    assert prepared.params==(marker,)
    assert cloud_storage.plain({'nested':[cloud_storage.JS_NULL,1]})=={'nested':[None,1]}

def test_cloud_backup_restores_into_empty_database(cloud_env):
    import subprocess
    import sys
    local,env=cloud_env
    _,client,post,tmp_path=local
    post('/orders/new',**test_app.order_data())
    backup=tmp_path/'backup.zip'
    backup.write_bytes(client.get('/backup').data)
    output=tmp_path/'restored'
    root=Path(__file__).parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts/prepare_restore.py'),str(backup),'--output',str(output)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    with sqlite3.connect(':memory:') as db:
        db.executescript('\n'.join(m.read_text() for m in sorted((root/'migrations').glob('*.sql'))))
        db.executescript((output/'database.sql').read_text())
        assert db.execute('SELECT total FROM orders').fetchone()[0]==87550
        assert db.execute('SELECT COUNT(*) FROM payments').fetchone()[0]>=1
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        photo=db.execute('SELECT photo FROM models').fetchone()[0]
    assert (output/'photos'/photo).read_bytes()==env.PHOTOS.files['models/'+photo]
