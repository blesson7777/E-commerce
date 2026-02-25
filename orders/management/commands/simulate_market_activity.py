from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import random
import string
import uuid

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import SellerProfile
from accounts.models import User
from analytics.services import calculate_seller_risk_batch
from catalog.models import Category
from catalog.models import Product
from locations.models import District
from locations.models import Location
from locations.models import State
from orders.models import Booking
from orders.models import BookingItem
from orders.models import Transaction as PaymentTransaction
from support.models import Complaint
from support.models import Feedback


PASSWORD_DEFAULT = 'Pass@12345'

FIRST_NAME_POOL = [
    'Aarav', 'Aisha', 'Amelia', 'Arjun', 'Charlotte', 'Dev', 'Diya', 'Ethan',
    'Eva', 'Harper', 'Isha', 'Isla', 'Kabir', 'Liam', 'Maya', 'Meera', 'Mia',
    'Noah', 'Oliver', 'Priya', 'Riya', 'Saanvi', 'Sophia', 'Tara', 'Vihaan',
    'William', 'Yash', 'Zara',
]

LAST_NAME_POOL = [
    'Anderson', 'Bennett', 'Brooks', 'Carter', 'Collins', 'Davis', 'Foster',
    'Garcia', 'Gupta', 'Harris', 'Iyer', 'Jain', 'Kapoor', 'Mehta', 'Miller',
    'Nair', 'Patel', 'Reed', 'Shah', 'Singh', 'Taylor', 'Varma', 'Walker',
    'Wilson', 'Young',
]

EMAIL_WORD_POOL = [
    'oak', 'river', 'meadow', 'harbor', 'cedar', 'spruce', 'lotus', 'coral',
    'maple', 'bloom', 'grove', 'summit', 'morning', 'forest', 'sunrise',
]


class Command(BaseCommand):
    help = (
        'Create demo users/sellers/products/bookings/transactions to simulate ongoing market '
        'activity with a small cancellation ratio.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--prefix', default='demo', help='Email/name prefix for generated data.')
        parser.add_argument('--seed', type=int, default=20260215, help='Random seed for reproducible generation.')
        parser.add_argument('--sellers', type=int, default=12, help='Number of seller users.')
        parser.add_argument(
            '--use-existing-sellers',
            action='store_true',
            help='Use all existing seller accounts instead of creating new seller users.',
        )
        parser.add_argument('--customers', type=int, default=90, help='Number of customer users.')
        parser.add_argument('--products-per-seller', type=int, default=8, help='Products per seller.')
        parser.add_argument('--bookings', type=int, default=420, help='Total bookings to generate.')
        parser.add_argument(
            '--cancel-rate',
            type=float,
            default=0.08,
            help='Cancellation probability (0 to 1). Keep low for "few cancellations".',
        )
        parser.add_argument(
            '--skip-risk',
            action='store_true',
            help='Skip running fraud scoring batch after data generation.',
        )

    def handle(self, *args, **options):
        rng = random.Random(options['seed'])
        prefix = (options['prefix'] or 'demo').strip().lower().replace(' ', '-')
        use_existing_sellers = bool(options['use_existing_sellers'])
        seller_target = max(1, int(options['sellers']))
        customer_target = max(1, int(options['customers']))
        products_per_seller = max(1, int(options['products_per_seller']))
        booking_target = max(1, int(options['bookings']))
        cancel_rate = min(0.45, max(0.0, float(options['cancel_rate'])))

        self.stdout.write(
            self.style.NOTICE(
                f'Simulating market activity prefix="{prefix}" sellers='
                f'{"existing" if use_existing_sellers else seller_target} '
                f'customers={customer_target} products/seller={products_per_seller} '
                f'bookings={booking_target} cancel_rate={cancel_rate:.2f}'
            )
        )

        with transaction.atomic():
            locations = self._ensure_locations(prefix=prefix, rng=rng)
            categories = self._ensure_categories(prefix=prefix)
            if use_existing_sellers:
                sellers = self._get_existing_sellers()
            else:
                sellers = self._ensure_users(
                    role=User.UserRole.SELLER,
                    count=seller_target,
                    prefix=prefix,
                    rng=rng,
                )
            customers = self._ensure_customers(count=customer_target, rng=rng)
            products_by_seller = self._ensure_products(
                sellers=sellers,
                categories=categories,
                locations=locations,
                products_per_seller=products_per_seller,
                prefix=prefix,
                rng=rng,
            )
            summary = self._create_activity(
                sellers=sellers,
                customers=customers,
                products_by_seller=products_by_seller,
                locations=locations,
                booking_target=booking_target,
                cancel_rate=cancel_rate,
                rng=rng,
            )

        if not options['skip_risk']:
            calculate_seller_risk_batch(sellers=sellers)
            self.stdout.write(self.style.SUCCESS('Fraud scoring batch executed for generated sellers.'))

        self.stdout.write(self.style.SUCCESS('Demo simulation completed.'))
        self.stdout.write(
            (
                f'Created/updated sellers={len(sellers)} customers={len(customers)} '
                f'products={summary["products"]} bookings={summary["bookings"]} '
                f'transactions={summary["transactions"]} cancellations={summary["cancellations"]} '
                f'complaints={summary["complaints"]} feedback={summary["feedback"]}'
            )
        )
        self.stdout.write(
            self.style.NOTICE(
                f'Login password for generated users: {PASSWORD_DEFAULT}'
            )
        )

    def _get_existing_sellers(self):
        sellers = list(User.objects.filter(role=User.UserRole.SELLER).order_by('id'))
        if not sellers:
            raise ValueError('No seller accounts exist. Create sellers first or run without --use-existing-sellers.')

        for index, seller in enumerate(sellers, start=1):
            profile, _ = SellerProfile.objects.get_or_create(
                user=seller,
                defaults={'store_name': f'Demo Store {index:03d}'},
            )
            if not profile.store_name:
                profile.store_name = f'Demo Store {index:03d}'
            profile.verification_status = SellerProfile.VerificationStatus.VERIFIED
            profile.is_suspended = False
            profile.suspension_note = ''
            profile.save(
                update_fields=[
                    'store_name',
                    'verification_status',
                    'is_suspended',
                    'suspension_note',
                    'updated_at',
                ]
            )
        return sellers

    def _ensure_locations(self, prefix, rng):
        state_names = [
            f'{prefix.title()} State A',
            f'{prefix.title()} State B',
            f'{prefix.title()} State C',
        ]
        district_roots = ['North District', 'Central District', 'South District']
        location_roots = ['Green Park', 'River View', 'Eco Hills', 'Market Town']

        locations = []
        base_postal = rng.randint(500000, 799999)
        sequence = 0
        for state_index, state_name in enumerate(state_names, start=1):
            state, _ = State.objects.get_or_create(
                name=state_name,
                defaults={'code': f'{prefix[:2].upper()}{state_index}'},
            )
            for district_name in district_roots:
                district, _ = District.objects.get_or_create(
                    state=state,
                    name=f'{district_name} {state_index}',
                )
                for location_name in location_roots:
                    sequence += 1
                    postal_code = str(base_postal + sequence)
                    location, _ = Location.objects.get_or_create(
                        district=district,
                        name=f'{location_name} {state_index}',
                        postal_code=postal_code,
                    )
                    locations.append(location)
        return locations

    def _ensure_categories(self, prefix):
        category_names = [
            f'{prefix.title()} Home Care',
            f'{prefix.title()} Organic Grocery',
            f'{prefix.title()} Reusable Essentials',
            f'{prefix.title()} Herbal Wellness',
            f'{prefix.title()} Eco Kids',
            f'{prefix.title()} Garden & Plants',
        ]
        categories = []
        for name in category_names:
            category, _ = Category.objects.get_or_create(
                name=name,
                defaults={'description': f'Demo category for {prefix} simulation.', 'is_active': True},
            )
            if not category.is_active:
                category.is_active = True
                category.save(update_fields=['is_active'])
            categories.append(category)
        return categories

    def _ensure_users(self, role, count, prefix, rng):
        users = []
        for index in range(1, count + 1):
            email = f'{prefix}.{role}{index:03d}@example.com'
            first_name = f'{role.title()}{index}'
            last_name = f'{prefix.title()}'
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(
                    email=email,
                    password=PASSWORD_DEFAULT,
                    role=role,
                    first_name=first_name,
                    last_name=last_name,
                    phone_number=f'+91000{rng.randint(100000, 999999)}',
                )
            else:
                changed = False
                if user.role != role:
                    user.role = role
                    changed = True
                if not user.first_name:
                    user.first_name = first_name
                    changed = True
                if not user.last_name:
                    user.last_name = last_name
                    changed = True
                if changed:
                    user.save(update_fields=['role', 'first_name', 'last_name', 'updated_at'])

            if role == User.UserRole.SELLER:
                profile, _ = SellerProfile.objects.get_or_create(
                    user=user,
                    defaults={'store_name': f'{prefix.title()} Store {index:03d}'},
                )
                profile.store_name = profile.store_name or f'{prefix.title()} Store {index:03d}'
                profile.verification_status = SellerProfile.VerificationStatus.VERIFIED
                profile.is_suspended = False
                profile.suspension_note = ''
                profile.save(
                    update_fields=[
                        'store_name',
                        'verification_status',
                        'is_suspended',
                        'suspension_note',
                        'updated_at',
                    ]
                )
            users.append(user)
        return users

    def _generate_customer_identity(self, rng, used_local_parts):
        for _ in range(60):
            first_name = rng.choice(FIRST_NAME_POOL)
            last_name = rng.choice(LAST_NAME_POOL)
            local_part = f'{first_name}.{last_name}'.lower()
            if rng.random() < 0.55:
                local_part = f'{local_part}.{rng.choice(EMAIL_WORD_POOL)}'
            local_part = local_part.replace(' ', '').replace("'", '')
            local_part = ''.join(ch for ch in local_part if ch.isalpha() or ch == '.')
            if not local_part or local_part in used_local_parts:
                continue
            used_local_parts.add(local_part)
            return first_name, last_name, f'{local_part}@example.com'

        # Rare fallback for uniqueness collisions: still alpha-only, no numbers.
        token = ''.join(rng.choice(string.ascii_lowercase) for _ in range(8))
        local_part = f'{rng.choice(EMAIL_WORD_POOL)}.{token}'
        used_local_parts.add(local_part)
        return rng.choice(FIRST_NAME_POOL), rng.choice(LAST_NAME_POOL), f'{local_part}@example.com'

    def _ensure_customers(self, count, rng):
        users = []
        existing_locals = {
            email.split('@', 1)[0]
            for email in User.objects.filter(email__iendswith='@example.com').values_list('email', flat=True)
        }

        for _ in range(count):
            first_name, last_name, email = self._generate_customer_identity(rng, existing_locals)
            user = User.objects.create_user(
                email=email,
                password=PASSWORD_DEFAULT,
                role=User.UserRole.CUSTOMER,
                first_name=first_name,
                last_name=last_name,
                phone_number=f'+91000{rng.randint(100000, 999999)}',
            )
            users.append(user)
        return users

    def _ensure_products(self, sellers, categories, locations, products_per_seller, prefix, rng):
        adjective_pool = ['Pure', 'Fresh', 'Natural', 'Green', 'Clean', 'Earth', 'Bio', 'Eco']
        noun_pool = ['Soap', 'Cleaner', 'Powder', 'Bottle', 'Bag', 'Oil', 'Snack', 'Kit', 'Spray', 'Serum']

        products_by_seller = defaultdict(list)
        for seller_index, seller in enumerate(sellers, start=1):
            for product_index in range(1, products_per_seller + 1):
                name = (
                    f'{prefix.title()} '
                    f'{adjective_pool[(seller_index + product_index) % len(adjective_pool)]} '
                    f'{noun_pool[(seller_index * product_index) % len(noun_pool)]} '
                    f'{product_index:02d}'
                )
                defaults = {
                    'category': categories[(seller_index + product_index) % len(categories)],
                    'location': locations[(seller_index * product_index) % len(locations)],
                    'description': f'Demo product {name} for live order simulation.',
                    'price': Decimal(str(round(rng.uniform(8.0, 145.0), 2))),
                    'stock_quantity': rng.randint(60, 260),
                    'weight': Decimal(str(round(rng.uniform(0.1, 3.5), 2))),
                    'size': f'{rng.randint(200, 950)} g',
                    'is_active': True,
                }
                product, created = Product.objects.get_or_create(
                    seller=seller,
                    name=name,
                    defaults=defaults,
                )
                if not created and not product.is_active:
                    product.is_active = True
                    product.save(update_fields=['is_active', 'updated_at'])

                if product.location_id is None:
                    product.location = defaults['location']
                    product.save(update_fields=['location', 'updated_at'])

                products_by_seller[seller.id].append(product)
        return products_by_seller

    def _draw_status(self, rng, cancel_rate):
        if rng.random() < cancel_rate:
            return Booking.BookingStatus.CANCELLED
        roll = rng.random()
        if roll < 0.37:
            return Booking.BookingStatus.DELIVERED
        if roll < 0.62:
            return Booking.BookingStatus.SHIPPED
        if roll < 0.77:
            return Booking.BookingStatus.OUT_FOR_DELIVERY
        if roll < 0.93:
            return Booking.BookingStatus.CONFIRMED
        return Booking.BookingStatus.PENDING

    def _generate_transaction_reference(self):
        for _ in range(6):
            reference = f'DEMO{uuid.uuid4().hex[:10].upper()}'
            if not PaymentTransaction.objects.filter(transaction_reference=reference).exists():
                return reference
        return f'DEMO{uuid.uuid4().hex[:10].upper()}'

    def _create_activity(self, *, sellers, customers, products_by_seller, locations, booking_target, cancel_rate, rng):
        now = timezone.now()
        summary = {
            'products': sum(len(values) for values in products_by_seller.values()),
            'bookings': 0,
            'transactions': 0,
            'cancellations': 0,
            'complaints': 0,
            'feedback': 0,
        }
        seller_lookup = {seller.id: seller for seller in sellers}

        for _ in range(booking_target):
            seller = sellers[rng.randrange(len(sellers))]
            seller_products = products_by_seller[seller.id]
            if not seller_products:
                continue
            product = seller_products[rng.randrange(len(seller_products))]
            customer = customers[rng.randrange(len(customers))]

            quantity = rng.randint(1, 4)
            unit_price = Decimal(product.price)
            total_amount = (unit_price * quantity).quantize(Decimal('0.01'))
            booked_at = now - timedelta(
                days=rng.randint(0, 16),
                hours=rng.randint(0, 23),
                minutes=rng.randint(0, 59),
            )
            status = self._draw_status(rng, cancel_rate)
            shipping_address = (
                f'{rng.randint(10, 999)} Demo Street, '
                f'{locations[rng.randrange(len(locations))].name}, '
                f'Landmark {rng.randint(1, 30)}'
            )
            delivery_location = product.location or locations[rng.randrange(len(locations))]

            booking = Booking.objects.create(
                customer=customer,
                seller=seller,
                delivery_location=delivery_location,
                shipping_address=shipping_address,
                total_amount=total_amount,
                status=status,
            )
            BookingItem.objects.create(
                booking=booking,
                product=product,
                quantity=quantity,
                unit_price=unit_price,
            )

            update_fields = []
            booking.booked_at = booked_at
            update_fields.append('booked_at')

            if status in {
                Booking.BookingStatus.CONFIRMED,
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.DELIVERED,
            }:
                booking.expected_delivery_date = (booked_at + timedelta(days=rng.randint(1, 5))).date()
                update_fields.append('expected_delivery_date')
                if status in {Booking.BookingStatus.SHIPPED, Booking.BookingStatus.OUT_FOR_DELIVERY, Booking.BookingStatus.DELIVERED}:
                    booking.tracking_number = f'TRK-{uuid.uuid4().hex[:10].upper()}'
                    update_fields.append('tracking_number')

                product.stock_quantity = max(0, product.stock_quantity - quantity)
                product.save(update_fields=['stock_quantity', 'updated_at'])

            if status == Booking.BookingStatus.CANCELLED:
                summary['cancellations'] += 1
                cancelled_by_seller = rng.random() < 0.22
                booking.cancelled_by_role = (
                    User.UserRole.SELLER if cancelled_by_seller else User.UserRole.CUSTOMER
                )
                booking.cancelled_at = booked_at + timedelta(hours=rng.randint(1, 48))
                booking.cancellation_reason = (
                    'Seller cancelled due to temporary stock mismatch.'
                    if cancelled_by_seller
                    else 'Customer requested cancellation after placing order.'
                )
                booking.cancellation_impact = (
                    Booking.CancellationImpact.NEGATIVE_IMPACT
                    if cancelled_by_seller
                    else Booking.CancellationImpact.NOT_REVIEWED
                )
                booking.cancellation_impact_note = (
                    'Auto-tagged negative impact for seller-side cancellation.'
                    if cancelled_by_seller
                    else ''
                )
                update_fields.extend(
                    [
                        'cancelled_by_role',
                        'cancelled_at',
                        'cancellation_reason',
                        'cancellation_impact',
                        'cancellation_impact_note',
                    ]
                )

            booking.save(update_fields=update_fields)
            summary['bookings'] += 1

            payment_method = rng.choice(
                [
                    PaymentTransaction.PaymentMethod.CARD,
                    PaymentTransaction.PaymentMethod.UPI,
                    PaymentTransaction.PaymentMethod.COD,
                ]
            )
            transaction_status = None
            if status in {
                Booking.BookingStatus.CONFIRMED,
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.DELIVERED,
            }:
                transaction_status = PaymentTransaction.TransactionStatus.SUCCESS
            elif status == Booking.BookingStatus.PENDING and rng.random() < 0.25:
                transaction_status = PaymentTransaction.TransactionStatus.INITIATED
            elif status == Booking.BookingStatus.CANCELLED:
                cancelled_roll = rng.random()
                if cancelled_roll < 0.55:
                    transaction_status = PaymentTransaction.TransactionStatus.REFUNDED
                elif cancelled_roll < 0.80:
                    transaction_status = PaymentTransaction.TransactionStatus.FAILED

            if transaction_status:
                paid_at = None
                if transaction_status in {
                    PaymentTransaction.TransactionStatus.SUCCESS,
                    PaymentTransaction.TransactionStatus.REFUNDED,
                } and payment_method != PaymentTransaction.PaymentMethod.COD:
                    paid_at = booked_at + timedelta(hours=rng.randint(1, 72))
                PaymentTransaction.objects.create(
                    booking=booking,
                    amount=total_amount,
                    payment_method=payment_method,
                    status=transaction_status,
                    transaction_reference=self._generate_transaction_reference(),
                    paid_at=paid_at,
                )
                summary['transactions'] += 1

            if status == Booking.BookingStatus.DELIVERED and rng.random() < 0.42:
                rating_roll = rng.random()
                if rating_roll < 0.62:
                    rating = rng.choice([4, 5])
                elif rating_roll < 0.88:
                    rating = 3
                else:
                    rating = rng.choice([1, 2])
                Feedback.objects.create(
                    customer=customer,
                    product=product,
                    booking=booking,
                    rating=rating,
                    comment=(
                        'Good delivery and packaging.'
                        if rating >= 4
                        else 'Delay/quality concerns, please improve.'
                    ),
                )
                summary['feedback'] += 1

            if status in {Booking.BookingStatus.CANCELLED, Booking.BookingStatus.DELIVERED} and rng.random() < 0.09:
                Complaint.objects.create(
                    customer=customer,
                    product=product,
                    booking=booking,
                    subject='Demo complaint about order experience',
                    message='Sample complaint generated for dashboard and support analytics demo.',
                    status=rng.choice(
                        [
                            Complaint.ComplaintStatus.OPEN,
                            Complaint.ComplaintStatus.IN_PROGRESS,
                            Complaint.ComplaintStatus.RESOLVED,
                        ]
                    ),
                )
                summary['complaints'] += 1

        # Keep seller profile risk score roughly synced for dashboards that read it directly.
        for seller in sellers:
            profile = SellerProfile.objects.filter(user_id=seller.id).first()
            if not profile:
                continue
            seller_booking_count = Booking.objects.filter(seller_id=seller.id).count()
            seller_cancelled = Booking.objects.filter(
                seller_id=seller.id,
                status=Booking.BookingStatus.CANCELLED,
                cancelled_by_role=User.UserRole.SELLER,
            ).count()
            risk_hint = round((seller_cancelled / max(1, seller_booking_count)) * 100.0, 2)
            profile.risk_score = risk_hint
            profile.save(update_fields=['risk_score', 'updated_at'])

        return summary
