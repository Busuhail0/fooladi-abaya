-- Separate physical ready stock from tailoring orders. Existing records are preserved.

CREATE TABLE IF NOT EXISTS ready_stock (
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
 model_id INTEGER NOT NULL REFERENCES models(id),
 model_code TEXT NOT NULL, model_title TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '',
 display_date TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0),
 size TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','reserved','sold','returned')),
 current_order_id INTEGER UNIQUE REFERENCES orders(id), created_at TEXT NOT NULL,
 CHECK((state='available' AND current_order_id IS NULL) OR (state<>'available' AND current_order_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ready_stock_model ON ready_stock(model_code);
CREATE TABLE IF NOT EXISTS ready_sales (
 order_id INTEGER PRIMARY KEY REFERENCES orders(id), stock_id TEXT NOT NULL REFERENCES ready_stock(id),
 state TEXT NOT NULL CHECK(state IN ('reserved','sold','returned','released')),
 display_date TEXT NOT NULL, sold_at TEXT, closed_at TEXT, reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ready_sales_stock ON ready_sales(stock_id);
