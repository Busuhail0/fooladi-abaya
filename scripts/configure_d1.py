"""Set the non-secret D1 database UUID from an environment variable."""
import json
import os
import uuid
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'wrangler.jsonc'
config=json.loads(path.read_text())
identifier=os.environ.get('FOOLADI_D1_DATABASE_ID','').strip() or config['d1_databases'][0]['database_id']
try:uuid.UUID(identifier)
except ValueError:raise SystemExit('Set FOOLADI_D1_DATABASE_ID to the UUID returned by Cloudflare D1 create.')
config['d1_databases'][0]['database_id']=identifier
path.write_text(json.dumps(config,indent=2)+'\n')
print('D1 binding configured.')
