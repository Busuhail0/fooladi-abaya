"""Create untracked development secrets without printing their values."""
import os
import secrets
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'.dev.vars'
try:
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
except FileExistsError:
    raise SystemExit('.dev.vars already exists; kept existing values.')
with os.fdopen(fd,'w') as f:
    f.write('SESSION_SECRET='+secrets.token_urlsafe(48)+'\n')
    f.write('SETUP_TOKEN='+secrets.token_urlsafe(48)+'\n')
print('Created .dev.vars for local development. Open it privately for the owner setup token. Do not commit it.')
