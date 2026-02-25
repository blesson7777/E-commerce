# Nature Nest

Nature Nest is a role-based Django marketplace for eco-friendly products with built-in booking, payment tracking, customer support workflows, and seller fraud-risk monitoring.

## Core Features

- Role-based authentication and dashboards (`Admin`, `Seller`, `Customer`)
- Location hierarchy management (`State`, `District`, `Location`)
- Category and product management with serviceable/non-serviceable location rules
- Cart, checkout, booking lifecycle, and transaction tracking
- Complaints and feedback modules
- Admin analytics dashboards with CSV/PDF exports
- Seller risk monitoring (hybrid anomaly + classification workflow)
- Self-contained UI assets in `static/themes/` (user and admin themes)

## Tech Stack

- Python 3.14 (workspace currently uses `Python 3.14.3`)
- Django
- SQLite (default local DB)
- ReportLab (PDF export in analytics)
- python-docx (table design documentation script)

## Project Apps

- `accounts`: auth, registration, profile, role-aware dashboards
- `locations`: state/district/place management and import tooling
- `catalog`: categories, products, inventory, cart helpers, delivery/restock prediction helpers
- `orders`: bookings, checkout, transactions, cancellation monitoring
- `support`: complaints and feedback
- `analytics`: fraud detection, risk incidents, reporting exports

## Quick Start (Windows / PowerShell)

1. Create and activate virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
python -m pip install --upgrade pip
pip install django reportlab python-docx
```

3. Run migrations:

```powershell
py manage.py migrate
```

4. Create an admin user:

```powershell
py manage.py createsuperuser
```

5. Start development server:

```powershell
py manage.py runserver
```

Open `http://127.0.0.1:8000/`.

## Useful Commands

- Run all tests:

```powershell
py manage.py test
```

- Run seller verification batch:

```powershell
py manage.py run_seller_verification
```

- Simulate marketplace activity with generated data:

```powershell
py manage.py simulate_market_activity --prefix demo --bookings 300
```

- Import India postal location dataset:

```powershell
py manage.py import_india_postal_data
```

## Optional Data Seeding Scripts

- Reset and seed eco catalog (users, categories, products, sample media):

```powershell
py scripts\reset_seed_eco_catalog.py
```

- Refresh product images from Openverse/fallback sources:

```powershell
py scripts\refresh_product_images.py
```

## Key Routes

- `/` home page
- `/accounts/login/` login
- `/accounts/dashboard/` role-aware dashboard
- `/catalog/products/` product listing
- `/orders/list/` booking list
- `/analytics/fraud-detection/` fraud analytics dashboard

Note: this project does not expose Django's default `/admin/` interface; management flows are implemented as custom app pages.

## Directory Overview

```text
config/        Django settings, URL config, shared form/context helpers
accounts/      User model, auth, profile, dashboards
locations/     State/district/location models + import command
catalog/       Category/product/cart/inventory modules
orders/        Booking, checkout, transaction, cancellation modules
support/       Complaints and feedback modules
analytics/     Risk scoring, incidents, model snapshots, exports
templates/     Shared and app templates
static/        Self-contained theme assets
media/         User-uploaded and seeded media files
scripts/       Standalone project utility scripts
docs/          Project diagrams and generated documentation
```

## Configuration Notes

- Current `config/settings.py` is development-oriented (SQLite, debug mode, and hardcoded mail settings).
- Before deployment, move secrets and SMTP credentials to environment variables and set `DEBUG = False`.
- `USE_CDN_ASSETS` can toggle template usage of external CDN assets.

## License

No license file is currently included. Add one if you plan to distribute this project.
