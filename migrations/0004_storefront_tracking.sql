-- Short-lived abuse counters; no customer data and no business records.
CREATE TABLE IF NOT EXISTS storefront_tracking_attempts (
 ip_hash TEXT NOT NULL, lookup_hash TEXT NOT NULL, stamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS storefront_tracking_ip ON storefront_tracking_attempts(ip_hash,stamp);
CREATE INDEX IF NOT EXISTS storefront_tracking_lookup ON storefront_tracking_attempts(lookup_hash,stamp);
