from datetime import timedelta
from decimal import Decimal

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import SellerProfile
from accounts.models import User
from analytics.models import SellerRiskIncident
from catalog.cart import CART_SESSION_KEY
from catalog.models import Category
from catalog.models import Product
from config.context_processors import ui_notifications
from locations.models import District
from locations.models import Location
from locations.models import State
from orders.models import Booking
from orders.models import BookingItem
from orders.models import Transaction
from orders.views import CART_CHECKOUT_PENDING_BOOKINGS_SESSION_KEY


class BookingStatusFlowTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.admin = User.objects.create_user(
            email='admin@example.com',
            password='Pass@12345',
            role=User.UserRole.ADMIN,
            username='admin-user',
        )
        self.seller = User.objects.create_user(
            email='seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='seller-user',
        )
        self.customer = User.objects.create_user(
            email='customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='customer-user',
        )
        state = State.objects.create(name='Kerala', code='KL')
        district = District.objects.create(name='Ernakulam', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Kakkanad',
            postal_code='682030',
        )
        category = Category.objects.create(name='Eco Kits', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Reusable Bottle',
            description='Steel bottle',
            price=Decimal('12.00'),
            stock_quantity=10,
            is_active=True,
        )

    def _create_booking(self, *, status, quantity, stock_quantity):
        self.product.stock_quantity = stock_quantity
        self.product.save(update_fields=['stock_quantity'])
        booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='Sample address',
            status=status,
            total_amount=Decimal('24.00'),
        )
        BookingItem.objects.create(
            booking=booking,
            product=self.product,
            quantity=quantity,
            unit_price=self.product.price,
        )
        return booking

    def _request_with_session(self, user):
        request = self.factory.get('/')
        request.user = user
        middleware = SessionMiddleware(lambda r: None)
        middleware.process_request(request)
        request.session.save()
        return request

    def _seller_cancel_payload(self):
        return {
            'status': Booking.BookingStatus.CANCELLED,
            'tracking_number': '',
            'expected_delivery_date': '',
            'seller_cancellation_reason_code': 'out_of_stock',
            'seller_cancellation_other_reason': '',
            'seller_cancellation_ack_note': 'Seller confirmed cancellation impact awareness.',
            'seller_cancellation_acknowledged': 'on',
        }

    def test_seller_can_confirm_shipped_booking_as_delivered(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.SHIPPED,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        response = self.client.post(reverse('orders:confirm_booking_delivered', args=[booking.id]))
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.DELIVERED)

    def test_seller_cannot_move_pending_booking_to_confirmed_without_payment(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.PENDING,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data={
                'status': Booking.BookingStatus.CONFIRMED,
                'tracking_number': '',
                'expected_delivery_date': '',
            },
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.PENDING)

    def test_seller_cannot_confirm_pending_booking_as_delivered(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.PENDING,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        response = self.client.post(reverse('orders:confirm_booking_delivered', args=[booking.id]))
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.PENDING)

    def test_cancelling_booking_restores_stock(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=5,
        )
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data=self._seller_cancel_payload(),
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CANCELLED)
        self.assertEqual(self.product.stock_quantity, 7)
        self.assertEqual(booking.cancellation_impact, Booking.CancellationImpact.NEGATIVE_IMPACT)
        self.assertIsNotNone(booking.anomaly_incident_id)
        seller_profile = SellerProfile.objects.get(user=self.seller)
        self.assertTrue(seller_profile.is_suspended)

    def test_seller_cancelling_paid_booking_marks_transaction_refunded(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=5,
        )
        transaction_obj = Transaction.objects.create(
            booking=booking,
            amount=booking.total_amount,
            payment_method=Transaction.PaymentMethod.CARD,
            status=Transaction.TransactionStatus.SUCCESS,
            transaction_reference='SELLREFUND01',
            paid_at=timezone.now(),
        )
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data=self._seller_cancel_payload(),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        transaction_obj.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CANCELLED)
        self.assertEqual(transaction_obj.status, Transaction.TransactionStatus.REFUNDED)
        self.assertContains(response, 'refunded')
        self.assertContains(response, 'Risk Score:')

    def test_seller_cancellation_requires_reason_and_acknowledgement(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=5,
        )
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data={
                'status': Booking.BookingStatus.CANCELLED,
                'tracking_number': '',
                'expected_delivery_date': '',
                'seller_cancellation_reason_code': '',
                'seller_cancellation_other_reason': '',
                'seller_cancellation_ack_note': '',
            },
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CONFIRMED)
        self.assertContains(response, 'Select a valid seller cancellation reason.')
        self.assertContains(response, 'Acknowledge the seller cancellation warning before continuing.')

    def test_customer_sees_seller_cancellation_reason_without_internal_acknowledgement_note(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=5,
        )
        self.client.force_login(self.seller)
        self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data={
                **self._seller_cancel_payload(),
                'seller_cancellation_ack_note': 'Internal audit acknowledgement from seller.',
            },
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CANCELLED)

        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:booking_detail', args=[booking.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Item became out of stock')
        self.assertNotContains(response, 'Internal audit acknowledgement from seller.')

    def test_admin_review_is_not_available_for_seller_cancelled_booking(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=5,
        )
        self.client.force_login(self.seller)
        self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data=self._seller_cancel_payload(),
        )
        booking.refresh_from_db()
        self.assertEqual(booking.cancelled_by_role, User.UserRole.SELLER)
        self.assertIsNotNone(booking.anomaly_incident_id)

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('orders:review_booking_cancellation', args=[booking.id]),
            data={
                'cancellation_impact': Booking.CancellationImpact.NO_IMPACT,
                'cancellation_impact_note': 'Try override.',
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.cancellation_impact, Booking.CancellationImpact.NEGATIVE_IMPACT)
        self.assertContains(response, 'Seller cancellations are auto-marked high risk')

    def test_admin_reopen_cancelled_booking_deducts_stock(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CANCELLED,
            quantity=3,
            stock_quantity=10,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data={
                'status': Booking.BookingStatus.CONFIRMED,
                'tracking_number': '',
                'expected_delivery_date': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CONFIRMED)
        self.assertEqual(self.product.stock_quantity, 7)

    def test_admin_reopen_cancelled_booking_fails_when_stock_low(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CANCELLED,
            quantity=3,
            stock_quantity=2,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data={
                'status': Booking.BookingStatus.CONFIRMED,
                'tracking_number': '',
                'expected_delivery_date': '',
            },
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CANCELLED)
        self.assertEqual(self.product.stock_quantity, 2)

    def test_confirmed_booking_status_page_shows_mark_shipped_and_cancel_actions(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        response = self.client.get(reverse('orders:update_booking_status', args=[booking.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Mark Shipped')
        self.assertContains(response, 'Cancel Booking')
        self.assertContains(response, 'type="date"', html=False)

    def test_seller_can_mark_confirmed_booking_as_shipped_with_tracking_and_expected_date(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        expected_date = timezone.localdate() + timedelta(days=3)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data={
                'action': 'mark_shipped',
                'tracking_number': 'TRACK-12345',
                'expected_delivery_date': expected_date.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.SHIPPED)
        self.assertEqual(booking.tracking_number, 'TRACK-12345')
        self.assertEqual(booking.expected_delivery_date, expected_date)

    def test_seller_cannot_mark_confirmed_booking_as_shipped_without_expected_date(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data={
                'action': 'mark_shipped',
                'tracking_number': 'TRACK-12345',
                'expected_delivery_date': '',
            },
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CONFIRMED)
        self.assertContains(response, 'Expected delivery date is required before marking shipped.')

    def test_booking_list_shows_anomaly_risk_for_seller_cancelled_bookings(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data=self._seller_cancel_payload(),
        )
        response = self.client.get(reverse('orders:booking_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Anomaly marked')

    def test_booking_list_highlights_delayed_confirmed_bookings_for_seller(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=8,
        )
        booking.booked_at = timezone.now() - timedelta(days=3)
        booking.save(update_fields=['booked_at'])

        self.client.force_login(self.seller)
        response = self.client.get(reverse('orders:booking_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Shipping delay >2 days')
        self.assertContains(response, 'delayed more than 2 days without shipment')

    def test_customer_booking_list_shows_seller_cancellation_reason(self):
        booking = self._create_booking(
            status=Booking.BookingStatus.CONFIRMED,
            quantity=2,
            stock_quantity=8,
        )
        self.client.force_login(self.seller)
        self.client.post(
            reverse('orders:update_booking_status', args=[booking.id]),
            data=self._seller_cancel_payload(),
        )

        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:booking_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reason: Item became out of stock')


class CategoryAvailabilityBookingTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='category-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='category-seller',
        )
        self.customer = User.objects.create_user(
            email='category-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='category-customer',
        )
        state = State.objects.create(name='Andhra Pradesh', code='AP')
        district = District.objects.create(name='Visakhapatnam', state=state)
        self.location = Location.objects.create(
            district=district,
            name='MVP Colony',
            postal_code='530017',
        )
        self.category = Category.objects.create(name='Booking Category', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            location=self.location,
            name='Booking Product',
            description='Booking category availability test',
            price=Decimal('11.00'),
            stock_quantity=9,
            is_active=True,
        )

    def test_create_booking_blocks_products_in_off_categories(self):
        self.client.force_login(self.customer)
        self.category.is_active = False
        self.category.save(update_fields=['is_active'])

        response = self.client.get(
            reverse('orders:create_booking', args=[self.product.id]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This product category is non-listed now. Booking is unavailable.')
        self.assertEqual(Booking.objects.count(), 0)


class CartCheckoutAvailabilityBlockTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='checkout-block-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='checkout-block-seller',
        )
        self.customer = User.objects.create_user(
            email='checkout-block-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='checkout-block-customer',
        )
        self.category = Category.objects.create(name='Checkout Block Category', is_active=True)
        self.state = State.objects.create(name='Odisha', code='OD')
        self.district = District.objects.create(name='Khordha', state=self.state)
        self.location = Location.objects.create(
            district=self.district,
            name='Bhubaneswar',
            postal_code='751001',
        )
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            location=self.location,
            name='Checkout Block Product',
            description='Checkout block test product',
            price=Decimal('13.00'),
            stock_quantity=7,
            is_active=True,
        )
        self.client.force_login(self.customer)

    def _set_cart_item(self, quantity=1):
        session = self.client.session
        session[CART_SESSION_KEY] = {str(self.product.id): quantity}
        session.save()

    def test_checkout_page_shows_red_warning_and_disables_submit_for_unavailable_category_item(self):
        self._set_cart_item(quantity=2)
        self.category.is_active = False
        self.category.save(update_fields=['is_active'])

        response = self.client.get(reverse('orders:cart_checkout'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Remove unavailable items to continue checkout.')
        self.assertContains(response, 'Checkout is locked until unavailable cart items are removed.')
        self.assertContains(response, 'Remove From Cart')
        self.assertContains(response, '<button type="submit" disabled>')

    def test_checkout_post_is_blocked_until_unavailable_items_are_removed(self):
        self._set_cart_item(quantity=1)
        self.category.is_active = False
        self.category.save(update_fields=['is_active'])

        response = self.client.post(
            reverse('orders:cart_checkout'),
            data={
                'address_mode': 'new',
                'delivery_pincode': self.location.postal_code,
                'shipping_address': '221 Lake View Road',
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Remove unavailable products from your cart before checkout.')
        self.assertEqual(Booking.objects.count(), 0)


class BookingListPaymentResumeTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='resume-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='resume-seller',
        )
        self.customer = User.objects.create_user(
            email='resume-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='resume-customer',
        )
        state = State.objects.create(name='Karnataka', code='KA')
        district = District.objects.create(name='Bengaluru', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Indiranagar',
            postal_code='560038',
        )
        category = Category.objects.create(name='Resume Payments', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Resume Product',
            description='Resume payment scenario',
            price=Decimal('18.00'),
            stock_quantity=9,
            is_active=True,
        )
        self.booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='18 Lake Side',
            status=Booking.BookingStatus.PENDING,
            total_amount=Decimal('18.00'),
        )
        BookingItem.objects.create(
            booking=self.booking,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )
        self.client.force_login(self.customer)

    def test_booking_list_shows_grouped_payment_resume_action(self):
        session = self.client.session
        session[CART_CHECKOUT_PENDING_BOOKINGS_SESSION_KEY] = [self.booking.id]
        session.save()

        response = self.client.get(reverse('orders:booking_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Resume Group Payment')
        self.assertContains(response, reverse('orders:cart_checkout_payment'))

    def test_booking_list_still_shows_per_booking_pay_now_link(self):
        response = self.client.get(reverse('orders:booking_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('orders:create_transaction', args=[self.booking.id]))
        self.assertContains(response, 'Pay Now')


class BookingReceiptTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='receipt-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='receipt-seller',
        )
        self.customer = User.objects.create_user(
            email='receipt-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='receipt-customer',
        )
        self.other_customer = User.objects.create_user(
            email='receipt-customer2@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='receipt-customer-2',
        )
        state = State.objects.create(name='Tamil Nadu', code='TN')
        district = District.objects.create(name='Chennai', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Anna Nagar',
            postal_code='600040',
        )
        category = Category.objects.create(name='Household', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Natural Cleaner',
            description='Plant based cleaner',
            price=Decimal('9.00'),
            stock_quantity=20,
            is_active=True,
        )

    def _create_booking(self, *, status, customer=None):
        booking = Booking.objects.create(
            customer=customer or self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='42 Green Avenue',
            status=status,
            total_amount=Decimal('18.00'),
        )
        BookingItem.objects.create(
            booking=booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )
        return booking

    def test_customer_can_view_receipt_after_shipping(self):
        booking = self._create_booking(status=Booking.BookingStatus.SHIPPED)
        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:booking_receipt', args=[booking.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nature Nest')
        self.assertContains(response, 'Invoice:')

    def test_customer_can_view_receipt_after_out_for_delivery(self):
        booking = self._create_booking(status=Booking.BookingStatus.OUT_FOR_DELIVERY)
        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:booking_receipt', args=[booking.id]))
        self.assertEqual(response.status_code, 200)

    def test_customer_is_redirected_before_shipping(self):
        booking = self._create_booking(status=Booking.BookingStatus.CONFIRMED)
        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:booking_receipt', args=[booking.id]))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('orders:booking_detail', args=[booking.id]))

    def test_other_customer_cannot_view_receipt(self):
        booking = self._create_booking(status=Booking.BookingStatus.SHIPPED)
        self.client.force_login(self.other_customer)
        response = self.client.get(reverse('orders:booking_receipt', args=[booking.id]))
        self.assertEqual(response.status_code, 404)


class ExpectedDeliveryDateVisibilityTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='expected-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='expected-seller',
        )
        self.customer = User.objects.create_user(
            email='expected-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='expected-customer',
        )
        state = State.objects.create(name='Rajasthan', code='RJ')
        district = District.objects.create(name='Jaipur', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Malviya Nagar',
            postal_code='302017',
        )
        category = Category.objects.create(name='Expected Date', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Expected Date Product',
            description='Expected date visibility product',
            price=Decimal('14.00'),
            stock_quantity=12,
            is_active=True,
        )
        self.expected_date = timezone.localdate() + timedelta(days=4)
        self.booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='88 Park Lane',
            status=Booking.BookingStatus.SHIPPED,
            tracking_number='VISIBILITY-123',
            expected_delivery_date=self.expected_date,
            total_amount=Decimal('28.00'),
        )
        BookingItem.objects.create(
            booking=self.booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )

    def test_customer_can_see_expected_delivery_date_in_booking_detail(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:booking_detail', args=[self.booking.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.expected_date.strftime('%b %d, %Y'))

    def test_customer_can_see_expected_delivery_date_in_booking_list(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:booking_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.expected_date.strftime('%b %d, %Y'))


class PublicDeliveryStatusUpdateTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='public-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='public-seller',
        )
        self.customer = User.objects.create_user(
            email='public-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='public-customer',
        )
        state = State.objects.create(name='Karnataka', code='KA')
        district = District.objects.create(name='Bangalore', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Indiranagar',
            postal_code='560038',
        )
        category = Category.objects.create(name='Organic Care', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Herbal Soap',
            description='Natural soap',
            price=Decimal('5.00'),
            stock_quantity=25,
            is_active=True,
        )

    def _create_booking(self, *, status):
        booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='57 Green Road',
            status=status,
            total_amount=Decimal('10.00'),
        )
        BookingItem.objects.create(
            booking=booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )
        return booking

    def test_public_page_is_accessible_without_login(self):
        response = self.client.get(reverse('orders:public_delivery_status_update'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Delivery Status Update Desk')

    def test_public_page_can_mark_out_for_delivery(self):
        booking = self._create_booking(status=Booking.BookingStatus.SHIPPED)
        response = self.client.post(
            reverse('orders:public_delivery_status_update'),
            data={
                'booking_id': booking.id,
                'target_status': Booking.BookingStatus.OUT_FOR_DELIVERY,
            },
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.OUT_FOR_DELIVERY)

    def test_public_page_can_mark_delivered(self):
        booking = self._create_booking(status=Booking.BookingStatus.OUT_FOR_DELIVERY)
        response = self.client.post(
            reverse('orders:public_delivery_status_update'),
            data={
                'booking_id': booking.id,
                'target_status': Booking.BookingStatus.DELIVERED,
            },
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.DELIVERED)

    def test_public_page_blocks_invalid_transition(self):
        booking = self._create_booking(status=Booking.BookingStatus.PENDING)
        response = self.client.post(
            reverse('orders:public_delivery_status_update'),
            data={
                'booking_id': booking.id,
                'target_status': Booking.BookingStatus.DELIVERED,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.PENDING)
        self.assertContains(response, 'Cannot update booking')

    def test_public_page_requires_out_for_delivery_before_delivered(self):
        booking = self._create_booking(status=Booking.BookingStatus.SHIPPED)
        response = self.client.post(
            reverse('orders:public_delivery_status_update'),
            data={
                'booking_id': booking.id,
                'target_status': Booking.BookingStatus.DELIVERED,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.SHIPPED)
        self.assertContains(response, 'Cannot update booking')

    def test_public_page_blocks_out_for_delivery_before_shipped(self):
        booking = self._create_booking(status=Booking.BookingStatus.CONFIRMED)
        response = self.client.post(
            reverse('orders:public_delivery_status_update'),
            data={
                'booking_id': booking.id,
                'target_status': Booking.BookingStatus.OUT_FOR_DELIVERY,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CONFIRMED)
        self.assertContains(response, 'Cannot update booking')

    def test_public_page_lists_orders_with_delivery_actions(self):
        shipped_booking = self._create_booking(status=Booking.BookingStatus.SHIPPED)
        out_booking = self._create_booking(status=Booking.BookingStatus.OUT_FOR_DELIVERY)
        response = self.client.get(reverse('orders:public_delivery_status_update'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'#{shipped_booking.id}')
        self.assertContains(response, f'#{out_booking.id}')
        self.assertContains(response, 'Mark Out for Delivery')
        self.assertContains(response, 'Mark Delivered')


class PaymentFlowTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seller = User.objects.create_user(
            email='payment-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='payment-seller',
        )
        self.customer = User.objects.create_user(
            email='payment-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='payment-customer',
        )
        state = State.objects.create(name='Goa', code='GA')
        district = District.objects.create(name='North Goa', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Panaji',
            postal_code='403001',
        )
        category = Category.objects.create(name='Payment Flow', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Payment Product',
            description='Payment product',
            price=Decimal('15.00'),
            stock_quantity=30,
            is_active=True,
        )
        self.booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='Payment address',
            status=Booking.BookingStatus.PENDING,
            total_amount=Decimal('30.00'),
        )
        BookingItem.objects.create(
            booking=self.booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )

    def _request_with_session(self, user):
        request = self.factory.get('/')
        request.user = user
        middleware = SessionMiddleware(lambda r: None)
        middleware.process_request(request)
        request.session.save()
        return request

    def test_customer_gets_payment_pending_notification(self):
        request = self._request_with_session(self.customer)
        payload = ui_notifications(request)
        payment_notifications = [
            item for item in payload['ui_notifications']
            if item.get('title') == 'Payment pending'
        ]
        self.assertEqual(len(payment_notifications), 1)
        self.assertIn(f'/orders/{self.booking.id}/pay/', payment_notifications[0]['url'])

    def test_successful_payment_redirects_to_success_screen_and_confirms_booking(self):
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('orders:create_transaction', args=[self.booking.id]),
            data={
                'payment_method': Transaction.PaymentMethod.CARD,
                'card_holder_name': 'Demo User',
                'card_number': '4111111111111111',
                'card_expiry': '12/30',
                'card_cvv': '123',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.BookingStatus.CONFIRMED)
        transaction_obj = self.booking.transactions.filter(status=Transaction.TransactionStatus.SUCCESS).first()
        self.assertIsNotNone(transaction_obj)
        self.assertIn(
            reverse('orders:transaction_success', args=[self.booking.id, transaction_obj.id]),
            response.url,
        )

    def test_paid_order_shows_order_and_payment_success_messages(self):
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('orders:create_transaction', args=[self.booking.id]),
            data={
                'payment_method': Transaction.PaymentMethod.UPI,
                'upi_id': 'demo@bank',
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Order successful. Booking has been confirmed.')
        self.assertContains(response, 'Payment successful.')
        self.assertContains(response, 'Order Successful')
        self.assertContains(response, 'Payment Successful')

    def test_cod_order_shows_only_order_success_message(self):
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('orders:create_transaction', args=[self.booking.id]),
            data={
                'payment_method': Transaction.PaymentMethod.COD,
                'cod_consent': 'on',
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Order successful. Booking has been confirmed.')
        self.assertNotContains(response, 'Payment successful.')
        self.assertContains(response, 'Order Successful')
        transaction_obj = self.booking.transactions.order_by('-id').first()
        self.assertIsNotNone(transaction_obj)
        self.assertEqual(transaction_obj.payment_method, Transaction.PaymentMethod.COD)
        self.assertIsNone(transaction_obj.paid_at)

    def test_customer_receives_refund_notification_after_paid_order_cancel(self):
        self.client.force_login(self.customer)
        self.client.post(
            reverse('orders:create_transaction', args=[self.booking.id]),
            data={
                'payment_method': Transaction.PaymentMethod.CARD,
                'card_holder_name': 'Demo User',
                'card_number': '4111111111111111',
                'card_expiry': '12/30',
                'card_cvv': '123',
            },
        )
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.BookingStatus.CONFIRMED)
        cancel_response = self.client.post(
            reverse('orders:cancel_booking', args=[self.booking.id]),
            data={'cancellation_reason': 'Need to change order items after payment.'},
            follow=True,
        )
        self.assertEqual(cancel_response.status_code, 200)
        transaction_obj = self.booking.transactions.order_by('-id').first()
        self.assertIsNotNone(transaction_obj)
        self.assertEqual(transaction_obj.status, Transaction.TransactionStatus.REFUNDED)

        request = self._request_with_session(self.customer)
        payload = ui_notifications(request)
        refund_notifications = [
            item for item in payload['ui_notifications']
            if item.get('title') == 'Refund processed'
        ]
        self.assertEqual(len(refund_notifications), 1)
        self.assertIn(f'/orders/transactions/{transaction_obj.id}/', refund_notifications[0]['url'])


class CustomerCancellationFlowTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            email='cancel-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='cancel-customer',
        )
        self.seller = User.objects.create_user(
            email='cancel-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='cancel-seller',
        )
        state = State.objects.create(name='Maharashtra', code='MH')
        district = District.objects.create(name='Pune', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Kothrud',
            postal_code='411038',
        )
        category = Category.objects.create(name='Cancellation', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Cancellation Product',
            description='Cancellation test product',
            price=Decimal('10.00'),
            stock_quantity=8,
            is_active=True,
        )

    def _create_booking(self, status=Booking.BookingStatus.PENDING):
        booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='27 Green Road',
            status=status,
            total_amount=Decimal('20.00'),
        )
        BookingItem.objects.create(
            booking=booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )
        return booking

    def test_customer_cancelling_paid_booking_marks_transaction_refunded(self):
        booking = self._create_booking(status=Booking.BookingStatus.CONFIRMED)
        transaction_obj = Transaction.objects.create(
            booking=booking,
            amount=booking.total_amount,
            payment_method=Transaction.PaymentMethod.UPI,
            status=Transaction.TransactionStatus.SUCCESS,
            transaction_reference='CUSREFUND01',
            paid_at=timezone.now(),
        )
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('orders:cancel_booking', args=[booking.id]),
            data={'cancellation_reason': 'I need to reorder with another address.'},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        transaction_obj.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CANCELLED)
        self.assertEqual(transaction_obj.status, Transaction.TransactionStatus.REFUNDED)
        self.assertContains(response, 'refunded to the customer')

    def test_customer_can_cancel_before_shipped_with_reason(self):
        booking = self._create_booking(status=Booking.BookingStatus.CONFIRMED)
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('orders:cancel_booking', args=[booking.id]),
            data={'cancellation_reason': 'Changed my plan and selected another variant.'},
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.CANCELLED)
        self.assertIn('Changed my plan', booking.cancellation_reason)
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(booking.cancellation_impact, Booking.CancellationImpact.NOT_REVIEWED)
        self.assertIsNone(booking.anomaly_incident_id)

    def test_customer_cannot_cancel_after_shipped(self):
        booking = self._create_booking(status=Booking.BookingStatus.SHIPPED)
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('orders:cancel_booking', args=[booking.id]),
            data={'cancellation_reason': 'Late cancellation request.'},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.BookingStatus.SHIPPED)
        self.assertContains(response, 'only before it reaches the shipped status')


class CancellationImpactAnomalyTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='cancel-admin@example.com',
            password='Pass@12345',
            role=User.UserRole.ADMIN,
            username='cancel-admin',
        )
        self.customer = User.objects.create_user(
            email='cancel-admin-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='cancel-admin-customer',
        )
        self.seller = User.objects.create_user(
            email='cancel-admin-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='cancel-admin-seller',
        )
        state = State.objects.create(name='Delhi', code='DL')
        district = District.objects.create(name='New Delhi', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Dwarka',
            postal_code='110075',
        )
        category = Category.objects.create(name='Impact Tests', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Impact Product',
            description='Impact product',
            price=Decimal('25.00'),
            stock_quantity=5,
            is_active=True,
        )
        self.cancelled_booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='Flat 12, Oak Residency',
            status=Booking.BookingStatus.CANCELLED,
            total_amount=Decimal('50.00'),
            cancellation_reason='Customer entered wrong pincode and cancelled.',
            cancelled_by_role=User.UserRole.CUSTOMER,
        )
        BookingItem.objects.create(
            booking=self.cancelled_booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )
        self.open_booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='Flat 18, Maple Residency',
            status=Booking.BookingStatus.CONFIRMED,
            total_amount=Decimal('25.00'),
        )
        BookingItem.objects.create(
            booking=self.open_booking,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )

    def test_admin_cancellation_monitor_page_loads(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('orders:cancellation_monitor'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cancellation Monitoring')
        self.assertContains(response, f'#{self.cancelled_booking.id}')

    def test_seller_can_view_customer_cancellation_reason(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('orders:booking_detail', args=[self.cancelled_booking.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Customer entered wrong pincode and cancelled')

    def test_admin_negative_impact_marks_anomaly_and_freezes_seller(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('orders:review_booking_cancellation', args=[self.cancelled_booking.id]),
            data={
                'cancellation_impact': Booking.CancellationImpact.NEGATIVE_IMPACT,
                'cancellation_impact_note': 'Pattern indicates repeated cancellation damage.',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.cancelled_booking.refresh_from_db()
        self.open_booking.refresh_from_db()
        self.product.refresh_from_db()

        self.assertEqual(
            self.cancelled_booking.cancellation_impact,
            Booking.CancellationImpact.NEGATIVE_IMPACT,
        )
        self.assertIsNotNone(self.cancelled_booking.anomaly_incident_id)
        incident = SellerRiskIncident.objects.get(id=self.cancelled_booking.anomaly_incident_id)
        self.assertTrue(incident.is_active)
        self.assertEqual(self.open_booking.status, Booking.BookingStatus.CANCELLED)
        self.assertEqual(self.product.stock_quantity, 6)

        profile = SellerProfile.objects.get(user=self.seller)
        self.assertTrue(profile.is_suspended)
