"""Cloudflare bindings, isolated from Flask business rules.

D1 has no multi-request SQL transaction. Writes are buffered, then committed in
ONE atomic D1 batch with a database revision compare-and-swap. A changed revision
aborts the whole batch. Reads occur before buffered writes (not read-your-writes).
Do not add read-after-write business logic without revisiting this contract.
"""
import io
import json
import re
import sqlite3
import zipfile
from collections import deque

from flask import request
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import URLSafeTimedSerializer

try:
    from pyodide.ffi import jsnull as JS_NULL
except ImportError:
    JS_NULL=object()  # Native CPython tests do not run in Pyodide.

TABLES=('settings','customers','models','orders','items','payments','events','login_attempts','order_numbers','ready_stock','ready_sales','storefront_products','customer_requests')
PHOTO_NAME=re.compile(r'^[0-9a-f]{32}\.jpg$')

class CloudBackendError(Exception):
    pass

class ConcurrentWrite(CloudBackendError):
    pass

def bridge(promise):
    from pyodide.ffi import run_sync
    return run_sync(promise)

def plain(value):
    value=value.to_py() if hasattr(value,'to_py') else value
    if value is JS_NULL:return None
    if isinstance(value,dict):return {key:plain(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):return [plain(item) for item in value]
    return value

def js_array(values):
    from pyodide.ffi import to_js
    return to_js(values)

def binding_array(values):
    # Prepared statements are SDK binding wrappers, not raw JsProxy objects.
    # Generic to_js creates borrowed Python proxies that expire before D1's
    # asynchronous batch finishes. The SDK converter unwraps the JS bindings.
    from workers.rpc import python_to_rpc
    return python_to_rpc(values)

def translate(error):
    detail=str(error)
    if 'cloud_revision_guard' in detail:
        return ConcurrentWrite('Concurrent update; no changes committed.')
    if any(marker in detail for marker in ('UNIQUE constraint failed','CHECK constraint failed','FOREIGN KEY constraint failed','NOT NULL constraint failed')):
        return sqlite3.IntegrityError('Database constraint failed.')
    return CloudBackendError('Cloud storage operation failed.')

class Row(dict):
    def __getitem__(self,key):
        return tuple(self.values())[key] if isinstance(key,int) else super().__getitem__(key)

class Cursor:
    def __init__(self,rows=(),lastrowid=None):
        self.rows=deque(Row(row) for row in rows)
        self.lastrowid=lastrowid
    def fetchone(self):
        return self.rows.popleft() if self.rows else None
    def fetchall(self):
        rows=list(self.rows);self.rows.clear();return rows
    def __iter__(self):
        while self.rows:yield self.rows.popleft()

class D1Connection:
    def __init__(self,binding,sync=None,array=None):
        self.binding=binding.withSession('first-primary')
        self.sync=sync or bridge
        self.array=array or binding_array
        self.null=None if sync is not None else JS_NULL
        self.pending=None
        self.revision=None
    def statement(self,sql,params=()):
        # Python None becomes JS undefined, which D1 rejects. Use explicit jsnull.
        values=tuple(self.null if value is None else value for value in params)
        return self.binding.prepare(sql).bind(*values) if values else self.binding.prepare(sql)
    def query(self,sql,params=()):
        try:
            result=plain(self.sync(self.statement(sql,params).all()))
            return Cursor(plain(result['results']),plain(result.get('meta',{})).get('last_row_id'))
        except Exception as error:
            raise translate(error) from error
    def execute(self,sql,params=()):
        verb=sql.strip().split(None,1)[0].upper()
        if verb=='BEGIN':
            if self.pending is not None:raise RuntimeError('Nested transaction is not supported.')
            row=self.query('SELECT revision FROM cloud_revision WHERE id=1').fetchone()
            if row is None:raise CloudBackendError('Database migration is required.')
            self.revision=row[0];self.pending=[]
            return Cursor()
        if verb=='SELECT':
            if self.pending:
                raise RuntimeError('Cloud adapter forbids SELECT after queued writes. Read first, then mutate.')
            return self.query(sql,params)
        if self.pending is None:
            raise RuntimeError('Cloud writes require an atomic transaction block.')
        lastrowid=None
        if sql.startswith('INSERT INTO orders('):
            # Reserve an order number, not an order. A failed write can leave a
            # harmless numbering gap. Reservation does not change business data.
            lastrowid=self.query('INSERT INTO order_numbers DEFAULT VALUES RETURNING id').fetchone()[0]
            sql=sql.replace('INSERT INTO orders(','INSERT INTO orders(id,',1).replace('VALUES (','VALUES (?,',1)
            params=(lastrowid,)+tuple(params)
        self.pending.append(self.statement(sql,params))
        return Cursor(lastrowid=lastrowid)
    def commit(self):
        if not self.pending:
            self.pending=None;return
        guard=self.statement('UPDATE cloud_revision SET revision=CASE WHEN revision=? THEN revision+1 ELSE -1 END WHERE id=1',(self.revision,))
        statements=[guard]+self.pending
        try:
            self.sync(self.binding.batch(self.array(statements)))
        except Exception as error:
            raise translate(error) from error
        finally:
            self.pending=None
    def rollback(self):
        self.pending=None
    def close(self):
        self.rollback()
    def snapshot(self):
        try:
            # A single read-only batch gives a consistent export across tables.
            results=plain(self.sync(self.binding.batch(self.array([self.statement(f'SELECT * FROM {table} LIMIT 10001') for table in TABLES]))))
            tables={table:plain(plain(result)['results']) for table,result in zip(TABLES,results)}
            if any(len(rows)>10000 for rows in tables.values()):
                raise CloudBackendError('Use D1 export for large databases.')
            return tables
        except CloudBackendError:
            raise
        except Exception as error:
            raise translate(error) from error

class R2Photos:
    def __init__(self,binding,sync=None,array=None):
        self.binding=binding;self.sync=sync or bridge;self.array=array or js_array
    def put(self,name,payload):
        if not PHOTO_NAME.fullmatch(name):raise ValueError('Invalid photo name')
        try:
            # Only re-encoded JPEGs generated by the application reach this path.
            self.sync(self.binding.put('models/'+name,self.array(payload)))
        except Exception as error:
            raise CloudBackendError('Photo upload failed.') from error
    def get(self,name):
        if not PHOTO_NAME.fullmatch(name):return None
        try:
            obj=self.sync(self.binding.get('models/'+name))
            if obj is None or obj is JS_NULL:return None
            raw=self.sync(obj.arrayBuffer())
            return raw.to_bytes() if hasattr(raw,'to_bytes') else bytes(plain(raw))
        except Exception as error:
            raise CloudBackendError('Photo download failed.') from error

class WorkerSessionInterface(SecureCookieSessionInterface):
    def get_signing_serializer(self,app):
        secret=str(getattr(request.environ['workers.env'],'SESSION_SECRET',''))
        if len(secret)<32:
            raise RuntimeError('SESSION_SECRET must be configured before deployment.')
        return URLSafeTimedSerializer(secret,salt=self.salt,serializer=self.serializer,signer_kwargs={'key_derivation':self.key_derivation,'digest_method':self.digest_method})

def build_backup(connection,photos,max_bytes=20*1024*1024):
    tables=connection.snapshot()
    names=sorted({row['photo'] for table in ('models','items','ready_stock','customer_requests') for row in tables[table] if row.get('photo')})
    if len(names)>100:raise CloudBackendError('Use administrative export for more than 100 photos.')
    raw=json.dumps({'format':'fooladi-cloud-v3','tables':tables},ensure_ascii=False).encode('utf-8')
    if len(raw)>max_bytes:raise CloudBackendError('Use administrative export for a large backup.')
    total=len(raw);out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('database.json',raw)
        for name in names:
            image=photos.get(name)
            if image is None:raise CloudBackendError('A referenced photo is missing; backup was not completed.')
            total+=len(image)
            if total>max_bytes:raise CloudBackendError('Use administrative export for a large backup.')
            z.writestr('photos/'+name,image)
        z.writestr('RESTORE.txt','Private customer data. Cloud backup format. Use scripts/prepare_restore.py to create an import folder. Restore only to a NEW EMPTY D1 database and a NEW private R2 bucket. Follow DEPLOY_AR.md.\n')
    return out.getvalue()
