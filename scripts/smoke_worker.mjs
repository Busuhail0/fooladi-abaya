import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
process.chdir(root);
const modules = ['worker.py','app.py','cloud_storage.py','bundled_assets.py','ready_stock.py',
  'password_security.py']
  .map(name => ({type:'PythonModule', path:path.join(root,name)}));
modules.push({type:'ESModule',path:path.join(root,'password_crypto.mjs')});
for (const name of fs.readdirSync('python_modules',{recursive:true})) {
  const file=path.join(root,'python_modules',name);
  if (fs.statSync(file).isFile() && !name.endsWith('.pyc'))
    modules.push({type:'Data',path:file});
}
const mf = new Miniflare(convertV4MiniflareOptions({modules,modulesRoot:root,
  compatibilityDate:'2026-09-15',compatibilityFlags:['python_workers'],
  bindings:{SESSION_SECRET:'s'.repeat(64),SETUP_TOKEN:'t'.repeat(64),MAINTENANCE:'0'},
  d1Databases:['DB'],r2Buckets:['PHOTOS'],cf:false}));
let cookie='';
async function request(url,data) {
  const r=await mf.dispatchFetch('https://local.test'+url,{method:data?'POST':'GET',
    headers:{cookie,...(data?{'content-type':'application/x-www-form-urlencoded'}:{})},
    ...(data?{body:new URLSearchParams(data).toString()}:{}),redirect:'manual'});
  const set=r.headers.get('set-cookie');if(set)cookie=set.split(';',1)[0];
  const body=await r.text();
  console.log(JSON.stringify({path:url,method:data?'POST':'GET',status:r.status,location:r.headers.get('location')}));
  return {status:r.status,body,location:r.headers.get('location')};
}
function csrf(body) {const match=body.match(/name="csrf" value="([^"]+)"/);if(!match)throw new Error('CSRF field missing');return match[1];}
try {
  const db=await mf.getD1Database('DB');
  const statements=JSON.parse(execFileSync('python',['-c',`
import json, sqlite3
from pathlib import Path
statements=[]
for file in sorted(Path('migrations').glob('*.sql')):
    pending=''
    for char in file.read_text():
        pending+=char
        if char==';' and sqlite3.complete_statement(pending):
            statements.append(pending.strip()); pending=''
    assert not pending.strip()
print(json.dumps(statements))
`],{encoding:'utf8'}));
  await db.batch(statements.map(sql=>db.prepare(sql)));
  const form=await request('/setup');
  if(form.status!==200)throw new Error('Setup page failed: '+form.body.slice(0,300));
  const saved=await request('/setup',{csrf:csrf(form.body),setup_token:'t'.repeat(64),
    username:'testowner',password:'TestOnlyPassword123!',confirm:'TestOnlyPassword123!',shop_name:'Test shop'});
  if(saved.status!==302)throw new Error('Setup failed: '+saved.body.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').slice(-1800));
  const dashboard=await request('/');
  if(dashboard.status!==200)throw new Error('Dashboard failed');
  await request('/logout',{csrf:csrf(dashboard.body)});
  const login=await request('/login');
  const incorrect=await request('/login',{csrf:csrf(login.body),username:'testowner',password:'wrong'});
  if(incorrect.status!==200)throw new Error('Wrong-password handling failed');
  const correct=await request('/login',{csrf:csrf(incorrect.body),username:'testowner',password:'TestOnlyPassword123!'});
  if(correct.status!==302)throw new Error('Login failed');
  if((await request('/')).status!==200)throw new Error('Authenticated dashboard failed');
  const settings=await request('/settings');
  const rotated=await request('/settings/password',{csrf:csrf(settings.body),
    current:'TestOnlyPassword123!',password:'ChangedTestPassword456!',confirm:'ChangedTestPassword456!'});
  if(rotated.status!==302 || rotated.location!=='/login')throw new Error('Password change failed');
  const afterChange=await request('/login');
  const oldPassword=await request('/login',{csrf:csrf(afterChange.body),username:'testowner',password:'TestOnlyPassword123!'});
  if(oldPassword.status!==200)throw new Error('Old password was not rejected');
  const newPassword=await request('/login',{csrf:csrf(oldPassword.body),username:'testowner',password:'ChangedTestPassword456!'});
  if(newPassword.status!==302 || (await request('/')).status!==200)throw new Error('New password failed');
  console.log('Workers runtime: setup, D1 persistence, dashboard, logout, wrong-password rejection, login, and password rotation passed.');
} finally {await mf.dispose();}
