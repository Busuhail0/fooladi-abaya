# Fooladi Abaya — Cloudflare + GitHub

برنامج إدارة محل ومشغل فولادي للعباية، بالعربية والإنجليزية، مع الاحتفاظ ببايثون وFlask.

**حالة المشروع: مرشح للنشر، غير منشور.** اختبارات منطق التطبيق وطبقة التخزين نجحت محلياً. لم يكتمل فحص البناء المحلي في Workers لأن بيئة الإنشاء تعذر عليها تنزيل حزمة Pyodide من GitHub. يعيد GitHub Actions فحص البناء دون نشر عند رفع الكود. لا تحتوي الملفات على بيانات عملاء أو أسرار تشغيل.

مستودع المشروع: https://github.com/Busuhail0/fooladi-abaya

| المكوّن | دوره |
|---|---|
| GitHub | حفظ الكود وسجل التعديلات واختبارات التحقق |
| Cloudflare Python Workers | تشغيل التطبيق ببايثون وFlask |
| Cloudflare D1 | حفظ العملاء والمقاسات والطلبات والفواتير والدفعات |
| Cloudflare R2 | حفظ صور الموديلات في حاوية خاصة |

ابدأ من [دليل النشر بالعربية](DEPLOY_AR.md). تعليمات تشغيل النسخة السابقة ومتابعة الطلبات موجودة في [دليل الاستخدام](USER_GUIDE.md).

## Version 2 — two distinct workspaces

- **Tailoring orders / طلبات التفصيل:** customer measurements, saved model photo, production stages, invoice, payments and pickup/courier handover.
- **Ready-to-wear / العبايات الجاهزة:** one physical piece per record, model code and saved photo, display date, price, size, customer, paid amount, balance and handover state.
- Stock states are on display, reserved, sold and returned. A reservation becomes a sale explicitly; payment and handover are recorded independently.
- A full return preserves the original sale, clears its receivable and tracks any money still owed back to the customer. Refunds are recorded separately when actually paid. Relisting creates a new availability cycle without overwriting earlier sales.
- Existing orders remain in the all-orders register. New ready sales must use the ready-stock workspace. Apply **all** migrations, including `0002_ready_stock.sql`, before deploying this version.

## Features

- Customer profiles and measurement defaults; independent measurements per order item.
- Model catalogue with saved photos, lookup by model code, and price snapshots.
- Made-to-measure production stages and ready-to-wear sales.
- Store pickup, manual courier/tracking details, and actual handover date.
- Deposits, partial payments, refunds, cancellations, invoices and workshop tickets.
- Search, CSV export and small-dataset cloud backup with images.
- Arabic RTL / English interface, authenticated photo routes and owner-protected setup.

## Validation

```bash
python -m pip install -r requirements-local.txt 'pytest>=8,<10'
python scripts/bundle_assets.py
python -m pytest -q
```

Tests include the original local workflow and the real D1 storage adapter against a SQLite-backed binding fake. Binding fakes validate SQL and business behaviour; they do **not** prove that Cloudflare's deployed runtime behaves identically.

Before live use, complete `uv run pywrangler deploy --dry-run`, apply migrations to an isolated test database, deploy to a test Worker with its own private R2 bucket, and verify login, photo upload/lookup, order creation, final payment, handover, invoice printing and backup. Only then connect production data.

## Architecture notes

- Flask is served through `workers.wsgi.entrypoint(app)`.
- Templates and static CSS/JS are embedded into `bundled_assets.py` at build time; the Worker does not depend on local persistent files.
- Every mutation buffers writes into **one atomic D1 batch**. Its first statement validates a database revision. A concurrent mutation causes the whole batch to roll back with a 409 response. Users refresh and retry; the app does not blindly replay payments.
- This conservative global revision is suited to a small shop/atelier. Busy multi-user installations can replace it with per-order revisions after load testing.
- Reads must precede buffered writes inside each business transaction. The adapter raises on read-after-write to prevent silently incorrect behaviour.
- Order numbers are reserved separately in D1. A failed transaction may leave a harmless numbering gap; it does not leave a half-created order.
- Pyodide `None` maps to JS `undefined`, so SQL NULL parameters explicitly use `pyodide.ffi.jsnull`.
- Images are decoded, resized and re-encoded as JPEG before R2 storage. R2 is private; image retrieval goes through authenticated Flask routes. There is no public R2 URL.
- If an image upload succeeds but its subsequent database mutation conflicts, an unused image can remain in R2. No order references it; no automatic cleanup deletes historical photos.
- Secrets live in Workers secrets. Initial setup requires a separate owner token. Cookies are Secure, HttpOnly and SameSite=Lax, with CSRF protection and database-backed authentication version checks.
- One login account is supported. Employee role separation, VAT calculation, automatic courier/payment integrations and partial returns remain outside this release. Full ready-to-wear returns are supported, including after delivery.

## Important boundaries

Python Workers are currently described as beta in Cloudflare's documentation. A Workers build and staging smoke test are required before treating this package as production-ready. Image processing and password hashing must be measured against the CPU/memory limits of the selected Cloudflare plan. Do not lower password hashing strength merely to fit a plan.

The built-in cloud backup is bounded to 10,000 rows per table, 100 referenced photos and 20 MiB of raw data. For larger installations, use an administrative D1 export and R2 backup. Setup does not enable scheduled backups automatically.

The cloud backup format (`fooladi-cloud-v2`) differs from the desktop ZIP. It includes stock and sale history. `scripts/prepare_restore.py` accepts cloud v1/v2 backups and converts them into SQL and photos for restoration to **new empty resources** after applying all migrations. It never contacts Cloudflare or modifies a live database.

## Official references

- [Flask on Python Workers](https://developers.cloudflare.com/workers/languages/python/packages/flask/)
- [D1 batches and sessions](https://developers.cloudflare.com/d1/worker-api/d1-database/)
- [R2 Workers API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)
- [GitHub integration](https://developers.cloudflare.com/workers/ci-cd/builds/git-integration/github-integration/)
- [Pyodide type conversions](https://pyodide.org/en/stable/usage/type-conversions.html)
