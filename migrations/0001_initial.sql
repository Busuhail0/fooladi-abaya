
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', measurements TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS customer_phone ON customers(phone);
CREATE TABLE IF NOT EXISTS models (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE, title TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0), photo TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL REFERENCES customers(id), customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, customer_address TEXT NOT NULL, created_at TEXT NOT NULL, due_date TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('pickup','delivery')), delivery_address TEXT NOT NULL DEFAULT '', courier TEXT NOT NULL DEFAULT '', tracking TEXT NOT NULL DEFAULT '', shipping_status TEXT NOT NULL DEFAULT 'pending' CHECK(shipping_status IN ('pending','out','delivered')), delivered_at TEXT, discount INTEGER NOT NULL CHECK(discount>=0), delivery_fee INTEGER NOT NULL CHECK(delivery_fee>=0), total INTEGER NOT NULL CHECK(total>=0), notes TEXT NOT NULL DEFAULT '', cancelled INTEGER NOT NULL DEFAULT 0, cancel_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), model_id INTEGER NOT NULL REFERENCES models(id), model_code TEXT NOT NULL, model_title TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100), price INTEGER NOT NULL CHECK(price>=0), kind TEXT NOT NULL CHECK(kind IN ('custom','stock')), stage TEXT NOT NULL CHECK(stage IN ('received','cutting','sewing','finishing','quality','ready')), measurements TEXT NOT NULL, tailor TEXT NOT NULL DEFAULT '', fabric TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS item_order ON items(order_id);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), request_key TEXT NOT NULL UNIQUE, amount INTEGER NOT NULL CHECK(amount<>0), method TEXT NOT NULL CHECK(method IN ('cash','card','transfer','cod')), reference TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS payment_order ON payments(order_id);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), item_id INTEGER REFERENCES items(id), kind TEXT NOT NULL, detail TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, stamp REAL NOT NULL);

CREATE TABLE IF NOT EXISTS cloud_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CONSTRAINT cloud_revision_guard CHECK(revision>=0));
INSERT OR IGNORE INTO cloud_revision(id,revision) VALUES (1,0);
CREATE TABLE IF NOT EXISTS order_numbers(id INTEGER PRIMARY KEY AUTOINCREMENT);
INSERT OR IGNORE INTO order_numbers(id) SELECT MAX(id) FROM orders HAVING MAX(id) IS NOT NULL;
CREATE INDEX IF NOT EXISTS login_attempt_ip_stamp ON login_attempts(ip,stamp);
CREATE INDEX IF NOT EXISTS event_order ON events(order_id);
CREATE INDEX IF NOT EXISTS order_due ON orders(due_date);
