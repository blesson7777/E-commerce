from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import SellerProfile
from accounts.models import User
from analytics.models import RiskModelBacktest
from analytics.models import RiskModelVersion
from analytics.models import RiskRealtimeEvent
from analytics.models import SellerRiskIncident
from analytics.models import SellerRiskSnapshot
from analytics.services import calculate_seller_risk_batch
from analytics.services import freeze_seller_operations
from analytics.services import report_booking_created_event
from analytics.services import report_failed_payment_event
from analytics.services import train_supervised_risk_model
from analytics.services import unfreeze_seller_operations
from catalog.models import Category
from catalog.models import Product
from orders.models import Booking
from orders.models import BookingItem
from orders.models import Transaction
from support.models import Complaint
from support.models import Feedback


class SellerVerificationServiceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='admin@test.com',
            password='Pass@12345',
            role=User.UserRole.ADMIN,
            username='admin-test',
        )
        self.customer = User.objects.create_user(
            email='customer@test.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='customer-test',
        )
        self.seller_safe = User.objects.create_user(
            email='safe-seller@test.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='safe-seller',
        )
        self.seller_risky = User.objects.create_user(
            email='risky-seller@test.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='risky-seller',
        )
        category = Category.objects.create(name='Eco Goods', description='Eco products')
        self.product_safe = Product.objects.create(
            seller=self.seller_safe,
            category=category,
            name='Reusable Straw',
            description='Steel straw',
            price=20,
            stock_quantity=30,
        )
        self.product_risky = Product.objects.create(
            seller=self.seller_risky,
            category=category,
            name='Organic Soap',
            description='Handmade soap bar',
            price=40,
            stock_quantity=30,
        )

        safe_booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller_safe,
            shipping_address='House 1, Green Street',
            total_amount=20,
            status=Booking.BookingStatus.DELIVERED,
        )
        Transaction.objects.create(
            booking=safe_booking,
            amount=20,
            payment_method=Transaction.PaymentMethod.UPI,
            status=Transaction.TransactionStatus.SUCCESS,
            transaction_reference='SAFE000001',
        )
        Feedback.objects.create(
            customer=self.customer,
            product=self.product_safe,
            booking=safe_booking,
            rating=5,
            comment='Great product',
        )

        risky_booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller_risky,
            shipping_address='House 2, River Street',
            total_amount=40,
            status=Booking.BookingStatus.CANCELLED,
        )
        Transaction.objects.create(
            booking=risky_booking,
            amount=40,
            payment_method=Transaction.PaymentMethod.CARD,
            status=Transaction.TransactionStatus.FAILED,
            transaction_reference='RISK000001',
        )
        Complaint.objects.create(
            customer=self.customer,
            booking=risky_booking,
            product=self.product_risky,
            subject='Defective product',
            message='Item quality issue',
        )
        Feedback.objects.create(
            customer=self.customer,
            product=self.product_risky,
            booking=risky_booking,
            rating=1,
            comment='Bad quality',
        )

    def test_batch_verification_creates_snapshots_with_model_fields(self):
        snapshots = calculate_seller_risk_batch()
        self.assertEqual(len(snapshots), 2)
        snapshot = SellerRiskSnapshot.objects.filter(seller=self.seller_risky).first()
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.model_version.startswith('hybrid_v'))
        self.assertIn(
            snapshot.classification_label,
            {
                SellerRiskSnapshot.ClassificationLabel.LOW,
                SellerRiskSnapshot.ClassificationLabel.MEDIUM,
                SellerRiskSnapshot.ClassificationLabel.HIGH,
            },
        )
        self.assertGreaterEqual(snapshot.anomaly_score, 0)

    def test_admin_report_exports_are_available(self):
        calculate_seller_risk_batch()
        self.client.force_login(self.admin)

        report_response = self.client.get(reverse('analytics:reports_export_csv'))
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(report_response['Content-Type'], 'text/csv')
        self.assertIn('naturenest_admin_reports.csv', report_response['Content-Disposition'])

        report_pdf_response = self.client.get(reverse('analytics:reports_export_pdf'))
        self.assertEqual(report_pdf_response.status_code, 200)
        self.assertEqual(report_pdf_response['Content-Type'], 'application/pdf')
        self.assertIn('naturenest_admin_reports.pdf', report_pdf_response['Content-Disposition'])

        verification_response = self.client.get(reverse('analytics:verification_results_export_csv'))
        self.assertEqual(verification_response.status_code, 200)
        self.assertEqual(verification_response['Content-Type'], 'text/csv')
        self.assertIn('naturenest_seller_verification.csv', verification_response['Content-Disposition'])

        verification_pdf_response = self.client.get(reverse('analytics:verification_results_export_pdf'))
        self.assertEqual(verification_pdf_response.status_code, 200)
        self.assertEqual(verification_pdf_response['Content-Type'], 'application/pdf')
        self.assertIn('naturenest_seller_verification.pdf', verification_pdf_response['Content-Disposition'])

        fraud_pdf_response = self.client.get(reverse('analytics:fraud_detection_export_pdf'))
        self.assertEqual(fraud_pdf_response.status_code, 200)
        self.assertEqual(fraud_pdf_response['Content-Type'], 'application/pdf')
        self.assertIn('naturenest_fraud_detection.pdf', fraud_pdf_response['Content-Disposition'])

        incident_pdf_response = self.client.get(reverse('analytics:risk_incident_export_pdf'))
        self.assertEqual(incident_pdf_response.status_code, 200)
        self.assertEqual(incident_pdf_response['Content-Type'], 'application/pdf')
        self.assertIn('naturenest_risk_incidents.pdf', incident_pdf_response['Content-Disposition'])

    def test_admin_can_open_fraud_detection_dashboard(self):
        calculate_seller_risk_batch()
        self.client.force_login(self.admin)
        response = self.client.get(reverse('analytics:fraud_detection_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fraud Detection (ML)')

    def test_manual_unfreeze_blocks_repeat_auto_freeze_without_new_signals(self):
        calculate_seller_risk_batch()
        profile = SellerProfile.objects.get(user=self.seller_risky)
        self.assertTrue(profile.is_suspended)

        incident = SellerRiskIncident.objects.filter(seller=self.seller_risky, is_active=True).first()
        self.assertIsNotNone(incident)
        unfreeze_time = timezone.now()
        incident.status = SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN
        incident.is_active = False
        incident.final_decision_at = unfreeze_time
        incident.save(update_fields=['status', 'is_active', 'final_decision_at', 'updated_at'])
        unfreeze_seller_operations(self.seller_risky, decision_note='Manual admin override')

        calculate_seller_risk_batch()
        profile.refresh_from_db()
        self.assertFalse(profile.is_suspended)
        self.assertFalse(
            SellerRiskIncident.objects.filter(seller=self.seller_risky, is_active=True).exists()
        )

    def test_manual_unfreeze_allows_refreeze_when_new_signals_arrive(self):
        calculate_seller_risk_batch()
        profile = SellerProfile.objects.get(user=self.seller_risky)
        self.assertTrue(profile.is_suspended)

        incident = SellerRiskIncident.objects.filter(seller=self.seller_risky, is_active=True).first()
        self.assertIsNotNone(incident)
        incident.status = SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN
        incident.is_active = False
        incident.final_decision_at = timezone.now()
        incident.save(update_fields=['status', 'is_active', 'final_decision_at', 'updated_at'])
        unfreeze_seller_operations(self.seller_risky, decision_note='Manual admin override')

        Booking.objects.create(
            customer=self.customer,
            seller=self.seller_risky,
            shipping_address='House 3, Green Street',
            total_amount=40,
            status=Booking.BookingStatus.CANCELLED,
            cancellation_reason='Cancelled after verification',
        )

        calculate_seller_risk_batch()
        profile.refresh_from_db()
        self.assertTrue(profile.is_suspended)
        self.assertTrue(
            SellerRiskIncident.objects.filter(seller=self.seller_risky, is_active=True).exists()
        )


class RiskIncidentWorkflowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='incident-admin@test.com',
            password='Pass@12345',
            role=User.UserRole.ADMIN,
            username='incident-admin',
        )
        self.customer = User.objects.create_user(
            email='incident-customer@test.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='incident-customer',
        )
        self.seller = User.objects.create_user(
            email='incident-seller@test.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='incident-seller',
        )
        category = Category.objects.create(name='Incident Category', description='Risk tests')
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            name='Incident Product',
            description='Test item',
            price=25,
            stock_quantity=5,
            is_active=True,
        )
        self.booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            shipping_address='Incident address',
            total_amount=50,
            status=Booking.BookingStatus.CONFIRMED,
        )
        BookingItem.objects.create(
            booking=self.booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )

    def test_freeze_seller_operations_cancels_bookings_and_restores_stock(self):
        freeze_seller_operations(self.seller, incident_note='Incident freeze')
        self.booking.refresh_from_db()
        self.product.refresh_from_db()
        profile = SellerProfile.objects.get(user=self.seller)

        self.assertTrue(profile.is_suspended)
        self.assertEqual(profile.verification_status, SellerProfile.VerificationStatus.FLAGGED)
        self.assertEqual(self.booking.status, Booking.BookingStatus.CANCELLED)
        self.assertEqual(self.product.stock_quantity, 7)

    def test_seller_fine_payment_auto_unfreezes_account(self):
        incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
            risk_score=82.3,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='Risk model flagged unusual behavior',
            fine_amount='250.00',
            is_active=True,
        )
        self.client.force_login(self.seller)

        pay_response = self.client.post(
            reverse('analytics:seller_risk_pay_fine', args=[incident.id]),
            data={
                'payment_method': 'upi',
                'upi_id': 'seller@upi',
            },
        )
        self.assertEqual(pay_response.status_code, 302)
        incident.refresh_from_db()
        self.assertEqual(incident.status, SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN)
        self.assertIsNotNone(incident.fine_paid_at)
        self.assertFalse(incident.is_active)
        profile = SellerProfile.objects.get(user=self.seller)
        self.assertFalse(profile.is_suspended)
        self.assertEqual(profile.verification_status, SellerProfile.VerificationStatus.VERIFIED)

    def test_seller_fine_payment_requires_valid_upi_or_card_details(self):
        incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
            risk_score=77.5,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='Risk model flagged repeated cancellation spikes',
            fine_amount='150.00',
            is_active=True,
        )
        self.client.force_login(self.seller)

        response = self.client.post(
            reverse('analytics:seller_risk_pay_fine', args=[incident.id]),
            data={
                'payment_method': 'upi',
                'upi_id': 'invalid-upi-id',
            },
        )
        self.assertEqual(response.status_code, 400)
        incident.refresh_from_db()
        self.assertEqual(incident.status, SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING)
        self.assertIsNone(incident.fine_paid_at)
        self.assertIn('Enter a valid UPI ID.', response.content.decode())

    def test_admin_can_unfreeze_and_waive_penalty_after_appeal_validation(self):
        incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.APPEALED,
            risk_score=72.0,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='Appeal validation scenario',
            fine_amount='350.00',
            appeal_text='Gateway issue caused temporary failures, issue is now resolved.',
            appealed_at=timezone.now(),
            is_active=True,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('analytics:risk_incident_finalize', args=[incident.id]),
            data={
                'decision': 'unfreeze',
                'decision_note': 'Appeal validated and evidence accepted.',
                'waive_fine': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)
        incident.refresh_from_db()
        self.assertEqual(incident.status, SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN)
        self.assertFalse(incident.is_active)
        self.assertEqual(float(incident.fine_amount), 0.0)
        self.assertEqual(float(incident.risk_score), 0.0)
        self.assertEqual(incident.classification_label, SellerRiskSnapshot.ClassificationLabel.LOW)
        profile = SellerProfile.objects.get(user=self.seller)
        self.assertFalse(profile.is_suspended)
        self.assertEqual(profile.verification_status, SellerProfile.VerificationStatus.VERIFIED)

    def test_admin_cannot_waive_penalty_without_appeal_submission(self):
        incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
            risk_score=68.0,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='No appeal submitted',
            fine_amount='210.00',
            is_active=True,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('analytics:risk_incident_finalize', args=[incident.id]),
            data={
                'decision': 'unfreeze',
                'decision_note': 'Attempted waive without appeal.',
                'waive_fine': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)
        incident.refresh_from_db()
        self.assertTrue(incident.is_active)
        self.assertEqual(incident.status, SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING)
        self.assertEqual(float(incident.fine_amount), 210.0)

    def test_admin_can_terminate_seller_in_final_decision(self):
        incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.UNDER_REVIEW,
            risk_score=94.0,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='Repeated high-risk incidents with severe signals.',
            fine_amount='500.00',
            is_active=True,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('analytics:risk_incident_finalize', args=[incident.id]),
            data={
                'decision': 'terminate',
                'decision_note': 'Confirmed repeated fraud pattern. Account terminated.',
            },
        )
        self.assertEqual(response.status_code, 302)
        incident.refresh_from_db()
        self.assertFalse(incident.is_active)
        self.assertEqual(incident.status, SellerRiskIncident.IncidentStatus.RESOLVED_TERMINATED)
        profile = SellerProfile.objects.get(user=self.seller)
        self.assertTrue(profile.is_suspended)
        self.assertEqual(profile.verification_status, SellerProfile.VerificationStatus.REJECTED)

    def test_admin_keep_frozen_shows_warning_when_fine_unpaid(self):
        incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
            risk_score=79.0,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='Fine unpaid case',
            fine_amount='180.00',
            is_active=True,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('analytics:risk_incident_finalize', args=[incident.id]),
            data={
                'decision': 'keep_frozen',
                'decision_note': 'Fine not paid by seller.',
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        incident.refresh_from_db()
        self.assertTrue(incident.is_active)
        self.assertEqual(incident.status, SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING)
        self.assertContains(response, 'has not paid the required fine')

    def test_seller_appeal_acknowledges_freeze_and_warns_to_pay(self):
        profile, _ = SellerProfile.objects.get_or_create(
            user=self.seller,
            defaults={'store_name': 'Appeal Seller Store'},
        )
        profile.is_suspended = True
        profile.verification_status = SellerProfile.VerificationStatus.FLAGGED
        profile.save(update_fields=['is_suspended', 'verification_status'])

        incident = SellerRiskIncident.objects.create(
            seller=self.seller,
            status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
            risk_score=74.5,
            classification_label=SellerRiskSnapshot.ClassificationLabel.HIGH,
            incident_reason='Appeal warning flow',
            fine_amount='190.00',
            is_active=True,
        )
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('analytics:seller_risk_submit_appeal', args=[incident.id]),
            data={'appeal_text': 'Payment delay happened due to temporary account hold. Please review logs.'},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        incident.refresh_from_db()
        self.assertEqual(incident.status, SellerRiskIncident.IncidentStatus.APPEALED)
        self.assertIsNotNone(incident.seller_acknowledged_at)
        self.assertContains(response, 'Seller account remains frozen. Pay the fine to auto-unfreeze')


class RiskRealtimeAndModelPipelineTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            email='pipeline-customer@test.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='pipeline-customer',
        )
        self.seller = User.objects.create_user(
            email='pipeline-seller@test.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='pipeline-seller',
        )
        category = Category.objects.create(name='Pipeline Category', description='Pipeline tests')
        self.product = Product.objects.create(
            seller=self.seller,
            category=category,
            name='Pipeline Product',
            description='Realtime/model test item',
            price=30,
            stock_quantity=40,
        )

    def test_realtime_booking_and_failed_payment_events_are_recorded(self):
        booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            shipping_address='Pipeline Address',
            total_amount=60,
            status=Booking.BookingStatus.PENDING,
        )
        BookingItem.objects.create(
            booking=booking,
            product=self.product,
            quantity=2,
            unit_price=self.product.price,
        )

        snapshot, event = report_booking_created_event(
            booking=booking,
            payload={
                'ip_address': '10.10.10.10',
                'device_fingerprint': 'pytest-device-a',
            },
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(event.event_type, RiskRealtimeEvent.EventType.BOOKING_CREATED)
        self.assertEqual(event.booking_id, booking.id)

        failed_tx = Transaction.objects.create(
            booking=booking,
            amount=booking.total_amount,
            payment_method=Transaction.PaymentMethod.UPI,
            status=Transaction.TransactionStatus.FAILED,
            transaction_reference='PIPEFAIL0001',
        )
        payment_snapshot, _incident, payment_event = report_failed_payment_event(
            transaction_obj=failed_tx,
            payload={'payment_handle': 'fail@upi'},
        )
        self.assertIsNotNone(payment_snapshot)
        self.assertEqual(payment_event.event_type, RiskRealtimeEvent.EventType.PAYMENT_FAILED)
        self.assertEqual(payment_event.transaction_id, failed_tx.id)

    def test_supervised_training_creates_version_and_backtest(self):
        seller_b = User.objects.create_user(
            email='pipeline-seller-b@test.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='pipeline-seller-b',
        )

        for idx in range(20):
            seller = self.seller if idx % 2 == 0 else seller_b
            positive = idx % 3 != 0
            feature_vector = {
                'complaint_ratio': 0.65 if positive else 0.08,
                'failed_transaction_ratio': 0.72 if positive else 0.05,
                'low_rating_ratio': 0.44 if positive else 0.09,
                'cancellation_ratio': 0.60 if positive else 0.06,
                'stale_pending_ratio': 0.25 if positive else 0.03,
                'network_risk_score': 0.58 if positive else 0.05,
                'sequence_risk_score': 0.61 if positive else 0.04,
                'booking_volume_30d': 0.42,
                'cancel_count_30d': 0.70 if positive else 0.05,
                'failed_payment_count_30d': 0.75 if positive else 0.04,
                'complaint_count_30d': 0.62 if positive else 0.07,
                'shared_phone_degree': 0.40 if positive else 0.03,
                'shared_address_degree': 0.36 if positive else 0.02,
                'shared_device_degree': 0.31 if positive else 0.02,
                'shared_ip_degree': 0.33 if positive else 0.01,
                'shared_payment_handle_degree': 0.48 if positive else 0.01,
                'cancel_spike_factor': 0.80 if positive else 0.06,
                'failed_payment_spike_factor': 0.77 if positive else 0.05,
                'event_booking_created': 0.0,
                'event_booking_cancelled': 1.0 if positive else 0.0,
                'event_payment_failed': 1.0 if positive else 0.0,
                'anomaly_score_hint': 0.82 if positive else 0.08,
                'risk_velocity_hint': 0.30 if positive else 0.01,
            }
            snapshot = SellerRiskSnapshot.objects.create(
                seller=seller,
                risk_score=88.0 if positive else 18.0,
                complaint_ratio=feature_vector['complaint_ratio'],
                failed_transaction_ratio=feature_vector['failed_transaction_ratio'],
                low_rating_ratio=feature_vector['low_rating_ratio'],
                cancellation_ratio=feature_vector['cancellation_ratio'],
                stale_pending_ratio=feature_vector['stale_pending_ratio'],
                anomaly_score=85.0 if positive else 9.0,
                confidence_score=70.0,
                risk_velocity=14.0 if positive else -2.0,
                model_probability=0.84 if positive else 0.16,
                calibrated_probability=0.86 if positive else 0.14,
                decision_threshold=0.70,
                drift_score=0.0,
                network_risk_score=58.0 if positive else 5.0,
                sequence_risk_score=61.0 if positive else 4.0,
                classification_label=(
                    SellerRiskSnapshot.ClassificationLabel.HIGH
                    if positive
                    else SellerRiskSnapshot.ClassificationLabel.LOW
                ),
                model_version='hybrid_v2_seed',
                feature_vector=feature_vector,
                top_contributors=[],
                risk_factors=[],
                is_flagged=positive,
            )
            SellerRiskIncident.objects.create(
                seller=seller,
                snapshot=snapshot,
                status=(
                    SellerRiskIncident.IncidentStatus.RESOLVED_FROZEN
                    if positive
                    else SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN
                ),
                risk_score=snapshot.risk_score,
                classification_label=snapshot.classification_label,
                incident_reason='Synthetic training sample',
                fine_amount='0.00',
                final_decision_at=timezone.now(),
                is_active=False,
            )

        model = train_supervised_risk_model(force=True, reason='unit_test')
        self.assertIsNotNone(model)
        self.assertTrue(model.version.startswith('hybrid_v'))
        self.assertTrue(RiskModelVersion.objects.filter(algorithm='supervised_logistic_v1').exists())
        self.assertTrue(RiskModelBacktest.objects.filter(model__algorithm='supervised_logistic_v1').exists())
