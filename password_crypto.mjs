import { pbkdf2Sync, scryptSync } from 'node:crypto';

// Return strings across the Python FFI: Pyodide cannot proxy Node Buffer safely.
export function scryptHex(password, salt, n, r, p) {
  return scryptSync(password, salt, 64, {
    N: n, r, p, maxmem: 132 * n * r * p,
  }).toString('hex');
}

export function pbkdf2Hex(password, salt, iterations) {
  return pbkdf2Sync(password, salt, iterations, 32, 'sha256').toString('hex');
}
