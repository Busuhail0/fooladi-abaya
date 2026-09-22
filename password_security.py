"""Werkzeug-compatible password hashes on native Python and Python Workers.

Pyodide 3.14 does not ship the OpenSSL-backed hashlib KDFs. In Workers,
delegate to the runtime's native node:crypto implementation inside a request.
Keep the standard hash format so backups remain usable by native Python.
"""
import secrets
import sys

from werkzeug.security import check_password_hash as _werkzeug_check
from werkzeug.security import generate_password_hash as _werkzeug_generate


# OWASP's 32 MiB scrypt configuration; also below Workers' scrypt cost limit.
CLOUD_PASSWORD_METHOD = 'scrypt:32768:8:3'


def _worker_hash(method, salt, password):
    from workers import import_from_javascript

    # Import lazily: JavaScript module imports require a request/JSPI context.
    crypto = import_from_javascript('./password_crypto.mjs')
    parts = method.split(':')
    if len(parts) == 4 and parts[0] == 'scrypt':
        n, r, p = map(int, parts[1:])
        if n < 2 or n & (n - 1) or not 1 <= r <= 8 or not 1 <= p <= 16:
            raise ValueError('Invalid scrypt parameters.')
        if n > 131072 or n * r * p > 1048576:
            raise ValueError('Unsupported scrypt cost.')
        return str(crypto.scryptHex(password, salt, n, r, p))
    if len(parts) == 3 and parts[:2] == ['pbkdf2', 'sha256']:
        # Read the previous release's format without reducing its work factor.
        iterations = int(parts[2])
        if not 1 <= iterations <= 1000000:
            raise ValueError('Unsupported PBKDF2 cost.')
        return str(crypto.pbkdf2Hex(password, salt, iterations))
    raise ValueError('Unsupported password hash method.')


def generate_password_hash(password, method='scrypt', salt_length=16):
    if sys.platform != 'emscripten':
        return _werkzeug_generate(password, method=method, salt_length=salt_length)
    if salt_length < 1:
        raise ValueError('Salt length must be positive.')
    if method == 'scrypt':
        method = 'scrypt:32768:8:1'
    salt = ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
                   for _ in range(salt_length))
    return f'{method}${salt}${_worker_hash(method, salt, password)}'


def check_password_hash(stored, password):
    if sys.platform != 'emscripten':
        return _werkzeug_check(stored, password)
    try:
        method, salt, expected = stored.split('$')
        if not salt or len(salt) > 256 or not expected:
            return False
        # Reject malformed digests before calling the native KDF.
        length = 128 if method.startswith('scrypt:') else 64
        if len(expected) != length or any(c not in '0123456789abcdef' for c in expected):
            return False
        actual = _worker_hash(method, salt, password)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)
