# NatureNest E-commerce

NatureNest is a Django-based e-commerce platform with role-based workflows for customers, sellers, and admins. It includes catalog and order management, location-aware delivery support, customer support flows, and seller risk analytics.

## Features

- Authentication with custom user model (`admin`, `seller`, `customer`) and email-based login.
- Customer and seller onboarding with profile/address management.
- Product catalog, category management, cart operations, and checkout flow.
- Booking/order lifecycle with payment tracking, cancellations, and delivery status updates.
- Seller inventory operations with restock and delivery prediction helpers.
- Support module for complaints and product feedback.
- Analytics module for seller verification, fraud/risk incident handling, and CSV/PDF report exports.
- India location hierarchy (state, district, pincode-level places) with import command support.

## Tech Stack

- Python 3.10+
- Django
- SQLite (default local database)
- ReportLab (PDF export generation)

## Project Structure

- `accounts/` - user model, auth flows, dashboards, profile/address management
- `catalog/` - categories, products, cart, seller inventory, prediction helpers
- `orders/` - bookings, checkout/payments, cancellation and delivery flows
- `support/` - complaints and feedback workflows
- `analytics/` - fraud/risk scoring, verification, dashboards, exports
- `locations/` - state/district/location models and import utilities
- `config/` - project settings, URL routing, shared context processors
- `templates/` and `static/` - UI templates and assets
- `scripts/` - seed and media-refresh scripts for demo data

## Local Setup (PowerShell)

```powershell
# 1) Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) Install dependencies
pip install django reportlab

# 3) Apply migrations
python manage.py migrate

# 4) (Optional) Seed demo catalog/users/data
python scripts/reset_seed_eco_catalog.py

# 5) Run development server
python manage.py runserver
```

Open `http://127.0.0.1:8000/` after starting the server.

## Useful Commands

```powershell
# Run test suite
python manage.py test

# Run seller verification batch
python manage.py run_seller_verification

# Simulate market activity for dashboards
python manage.py simulate_market_activity

# Import India postal dataset (remote source)
python manage.py import_india_postal_data

# Refresh product images from Openverse
python scripts/refresh_product_images.py
```

## Seed Script Notes

- `scripts/reset_seed_eco_catalog.py` resets and reseeds major domain tables.
- It creates demo sellers/products and may download product images from online sources.
- Use it only in local/dev environments.

## Security Notes

Current `config/settings.py` is configured for local development (`DEBUG=True`) and includes hardcoded sensitive values. Before deploying or sharing publicly:

- Move `SECRET_KEY` and email credentials to environment variables.
- Set `DEBUG=False`.
- Restrict `ALLOWED_HOSTS` to trusted domains.
- Use a production database and proper static/media serving.

## Development Notes

- Default DB is `db.sqlite3`.
- Uploaded files are stored in `media/`.
- `AUTH_USER_MODEL` is `accounts.User`.

