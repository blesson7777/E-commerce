import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import django
from django.core.files.base import ContentFile
from django.db import transaction


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from accounts.models import CustomerAddress  # noqa: E402
from accounts.models import SellerProfile  # noqa: E402
from accounts.models import User  # noqa: E402
from analytics.models import SellerRiskIncident  # noqa: E402
from analytics.models import SellerRiskSnapshot  # noqa: E402
from catalog.models import Category  # noqa: E402
from catalog.models import Product  # noqa: E402
from django.conf import settings  # noqa: E402
from locations.models import District  # noqa: E402
from locations.models import Location  # noqa: E402
from locations.models import State  # noqa: E402
from orders.models import Booking  # noqa: E402
from orders.models import BookingItem  # noqa: E402
from orders.models import Transaction  # noqa: E402
from support.models import Complaint  # noqa: E402
from support.models import Feedback  # noqa: E402


USER_AGENT = 'NatureNestSeeder/1.0 (academic project setup)'
DEFAULT_SELLER_PASSWORD = 'Seller@12345'
DEFAULT_ADMIN_PASSWORD = 'Admin@12345'


CATEGORY_DATA = {
    'Reusable Kitchen Essentials': 'Durable kitchen alternatives that reduce single-use waste.',
    'Plastic-Free Personal Care': 'Daily care items designed without disposable plastic packaging.',
    'Sustainable Home Cleaning': 'Plant-based cleaning products with refill-ready formats.',
    'Organic Wellness Pantry': 'Natural food and wellness essentials sourced responsibly.',
    'Eco Fashion & Accessories': 'Fashion and accessories made from low-impact materials.',
}


SELLER_DATA = [
    ('greensprout.seller@naturenest.local', 'GreenSprout', 'Essentials', 'GreenSprout Essentials'),
    ('earthwise.seller@naturenest.local', 'Earthwise', 'Living', 'Earthwise Living'),
    ('ecocraft.seller@naturenest.local', 'EcoCraft', 'Market', 'EcoCraft Market'),
    ('pureleaf.seller@naturenest.local', 'PureLeaf', 'Goods', 'PureLeaf Goods'),
    ('terracycle.seller@naturenest.local', 'TerraCycle', 'Hub', 'TerraCycle Hub'),
]


# (name, category, price, stock, weight, size, image_query)
PRODUCT_DATA = [
    ('Bamboo Cutlery Travel Kit', 'Reusable Kitchen Essentials', '12.99', 90, '0.35', 'Set of 5', 'bamboo cutlery set'),
    ('Stainless Steel Lunch Box', 'Reusable Kitchen Essentials', '24.50', 70, '0.80', '1200 ml', 'stainless steel lunch box'),
    ('Beeswax Food Wrap Set', 'Reusable Kitchen Essentials', '14.75', 100, '0.20', 'Pack of 3', 'beeswax food wrap'),
    ('Coconut Shell Serving Bowl', 'Reusable Kitchen Essentials', '11.40', 85, '0.25', 'Medium', 'coconut shell bowl'),
    ('Glass Spice Jar Refill Pack', 'Reusable Kitchen Essentials', '19.90', 65, '0.95', 'Pack of 6', 'glass spice jars kitchen'),
    ('Recycled Cotton Dish Cloths', 'Reusable Kitchen Essentials', '9.50', 120, '0.18', 'Pack of 4', 'recycled cotton kitchen cloth'),
    ('Compostable Scrub Sponge Set', 'Reusable Kitchen Essentials', '8.60', 140, '0.12', 'Pack of 5', 'compostable kitchen sponge'),
    ('Silicone Reusable Food Bag', 'Reusable Kitchen Essentials', '13.20', 95, '0.22', '1000 ml', 'reusable silicone food bag'),
    ('Wooden Cooking Spoon Trio', 'Reusable Kitchen Essentials', '10.80', 88, '0.30', 'Set of 3', 'wooden cooking spoon set'),
    ('Natural Fiber Dish Brush', 'Reusable Kitchen Essentials', '7.40', 130, '0.16', 'Standard', 'natural dish brush'),
    ('Bamboo Toothbrush Duo', 'Plastic-Free Personal Care', '6.99', 200, '0.08', 'Pack of 2', 'bamboo toothbrush'),
    ('Shampoo Bar Citrus Fresh', 'Plastic-Free Personal Care', '11.20', 150, '0.10', '100 g', 'shampoo bar'),
    ('Conditioner Bar Herbal Care', 'Plastic-Free Personal Care', '12.10', 130, '0.10', '90 g', 'conditioner bar natural'),
    ('Refillable Aluminum Deodorant', 'Plastic-Free Personal Care', '15.80', 110, '0.12', '75 ml', 'refillable deodorant'),
    ('Organic Cotton Makeup Pads', 'Plastic-Free Personal Care', '10.25', 140, '0.15', 'Pack of 10', 'organic cotton makeup pads'),
    ('Biodegradable Dental Floss', 'Plastic-Free Personal Care', '5.95', 220, '0.05', '30 m', 'biodegradable dental floss'),
    ('Neem Wood Comb', 'Plastic-Free Personal Care', '7.30', 170, '0.07', 'Pocket', 'wooden neem comb'),
    ('Plastic-Free Shaving Kit', 'Plastic-Free Personal Care', '28.90', 75, '0.45', 'Starter kit', 'safety razor kit'),
    ('Aloe Vera Soap Bar Pack', 'Plastic-Free Personal Care', '9.80', 165, '0.24', 'Pack of 3', 'natural soap bars'),
    ('Reusable Safety Razor Blades', 'Plastic-Free Personal Care', '6.40', 210, '0.04', 'Pack of 20', 'safety razor blades'),
    ('Plant-Based Laundry Liquid', 'Sustainable Home Cleaning', '17.90', 100, '1.20', '1 L', 'plant based laundry detergent'),
    ('Citrus Floor Cleaner Concentrate', 'Sustainable Home Cleaning', '13.60', 125, '0.75', '500 ml', 'eco floor cleaner'),
    ('Refillable All-Purpose Cleaner', 'Sustainable Home Cleaning', '12.95', 115, '0.70', '500 ml', 'all purpose cleaner eco'),
    ('Compostable Garbage Bag Roll', 'Sustainable Home Cleaning', '9.40', 190, '0.30', '24 bags', 'compostable garbage bags'),
    ('Natural Air Freshener Spray', 'Sustainable Home Cleaning', '8.75', 145, '0.26', '250 ml', 'natural air freshener'),
    ('Reusable Cleaning Cloth Set', 'Sustainable Home Cleaning', '10.30', 160, '0.18', 'Pack of 6', 'reusable cleaning cloth'),
    ('Eco Toilet Cleaner Tablets', 'Sustainable Home Cleaning', '11.50', 135, '0.14', 'Pack of 12', 'toilet cleaner tablets'),
    ('Refill Dishwashing Liquid', 'Sustainable Home Cleaning', '8.90', 180, '0.95', '900 ml', 'eco dishwashing liquid'),
    ('Bio Enzyme Bathroom Cleaner', 'Sustainable Home Cleaning', '14.20', 105, '0.80', '750 ml', 'bio enzyme cleaner'),
    ('Vinegar Glass Cleaner Refill', 'Sustainable Home Cleaning', '9.10', 150, '0.65', '500 ml', 'vinegar glass cleaner'),
    ('Organic Green Tea Blend', 'Organic Wellness Pantry', '10.90', 145, '0.12', '100 g', 'organic green tea'),
    ('Fair Trade Coffee Beans', 'Organic Wellness Pantry', '18.40', 110, '0.50', '500 g', 'fair trade coffee beans'),
    ('Herbal Immunity Kadha Mix', 'Organic Wellness Pantry', '12.35', 120, '0.20', '200 g', 'herbal kadha mix'),
    ('Raw Forest Honey Jar', 'Organic Wellness Pantry', '16.80', 95, '0.55', '500 g', 'raw honey jar'),
    ('Cold Pressed Coconut Oil', 'Organic Wellness Pantry', '14.90', 100, '0.95', '1 L', 'cold pressed coconut oil'),
    ('Millet Breakfast Granola', 'Organic Wellness Pantry', '11.75', 130, '0.35', '350 g', 'millet granola'),
    ('Turmeric Ginger Wellness Powder', 'Organic Wellness Pantry', '9.95', 140, '0.18', '180 g', 'turmeric powder organic'),
    ('Organic Jaggery Cubes', 'Organic Wellness Pantry', '7.90', 170, '0.50', '500 g', 'organic jaggery cubes'),
    ('Natural Peanut Butter', 'Organic Wellness Pantry', '13.55', 115, '0.40', '400 g', 'natural peanut butter jar'),
    ('Vegan Protein Seed Mix', 'Organic Wellness Pantry', '15.25', 105, '0.30', '300 g', 'seed mix healthy food'),
    ('Hemp Tote Bag Urban', 'Eco Fashion & Accessories', '19.60', 90, '0.28', 'Large', 'hemp tote bag'),
    ('Cork Wallet Classic', 'Eco Fashion & Accessories', '22.70', 80, '0.16', 'Bi-fold', 'cork wallet'),
    ('Bamboo Sunglasses', 'Eco Fashion & Accessories', '27.40', 75, '0.14', 'Unisex', 'bamboo sunglasses'),
    ('Recycled Fabric Backpack', 'Eco Fashion & Accessories', '35.90', 65, '0.70', '18 L', 'recycled fabric backpack'),
    ('Organic Cotton T-Shirt', 'Eco Fashion & Accessories', '21.30', 120, '0.22', 'M', 'organic cotton t shirt'),
    ('Jute Yoga Mat Bag', 'Eco Fashion & Accessories', '18.20', 95, '0.34', 'Standard', 'jute yoga mat bag'),
    ('Upcycled Denim Pouch', 'Eco Fashion & Accessories', '12.80', 130, '0.12', 'Small', 'upcycled denim pouch'),
    ('Cork Key Holder', 'Eco Fashion & Accessories', '8.50', 180, '0.06', 'Compact', 'cork key holder'),
    ('Recycled PET Cap', 'Eco Fashion & Accessories', '11.90', 140, '0.09', 'Adjustable', 'recycled cap'),
    ('Hemp Laptop Sleeve', 'Eco Fashion & Accessories', '29.90', 70, '0.38', '15 inch', 'hemp laptop sleeve'),
]


def slugify_ascii(value):
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    return value.strip('-') or 'item'


def fetch_wikimedia_image_bytes(search_text):
    search_terms = [
        search_text,
        f'eco friendly {search_text}',
        'sustainable household product',
        'zero waste product',
    ]

    for term in search_terms:
        params = {
            'action': 'query',
            'format': 'json',
            'generator': 'search',
            'gsrsearch': f'filetype:bitmap {term}',
            'gsrnamespace': '6',
            'gsrlimit': '20',
            'prop': 'imageinfo',
            'iiprop': 'url',
            'iiurlwidth': '1200',
        }
        api_url = 'https://commons.wikimedia.org/w/api.php?' + urlencode(params)
        try:
            req = Request(api_url, headers={'User-Agent': USER_AGENT})
            with urlopen(req, timeout=45) as resp:
                data = json.load(resp)
        except Exception:
            continue

        pages = data.get('query', {}).get('pages', {})
        for page in pages.values():
            imageinfo = (page.get('imageinfo') or [{}])[0]
            candidate_url = imageinfo.get('thumburl') or imageinfo.get('url') or ''
            base_url = candidate_url.split('?', 1)[0].lower()
            if not base_url.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                continue
            try:
                image_req = Request(candidate_url, headers={'User-Agent': USER_AGENT})
                with urlopen(image_req, timeout=60) as img_resp:
                    image_bytes = img_resp.read()
                if len(image_bytes) < 8192:
                    continue
            except Exception:
                continue
            ext = Path(base_url).suffix.lower().replace('.', '')
            if ext == 'jpeg':
                ext = 'jpg'
            return image_bytes, ext
    return None, None


def fetch_fallback_image():
    fallback_urls = [
        'https://picsum.photos/seed/eco-market/1200/800',
        'https://picsum.photos/seed/green-products/1200/800',
    ]
    for url in fallback_urls:
        try:
            req = Request(url, headers={'User-Agent': USER_AGENT})
            with urlopen(req, timeout=45) as resp:
                data = resp.read()
                final_url = resp.geturl().split('?', 1)[0].lower()
            if len(data) < 8192:
                continue
            ext = Path(final_url).suffix.lower().replace('.', '') or 'jpg'
            if ext == 'jpeg':
                ext = 'jpg'
            if ext not in {'jpg', 'png', 'webp'}:
                ext = 'jpg'
            return data, ext
        except Exception:
            continue
    return None, None


def main():
    if len(PRODUCT_DATA) != 50:
        raise RuntimeError('PRODUCT_DATA must contain exactly 50 products.')

    state, _ = State.objects.get_or_create(
        name='Kerala Eco Region',
        defaults={'code': 'KER-ECO', 'is_active': True},
    )
    district, _ = District.objects.get_or_create(
        state=state,
        name='Kochi Eco District',
        defaults={'is_active': True},
    )
    location, _ = Location.objects.get_or_create(
        district=district,
        name='Edappally Green Hub',
        postal_code='682024',
        defaults={'is_active': True},
    )
    if not state.is_active:
        state.is_active = True
        state.save(update_fields=['is_active'])
    if not district.is_active:
        district.is_active = True
        district.save(update_fields=['is_active'])
    if not location.is_active:
        location.is_active = True
        location.save(update_fields=['is_active'])

    media_dir = Path(settings.MEDIA_ROOT) / 'product_photos'
    media_dir.mkdir(parents=True, exist_ok=True)
    for old_file in media_dir.glob('*'):
        if old_file.is_file():
            old_file.unlink()

    with transaction.atomic():
        Feedback.objects.all().delete()
        Complaint.objects.all().delete()
        Transaction.objects.all().delete()
        BookingItem.objects.all().delete()
        Booking.objects.all().delete()
        Product.objects.all().delete()
        Category.objects.all().delete()
        SellerRiskIncident.objects.all().delete()
        SellerRiskSnapshot.objects.all().delete()
        CustomerAddress.objects.all().delete()

        if not User.objects.filter(is_superuser=True).exists():
            User.objects.create_superuser(
                email='admin@naturenest.local',
                password=DEFAULT_ADMIN_PASSWORD,
                first_name='Nature',
                last_name='Admin',
            )

        User.objects.filter(is_superuser=False).delete()

        categories = {}
        for name, description in CATEGORY_DATA.items():
            categories[name] = Category.objects.create(
                name=name,
                description=description,
                is_active=True,
            )

        sellers = []
        for email, first_name, last_name, store_name in SELLER_DATA:
            seller = User.objects.create_user(
                email=email,
                password=DEFAULT_SELLER_PASSWORD,
                role=User.UserRole.SELLER,
                first_name=first_name,
                last_name=last_name,
                phone_number='+911234567890',
                is_email_verified=True,
            )
            profile = SellerProfile.objects.get(user=seller)
            profile.store_name = store_name
            profile.verification_status = SellerProfile.VerificationStatus.VERIFIED
            profile.is_suspended = False
            profile.suspension_note = ''
            profile.save(
                update_fields=['store_name', 'verification_status', 'is_suspended', 'suspension_note']
            )
            sellers.append(seller)

        fallback_bytes, fallback_ext = fetch_fallback_image()
        if not fallback_bytes:
            raise RuntimeError('Could not download fallback image.')

        for idx, product_data in enumerate(PRODUCT_DATA, start=1):
            name, category_name, price, stock, weight, size, query = product_data
            seller = sellers[(idx - 1) // 10]
            description = (
                f'{name} is an eco-friendly choice crafted for low-waste living. '
                f'Made with sustainable materials and designed for long-term reuse, '
                f'it helps reduce environmental impact in day-to-day life.'
            )

            image_bytes, image_ext = fetch_wikimedia_image_bytes(query)
            if not image_bytes:
                image_bytes, image_ext = fallback_bytes, fallback_ext

            product = Product(
                seller=seller,
                category=categories[category_name],
                location=location,
                name=name,
                description=description,
                price=Decimal(price),
                stock_quantity=int(stock),
                weight=Decimal(weight),
                size=size,
                is_active=True,
            )
            filename = f'{idx:02d}-{slugify_ascii(name)}.{image_ext}'
            product.photo.save(filename, ContentFile(image_bytes), save=False)
            product.save()
            product.serviceable_states.set([state])
            product.serviceable_districts.set([district])
            product.serviceable_locations.set([location])

    summary = {
        'superusers': User.objects.filter(is_superuser=True).count(),
        'sellers': User.objects.filter(role=User.UserRole.SELLER).count(),
        'customers': User.objects.filter(role=User.UserRole.CUSTOMER).count(),
        'categories': Category.objects.count(),
        'products': Product.objects.count(),
        'bookings': Booking.objects.count(),
        'transactions': Transaction.objects.count(),
        'feedback': Feedback.objects.count(),
        'complaints': Complaint.objects.count(),
    }
    print(json.dumps(summary, indent=2))
    print(f'Default seller password: {DEFAULT_SELLER_PASSWORD}')
    if User.objects.filter(email='admin@naturenest.local', is_superuser=True).exists():
        print(f'Created admin password: {DEFAULT_ADMIN_PASSWORD}')
    print('Seeded sellers:')
    for email, _, _, store_name in SELLER_DATA:
        print(f' - {email} ({store_name})')


if __name__ == '__main__':
    main()
