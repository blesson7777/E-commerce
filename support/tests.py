from decimal import Decimal

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from analytics.models import SellerRiskSnapshot
from catalog.models import Category
from catalog.models import Product
from config.context_processors import ui_notifications
from locations.models import District
from locations.models import Location
from locations.models import State
from orders.models import Booking
from orders.models import BookingItem
from support.models import Complaint
from support.models import Feedback


class FeedbackFlowTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seller = User.objects.create_user(
            email='support-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='support-seller',
        )
        self.customer = User.objects.create_user(
            email='support-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='support-customer',
        )
        self.other_customer = User.objects.create_user(
            email='support-customer-2@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='support-customer-2',
        )
        state = State.objects.create(name='Kerala Support', code='KLS')
        district = District.objects.create(name='Kochi Support', state=state)
        self.location = Location.objects.create(
            district=district,
            name='Edappally',
            postal_code='682024',
        )
        category = Category.objects.create(name='Support Test Category', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Support Test Product',
            description='Support product',
            price=Decimal('8.00'),
            stock_quantity=30,
            is_active=True,
        )
        self.other_product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=self.location,
            name='Support Other Product',
            description='Other product',
            price=Decimal('12.00'),
            stock_quantity=15,
            is_active=True,
        )

    def _create_booking(self, *, status, product=None):
        product = product or self.product
        booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=self.location,
            shipping_address='Support address',
            status=status,
            total_amount=Decimal('16.00'),
        )
        BookingItem.objects.create(
            booking=booking,
            product=product,
            quantity=2,
            unit_price=product.price,
        )
        return booking

    def _request_with_session(self, user):
        request = self.factory.get('/')
        request.user = user
        middleware = SessionMiddleware(lambda r: None)
        middleware.process_request(request)
        request.session.save()
        return request

    def test_feedback_page_redirects_when_no_delivered_booking(self):
        self._create_booking(status=Booking.BookingStatus.CONFIRMED)
        self.client.force_login(self.customer)
        response = self.client.get(reverse('support:feedback_create'))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('orders:booking_list'))

    def test_feedback_submission_allowed_for_delivered_booking_item(self):
        booking = self._create_booking(status=Booking.BookingStatus.DELIVERED)
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('support:feedback_create'),
            data={
                'booking': booking.id,
                'product': self.product.id,
                'rating': 5,
                'comment': 'Delivered well and good quality.',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Feedback.objects.filter(
                customer=self.customer,
                booking=booking,
                product=self.product,
                rating=5,
            ).exists()
        )

    def test_feedback_submission_rejects_product_not_in_booking(self):
        booking = self._create_booking(status=Booking.BookingStatus.DELIVERED)
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('support:feedback_create'),
            data={
                'booking': booking.id,
                'product': self.other_product.id,
                'rating': 4,
                'comment': 'Attempting wrong product feedback.',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not part of the selected booking')
        self.assertFalse(Feedback.objects.filter(customer=self.customer, booking=booking).exists())

    def test_customer_notifications_include_review_prompt_after_delivery(self):
        booking = self._create_booking(status=Booking.BookingStatus.DELIVERED)
        request = self._request_with_session(self.customer)
        payload = ui_notifications(request)
        review_notifications = [
            item for item in payload['ui_notifications']
            if item.get('title') == 'Review delivered items'
        ]
        self.assertEqual(len(review_notifications), 1)
        self.assertIn('/support/feedback/new/?booking=', review_notifications[0]['url'])
        self.assertIn(str(booking.id), review_notifications[0]['url'])

    def test_feedback_form_locks_booking_and_product_from_booking_link(self):
        booking = self._create_booking(status=Booking.BookingStatus.DELIVERED, product=self.product)
        self.client.force_login(self.customer)
        response = self.client.get(
            f"{reverse('support:feedback_create')}?booking={booking.id}&product={self.product.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Review Target Locked')
        self.assertNotContains(response, 'name="booking" class="form-select')
        self.assertNotContains(response, 'name="product" class="form-select')

    def test_feedback_form_blocks_tampering_when_target_locked(self):
        booking = self._create_booking(status=Booking.BookingStatus.DELIVERED, product=self.product)
        other_booking = self._create_booking(
            status=Booking.BookingStatus.DELIVERED,
            product=self.other_product,
        )
        self.client.force_login(self.customer)
        response = self.client.post(
            f"{reverse('support:feedback_create')}?booking={booking.id}&product={self.product.id}",
            data={
                'booking': other_booking.id,
                'product': self.other_product.id,
                'rating': 4,
                'comment': 'Trying to change booking and product.',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select a valid choice')
        self.assertFalse(
            Feedback.objects.filter(
                customer=self.customer,
                booking=other_booking,
                product=self.other_product,
            ).exists()
        )

    def test_one_star_feedback_marks_anomaly_signal_in_risk_snapshot(self):
        booking = self._create_booking(status=Booking.BookingStatus.DELIVERED, product=self.product)
        self.client.force_login(self.customer)
        response = self.client.post(
            f"{reverse('support:feedback_create')}?booking={booking.id}&product={self.product.id}",
            data={
                'booking': booking.id,
                'product': self.product.id,
                'rating': 1,
                'comment': 'Very poor quality and bad experience.',
            },
        )
        self.assertEqual(response.status_code, 302)
        snapshot = SellerRiskSnapshot.objects.filter(seller=self.seller).first()
        self.assertIsNotNone(snapshot)
        self.assertTrue(
            any(
                '1-star feedback signal' in str(factor)
                for factor in (snapshot.risk_factors or [])
            )
        )

    def test_customer_can_read_product_reviews_with_product_filter(self):
        Feedback.objects.create(
            customer=self.customer,
            product=self.product,
            rating=5,
            comment='Loved this product overall.',
        )
        Feedback.objects.create(
            customer=self.other_customer,
            product=self.product,
            rating=2,
            comment='Packaging issue on delivery.',
        )
        Feedback.objects.create(
            customer=self.other_customer,
            product=self.other_product,
            rating=4,
            comment='This is for another product.',
        )

        self.client.force_login(self.customer)
        response = self.client.get(f"{reverse('support:feedback_list')}?product={self.product.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'Reviews: {self.product.name}')
        self.assertContains(response, 'Loved this product overall.')
        self.assertContains(response, 'Packaging issue on delivery.')
        self.assertNotContains(response, 'This is for another product.')


class ComplaintWorkflowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='support-admin@example.com',
            password='Pass@12345',
            role=User.UserRole.ADMIN,
            username='support-admin',
        )
        self.seller = User.objects.create_user(
            email='support-complaint-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='support-complaint-seller',
        )
        self.customer = User.objects.create_user(
            email='support-complaint-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='support-complaint-customer',
        )

        state = State.objects.create(name='Complaint State', code='CSP')
        district = District.objects.create(name='Complaint District', state=state)
        location = Location.objects.create(
            district=district,
            name='Complaint Town',
            postal_code='601234',
        )
        category = Category.objects.create(name='Complaint Category', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=location,
            name='Complaint Product',
            description='Complaint product',
            price=Decimal('19.00'),
            stock_quantity=20,
            is_active=True,
        )
        self.booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            delivery_location=location,
            shipping_address='Complaint Street',
            status=Booking.BookingStatus.CONFIRMED,
            total_amount=Decimal('19.00'),
        )
        BookingItem.objects.create(
            booking=self.booking,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )
        self.complaint = Complaint.objects.create(
            customer=self.customer,
            product=self.product,
            booking=self.booking,
            subject='Delivery concern',
            message='Order status is not moving and shipment is delayed for this booking.',
            status=Complaint.ComplaintStatus.OPEN,
        )

    def test_complaint_list_shows_product_seller_links_and_view_button(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('support:complaint_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('catalog:product_detail', args=[self.product.id]))
        self.assertContains(response, f"{reverse('catalog:product_list')}?seller={self.seller.id}")
        self.assertContains(response, reverse('support:complaint_detail', args=[self.complaint.id]))
        self.assertContains(response, 'View')

    def test_admin_can_mark_complaint_as_anomaly_and_run_ml(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('support:complaint_detail', args=[self.complaint.id]),
            data={
                'status': Complaint.ComplaintStatus.IN_PROGRESS,
                'mark_anomaly': 'on',
                'run_ml_check': 'on',
                'anomaly_note': 'Repeated suspicious complaints with delayed-shipping pattern.',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.complaint.refresh_from_db()
        self.assertEqual(self.complaint.status, Complaint.ComplaintStatus.IN_PROGRESS)
        self.assertTrue(self.complaint.is_anomaly)
        self.assertEqual(self.complaint.anomaly_marked_by_id, self.admin.id)
        self.assertIsNotNone(self.complaint.anomaly_marked_at)
        self.assertIsNotNone(self.complaint.ml_scored_at)
        self.assertIsNotNone(self.complaint.risk_snapshot)

    def test_customer_cannot_submit_complaint_action_form(self):
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('support:complaint_detail', args=[self.complaint.id]),
            data={
                'status': Complaint.ComplaintStatus.RESOLVED,
                'mark_anomaly': 'on',
                'run_ml_check': 'on',
                'anomaly_note': 'Not allowed for customer',
            },
        )
        self.assertEqual(response.status_code, 403)
        self.complaint.refresh_from_db()
        self.assertEqual(self.complaint.status, Complaint.ComplaintStatus.OPEN)
