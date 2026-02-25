from datetime import timedelta
from decimal import Decimal

from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.forms import ProfileUpdateForm
from accounts.models import SellerProfile
from accounts.models import User
from analytics.models import SellerRiskIncident
from analytics.models import SellerRiskSnapshot
from catalog.models import Category
from catalog.models import Product
from config.context_processors import ui_notifications
from locations.models import District
from locations.models import Location
from locations.models import State
from orders.models import Booking
from orders.models import BookingItem
from orders.models import Transaction
from support.models import Feedback


class ProfileUpdateFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='owner@example.com',
            password='Pass@12345',
            first_name='Owner',
            last_name='User',
            role=User.UserRole.CUSTOMER,
            username='owner-user',
        )
        self.other_user = User.objects.create_user(
            email='other@example.com',
            password='Pass@12345',
            first_name='Other',
            last_name='User',
            role=User.UserRole.CUSTOMER,
            username='existing-name',
        )

    def test_profile_update_allows_username_change(self):
        form = ProfileUpdateForm(
            data={
                'first_name': 'Updated',
                'last_name': 'Owner',
                'username': 'fresh-username',
                'email': 'owner@example.com',
                'phone_number': '+15551234567',
            },
            instance=self.user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        updated_user = form.save()
        self.assertEqual(updated_user.username, 'fresh-username')

    def test_profile_update_rejects_duplicate_username(self):
        form = ProfileUpdateForm(
            data={
                'first_name': 'Owner',
                'last_name': 'User',
                'username': self.other_user.username,
                'email': 'owner@example.com',
                'phone_number': '',
            },
            instance=self.user,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)

    def test_profile_update_accepts_uploaded_photo_file(self):
        upload = SimpleUploadedFile(
            'avatar.png',
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01',
            content_type='image/png',
        )
        form = ProfileUpdateForm(
            data={
                'first_name': 'Owner',
                'last_name': 'User',
                'username': 'owner-user',
                'email': 'owner@example.com',
                'phone_number': '',
            },
            files={'profile_photo': upload},
            instance=self.user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        updated_user = form.save()
        self.assertIn('profile_photos/', updated_user.profile_photo_url)


class AdminSellerCreationViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='admin@example.com',
            password='Pass@12345',
            role=User.UserRole.ADMIN,
            username='admin-user',
        )
        self.client.force_login(self.admin)

    def test_admin_add_seller_page_is_available(self):
        response = self.client.get(reverse('accounts:admin_add_seller'))
        self.assertEqual(response.status_code, 200)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PasswordResetOTPFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='otp-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='otp-customer',
        )

    def test_unregistered_email_shows_form_error(self):
        response = self.client.post(
            reverse('accounts:password_reset'),
            data={'email': 'missing@example.com'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No active account found with this email address.')
        self.assertNotIn('password_reset_otp_state', self.client.session)

    def test_registered_email_sends_otp_and_updates_password(self):
        request_response = self.client.post(
            reverse('accounts:password_reset'),
            data={'email': self.user.email},
        )
        self.assertRedirects(request_response, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Nature Nest password reset OTP', mail.outbox[0].subject)

        session = self.client.session
        reset_state = session.get('password_reset_otp_state')
        self.assertIsNotNone(reset_state)

        verify_response = self.client.post(
            reverse('accounts:password_reset_confirm'),
            data={
                'otp': reset_state['otp_code'],
                'new_password1': 'NewSecurePass@987',
                'new_password2': 'NewSecurePass@987',
            },
        )
        self.assertRedirects(verify_response, reverse('accounts:password_reset_complete'))
        self.assertTrue(
            self.client.login(
                email=self.user.email,
                password='NewSecurePass@987',
            )
        )


class NotificationMarkAllTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seller = User.objects.create_user(
            email='notify-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='notify-seller',
        )
        self.customer = User.objects.create_user(
            email='notify-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='notify-customer',
        )

    def _request_for_user_with_session(self, user, session):
        request = self.factory.get('/')
        request.user = user
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session = session
        return request

    def test_mark_all_notifications_hides_current_seller_notifications(self):
        Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            shipping_address='Notify lane',
            total_amount=10,
            status=Booking.BookingStatus.PENDING,
        )
        self.client.force_login(self.seller)

        request_before = self._request_for_user_with_session(self.seller, self.client.session)
        payload_before = ui_notifications(request_before)
        self.assertGreater(payload_before['ui_notification_count'], 0)

        response = self.client.post(
            reverse('accounts:mark_all_notifications_read'),
            data={'next': reverse('accounts:dashboard')},
        )
        self.assertEqual(response.status_code, 302)

        request_after = self._request_for_user_with_session(self.seller, self.client.session)
        payload_after = ui_notifications(request_after)
        self.assertEqual(payload_after['ui_notification_count'], 0)

        Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            shipping_address='Notify lane 2',
            total_amount=12,
            status=Booking.BookingStatus.PENDING,
        )
        request_new = self._request_for_user_with_session(self.seller, self.client.session)
        payload_new = ui_notifications(request_new)
        self.assertGreater(payload_new['ui_notification_count'], 0)


class SellerCategoryNonListedDashboardTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seller = User.objects.create_user(
            email='seller-category-warning@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='seller-category-warning',
        )
        self.category = Category.objects.create(
            name='Warning Category',
            is_active=False,
        )
        Product.objects.create(
            seller=self.seller,
            category=self.category,
            name='Warning Product',
            description='Product in disabled category',
            price=Decimal('9.50'),
            stock_quantity=5,
            is_active=True,
        )

    def test_seller_dashboard_shows_category_non_listed_warning(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Category non-listed now for customer booking and dashboard listing.',
        )

    def test_seller_notifications_include_category_non_listed_alert(self):
        request = self.factory.get('/')
        request.user = self.seller
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()
        payload = ui_notifications(request)
        titles = [item.get('title') for item in payload['ui_notifications']]
        self.assertIn('Category non-listed now', titles)


class DashboardEnhancementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='dash-admin@example.com',
            password='Pass@12345',
            role=User.UserRole.ADMIN,
            username='dash-admin',
        )
        self.seller = User.objects.create_user(
            email='dash-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='dash-seller',
        )
        self.customer = User.objects.create_user(
            email='dash-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='dash-customer',
        )
        category = Category.objects.create(name='Dashboard Metrics', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            name='Dashboard Product',
            description='Dashboard product',
            price=Decimal('22.00'),
            stock_quantity=3,
            is_active=True,
        )
        self.booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            shipping_address='48 Metrics Street',
            total_amount=Decimal('44.00'),
            status=Booking.BookingStatus.PENDING,
        )
        BookingItem.objects.create(
            booking=self.booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )
        Transaction.objects.create(
            booking=self.booking,
            amount=Decimal('44.00'),
            payment_method=Transaction.PaymentMethod.UPI,
            status=Transaction.TransactionStatus.SUCCESS,
            transaction_reference='DASHPAY001',
        )

    def test_admin_dashboard_shows_new_feature_sections(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Booking Status Snapshot')
        self.assertContains(response, 'Top Sellers by Revenue')
        self.assertContains(response, 'Recent Booking Activity')
        self.assertContains(response, 'Recent Payment Activity')

    def test_seller_dashboard_shows_new_feature_sections(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Booking Status Snapshot')
        self.assertContains(response, 'Top Products by Demand')
        self.assertContains(response, 'Critical Restock Watch')

    def test_seller_dashboard_warns_when_confirmed_booking_is_delayed_for_two_days(self):
        delayed_booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            shipping_address='Delayed shipment lane',
            total_amount=Decimal('44.00'),
            status=Booking.BookingStatus.CONFIRMED,
        )
        delayed_booking.booked_at = timezone.now() - timedelta(days=3)
        delayed_booking.save(update_fields=['booked_at'])

        self.client.force_login(self.seller)
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'delayed more than 2 days without shipment')
        self.assertContains(response, f'#{delayed_booking.id}')


class CustomerStockVisibilityTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='stock-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='stock-seller',
        )
        self.customer = User.objects.create_user(
            email='stock-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='stock-customer',
        )
        state = State.objects.create(name='Stock State', code='SS')
        district = District.objects.create(name='Stock District', state=state)
        location = Location.objects.create(
            district=district,
            name='Stock Town',
            postal_code='600001',
        )
        category = Category.objects.create(name='Stock Category', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            location=location,
            name='Stock Visibility Product',
            description='Stock visibility test product',
            price=Decimal('14.00'),
            stock_quantity=4,
            is_active=True,
        )
        Feedback.objects.create(
            customer=self.customer,
            product=self.product,
            rating=4,
            comment='Great quality',
        )
        self.client.force_login(self.customer)

    def test_customer_dashboard_shows_stock_available_on_product_cards(self):
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Stock available:')
        self.assertContains(response, str(self.product.stock_quantity))
        self.assertContains(response, '4.0/5 (1 review)')

    def test_search_results_shows_stock_available_on_product_cards(self):
        response = self.client.get(reverse('accounts:search_results'), data={'q': 'Stock Visibility'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Stock available:')
        self.assertContains(response, str(self.product.stock_quantity))


class SellerRiskPopupTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='popup-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='popup-seller',
        )
        profile, _ = SellerProfile.objects.get_or_create(
            user=self.seller,
            defaults={'store_name': 'Popup Seller Store'},
        )
        profile.verification_status = SellerProfile.VerificationStatus.FLAGGED
        profile.is_suspended = True
        profile.suspension_note = 'Risk freeze initiated.'
        profile.save(update_fields=['verification_status', 'is_suspended', 'suspension_note'])
        self.incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
            risk_score=81.2,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='Repeated cancellation spikes detected.',
            fine_amount='250.00',
            is_active=True,
        )

    def test_seller_dashboard_shows_red_risk_popup_block(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Seller Account Frozen Action Required')
        self.assertContains(response, 'Open Risk Action Center')
        self.assertContains(response, 'Acknowledge')
        self.assertContains(response, 'Account unlock pending: pay fine')
        self.assertContains(response, 'Pay Fine & Unlock')

    def test_seller_can_acknowledge_dashboard_risk_popup(self):
        self.client.force_login(self.seller)
        ack_response = self.client.post(
            reverse('accounts:acknowledge_seller_risk_action'),
            data={'incident_id': self.incident.id},
        )
        self.assertEqual(ack_response.status_code, 302)
        self.incident.refresh_from_db()
        self.assertIsNotNone(self.incident.seller_acknowledged_at)

        dashboard_response = self.client.get(reverse('accounts:dashboard'))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertNotContains(dashboard_response, 'Seller Account Frozen Action Required')
        self.assertContains(dashboard_response, 'Account unlock pending: pay fine')
