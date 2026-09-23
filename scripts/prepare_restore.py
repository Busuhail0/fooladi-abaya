"""Convert a trusted Fooladi cloud backup into SQL and photos for a NEW database.

This script never contacts Cloudflare or changes a live database.
"""
import argparse
import json
import re
import sqlite3
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TABLES=('settings','customers','models','orders','items','payments','events','login_attempts','order_numbers','ready_stock','ready_sales','storefront_products','customer_requests')
parser=argparse.ArgumentParser(description='Prepare a Fooladi cloud backup for restoration to empty resources.')
parser.add_argument('backup',type=Path)
parser.add_argument('--output',type=Path,default=ROOT/'restore-output')
args=parser.parse_args()
if args.output.exists():raise SystemExit('Output directory already exists; choose a new folder.')
with zipfile.ZipFile(args.backup) as archive:
    if sum(info.file_size for info in archive.infolist())>30*1024*1024:raise SystemExit('Backup is too large for this helper.')
    content=json.loads(archive.read('database.json'))
    if content.get('format') not in ('fooladi-cloud-v1','fooladi-cloud-v2','fooladi-cloud-v3'):raise SystemExit('Unsupported backup format.')
    if content['format']=='fooladi-cloud-v1':
        content['tables'].setdefault('ready_stock',[])
        content['tables'].setdefault('ready_sales',[])
    content['tables'].setdefault('storefront_products',[])
    content['tables'].setdefault('customer_requests',[])
    db=sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    for migration in sorted((ROOT/'migrations').glob('*.sql')):
        db.executescript(migration.read_text())
    with db:
        for table in TABLES:
            columns=[row[1] for row in db.execute(f'PRAGMA table_info({table})')]
            placeholders=','.join('?' for _ in columns)
            for row in content['tables'][table]:
                if set(row)!=set(columns):raise SystemExit(f'Unexpected columns in {table}.')
                db.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({placeholders})',[row[c] for c in columns])
    if db.execute('PRAGMA foreign_key_check').fetchall():raise SystemExit('Backup has invalid references.')
    # Dump tables in foreign-key order; omit generated sqlite_sequence statements.
    sql=['-- Restore to a NEW EMPTY D1 database after applying ALL migrations.']
    for table in TABLES:
        columns=[row[1] for row in db.execute(f'PRAGMA table_info({table})')]
        for row in db.execute(f'SELECT * FROM {table}'):
            quoted=[db.execute('SELECT quote(?)',(value,)).fetchone()[0] for value in row]
            sql.append(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join(quoted)});')
    names={row[0] for table in ('models','items','ready_stock','customer_requests') for row in db.execute(f"SELECT photo FROM {table} WHERE photo<>''")}
    photos={}
    for name in names:
        if not re.fullmatch(r'[0-9a-f]{32}\.jpg',name):raise SystemExit('Invalid photo reference in backup.')
        photos[name]=archive.read('photos/'+name)
    args.output.mkdir(parents=True)
    (args.output/'photos').mkdir()
    (args.output/'database.sql').write_text('\n'.join(sql)+'\n',encoding='utf-8')
    for name,payload in photos.items():(args.output/'photos'/name).write_bytes(payload)
print('Restore files prepared. No cloud resources were changed.')
