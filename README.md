# Coupon Swap

A Django monolith for coupon exchange and trading.

## Environment setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

SQLite remains the default for local development. To use the PostgreSQL
development container instead, start Docker Desktop and run:

```sh
docker compose up -d db
export DB_ENGINE=postgresql
export DB_NAME=swapstash
export DB_USER=swapstash
export DB_PASSWORD=swapstash-local-only
export DB_HOST=127.0.0.1
export DB_PORT=5432
python manage.py migrate
python manage.py runserver
```

The compose credentials are for local development only and must not be reused in
production. Set the database connection variables through the deployment's
secret manager in production. Stop the local container with
`docker compose down`; persistent database data is stored in the
`postgres_data` volume.

## S3 image storage

Local development uses filesystem storage by default. To store incoming coupon
proof images in S3, install the requirements and configure the bucket and region
through the environment:

```sh
export USE_S3_STORAGE=true
export AWS_STORAGE_BUCKET_NAME=your-private-bucket
export AWS_S3_REGION_NAME=your-aws-region
```

The application stages uploads under `coupons/pending/` in the configured
bucket, runs OCR by opening that stored object, and saves confirmed proofs under
`coupons/proofs/`. S3 access uses the standard boto3 credential chain (for
example, an attached IAM role or a local AWS profile); do not put access keys
in source code. Keep S3 Block Public Access enabled and grant the application
identity only the required bucket permissions. Image URLs are signed rather
than public. Configure an S3 lifecycle rule to expire abandoned objects under
`coupons/pending/`.

For deployment, set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, and
`DJANGO_ALLOWED_HOSTS` to a comma-separated list of hostnames. Uploaded media is
served by Django only in debug mode; production deployments should route media
through a dedicated web server or storage service.

The project is split into three Django apps:

- `accounts` — custom user accounts, authentication, and baseline reputation fields
- `coupons` — coupon listings and OCR-related functionality
- `trades` — trade and escrow flows

Authentication routes are available at `/accounts/signup/`,
`/accounts/login/`, `/accounts/logout/`, and the authenticated
`/accounts/profile/`.

Authenticated users can upload coupons at `/coupons/upload/`. JPEG, PNG, and
WebP proof images are limited to 10 MB. The upload flow scans the image, then
lets the user confirm or manually enter the coupon code and expiry date before
the coupon is saved. Sanitized coupon codes are hashed and constrained to be
unique per owner.

The authenticated trade dashboard is available at `/trades/dashboard/`. It
shows active listings, incoming and outgoing requests, active exchanges, and
resolved trade history. Trade lifecycle events are recorded for participants
and are available to staff as a read-only audit log in Django Admin.

The public marketplace is available at `/coupons/`. It lists available coupons
and supports keyword, category, platform, and expiry-timeline filters. Coupon
codes are not included in marketplace results.

Trade proposals are handled by `trades.services.create_trade(...)`. A proposal
locks only the sender's coupon as `LOCKED_PENDING`; the receiver's coupon remains
available until acceptance. Acceptance atomically locks both coupons, opens a
15-minute verification window, and enables private code disclosure to each
counterparty. Each participant confirms the trade after testing their received
code. When both confirm, the trade is completed and both coupons become
`TRADED`. Either participant can report an invalid code; this immediately marks
the trade `DISPUTED`, quarantines both coupons, increments both dispute counts,
and flags both accounts for administrative review. An invalid-code report does
not automatically change either trust score; administrators assess dispute
history before deciding on further action. The user admin list surfaces
successful and disputed trade counts, the calculated dispute rate, and a
filter for accounts flagged for review. Disputed and mutually abandoned coupons
are quarantined for review.

Run `python manage.py process_expired_trades` from a once-per-minute scheduler
(for example cron: `* * * * * cd /path/to/project && /path/to/project/.venv/bin/python manage.py process_expired_trades`)
to resolve expired verification windows. One-sided
verification completes the trade, rewards the active participant, and applies
a 15-point inactivity penalty to the silent participant. Mutual abandonment
quarantines both coupons and deducts 25 trust points from each participant.
After two inactivity timeouts, the account is flagged for review.
Flagged accounts cannot initiate or accept new trades until staff clear the
review flag in Django Admin.

Image text extraction uses the Python `pytesseract` package and also requires the
Tesseract OCR executable to be installed on the host.
