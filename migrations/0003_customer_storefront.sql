
CREATE TABLE IF NOT EXISTS storefront_products (
 model_id INTEGER PRIMARY KEY REFERENCES models(id),
 published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
 custom_available INTEGER NOT NULL DEFAULT 1 CHECK(custom_available IN (0,1)),
 description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS customer_requests (
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, browser_key TEXT NOT NULL,
 ip_hash TEXT NOT NULL, created_epoch INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','contacted','confirmed','closed')),
 kind TEXT NOT NULL CHECK(kind IN ('custom','stock')),
 model_id INTEGER NOT NULL REFERENCES models(id), stock_id TEXT REFERENCES ready_stock(id),
 model_code TEXT NOT NULL, model_title TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '',
 unit_price INTEGER NOT NULL CHECK(unit_price>=0), quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 10),
 measurements TEXT NOT NULL DEFAULT '{}', size TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
 customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('pickup','delivery')),
 delivery_address TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', admin_note TEXT NOT NULL DEFAULT '',
 order_id INTEGER UNIQUE REFERENCES orders(id),
 CHECK((kind='custom' AND stock_id IS NULL) OR (kind='stock' AND stock_id IS NOT NULL AND quantity=1))
);
CREATE INDEX IF NOT EXISTS customer_requests_created ON customer_requests(created_epoch);
CREATE INDEX IF NOT EXISTS customer_requests_rate ON customer_requests(ip_hash,created_epoch);
CREATE INDEX IF NOT EXISTS customer_requests_status ON customer_requests(status,created_at);
