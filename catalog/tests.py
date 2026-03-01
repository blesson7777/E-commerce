from datetime import timedelta
from decimal import Decimal

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from catalog.cart import CART_SESSION_KEY
from catalog.cart import cart_snapshot
from catalog.delivery_prediction import predict_delivery_for_product
from catalog.models import Category
from catalog.models import Product
from catalog.restock_prediction import attach_restock_predictions
from orders.models import Booking
from orders.models import BookingItem
from support.models import Feedback


class CategoryAccessTests(TestCase):
    def setUp(self):
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

    def test_seller_can_open_category_management_page(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('catalog:category_list'))
        self.assertEqual(response.status_code, 200)

    def test_seller_can_create_category(self):
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('catalog:category_list'),
            data={
                'name': 'Compostables',
                'description': 'Eco compostable items',
                'is_active': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Category.objects.filter(name='Compostables').exists())

    def test_customer_cannot_access_category_management(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('catalog:category_list'))
        self.assertEqual(response.status_code, 302)


class ProductManagementTests(TestCase):
    def setUp(self):
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
        self.category = Category.objects.create(
            name='Compostable Packs',
            description='Eco products',
            is_active=True,
        )
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name='Bamboo Toothbrush',
            description='Natural bamboo brush',
            price=Decimal('3.50'),
            stock_quantity=5,
            is_active=True,
        )

    def test_seller_can_update_stock(self):
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse('catalog:seller_product_update_stock', args=[self.product.id]),
            data={
                'stock_quantity': '12',
                'next': reverse('catalog:product_list'),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 12)

    def test_admin_can_toggle_product_availability(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('catalog:admin_product_toggle_availability', args=[self.product.id]),
            data={
                'is_active': 'off',
                'next': reverse('catalog:product_list'),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)

    def test_admin_can_delete_product(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('catalog:admin_product_delete', args=[self.product.id]),
            data={'next': reverse('catalog:product_list')},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Product.objects.filter(id=self.product.id).exists())


class CartSnapshotCategoryAvailabilityTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seller = User.objects.create_user(
            email='cart-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='cart-seller',
        )
        self.customer = User.objects.create_user(
            email='cart-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='cart-customer',
        )
        self.category = Category.objects.create(
            name='Cart Category',
            description='Cart test category',
            is_active=True,
        )
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name='Cart Product',
            description='Cart snapshot test product',
            price=Decimal('10.00'),
            stock_quantity=8,
            is_active=True,
        )

    def _request_with_session(self):
        request = self.factory.get('/')
        request.user = self.customer
        middleware = SessionMiddleware(lambda r: None)
        middleware.process_request(request)
        request.session.save()
        return request

    def test_cart_snapshot_marks_products_in_off_categories_as_unavailable(self):
        request = self._request_with_session()
        request.session[CART_SESSION_KEY] = {str(self.product.id): 2}
        request.session.save()

        self.category.is_active = False
        self.category.save(update_fields=['is_active'])

        snapshot = cart_snapshot(request)
        self.assertEqual(snapshot['cart_item_count'], 2)
        self.assertEqual(snapshot['cart_available_item_count'], 0)
        self.assertEqual(snapshot['cart_unavailable_count'], 1)
        self.assertTrue(snapshot['cart_checkout_blocked'])
        self.assertEqual(len(snapshot['cart_unavailable_items']), 1)
        self.assertEqual(snapshot['cart_unavailable_items'][0]['product_id'], self.product.id)
        self.assertEqual(request.session.get(CART_SESSION_KEY), {str(self.product.id): 2})


class CartQuantityLimitWarningTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='limit-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='limit-seller',
        )
        self.customer = User.objects.create_user(
            email='limit-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='limit-customer',
        )
        self.category = Category.objects.create(
            name='Limit Category',
            description='Limit test category',
            is_active=True,
        )
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name='Limited Stock Product',
            description='Only one in stock',
            price=Decimal('8.00'),
            stock_quantity=1,
            is_active=True,
        )
        self.client.force_login(self.customer)

    def test_cart_add_ajax_returns_warning_once_stock_limit_is_reached(self):
        add_url = reverse('catalog:cart_add', args=[self.product.id])
        headers = {
            'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest',
            'HTTP_ACCEPT': 'application/json',
        }
        first_response = self.client.post(add_url, data={'quantity': 1}, **headers)
        self.assertEqual(first_response.status_code, 200)
        first_payload = first_response.json()
        self.assertTrue(first_payload.get('ok'))
        self.assertFalse(first_payload.get('warning', False))

        second_response = self.client.post(add_url, data={'quantity': 1}, **headers)
        self.assertEqual(second_response.status_code, 200)
        second_payload = second_response.json()
        self.assertTrue(second_payload.get('ok'))
        self.assertTrue(second_payload.get('warning'))
        self.assertIn('Only 1 item available', second_payload.get('message', ''))
        self.assertEqual(
            self.client.session.get(CART_SESSION_KEY),
            {str(self.product.id): 1},
        )

class DeliveryPredictionTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='delivery-predict-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='delivery-predict-seller',
        )
        self.customer = User.objects.create_user(
            email='delivery-predict-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='delivery-predict-customer',
        )
        self.category = Category.objects.create(
            name='Delivery Predict Category',
            is_active=True,
        )
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name='Delivery Predict Product',
            description='Prediction test product',
            price=Decimal('20.00'),
            stock_quantity=15,
            is_active=True,
        )

    def _create_historical_booking(self, lead_days, *, booked_days_ago=15):
        booking = Booking.objects.create(
            customer=self.customer,
            seller=self.seller,
            shipping_address='Prediction street',
            status=Booking.BookingStatus.DELIVERED,
            total_amount=Decimal('20.00'),
            expected_delivery_date=timezone.localdate() - timedelta(days=booked_days_ago - lead_days),
        )
        booking.booked_at = timezone.now() - timedelta(days=booked_days_ago)
        booking.save(update_fields=['booked_at'])
        BookingItem.objects.create(
            booking=booking,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )

    def test_predict_delivery_defaults_to_ten_days_without_history(self):
        prediction = predict_delivery_for_product(self.product, booking_date=timezone.localdate())
        self.assertEqual(prediction.days, 10)
        self.assertTrue(prediction.is_fallback)
        self.assertEqual(prediction.source, 'default')

    def test_predict_delivery_uses_product_history(self):
        for idx in range(5):
            self._create_historical_booking(lead_days=4, booked_days_ago=20 + idx)
        prediction = predict_delivery_for_product(self.product, booking_date=timezone.localdate())
        self.assertLessEqual(prediction.days, 6)
        self.assertFalse(prediction.is_fallback)
        self.assertEqual(prediction.source, 'product')

    def test_product_detail_renders_estimated_delivery_line(self):
        response = self.client.get(reverse('catalog:product_detail', args=[self.product.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Estimated delivery:')
        self.assertContains(response, '10 days from booking')
        self.assertContains(response, 'Read Reviews')
        self.assertContains(response, f"{reverse('support:feedback_list')}?product={self.product.id}")
        self.assertContains(response, 'No ratings yet')

    def test_product_detail_renders_average_rating_when_feedback_exists(self):
        Feedback.objects.create(
            customer=self.customer,
            product=self.product,
            rating=5,
            comment='Excellent',
        )
        response = self.client.get(reverse('catalog:product_detail', args=[self.product.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '5.0/5 (1 review)')


class SellerRestockDashboardTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email='restock-seller@example.com',
            password='Pass@12345',
            role=User.UserRole.SELLER,
            username='restock-seller',
        )
        self.customer = User.objects.create_user(
            email='restock-customer@example.com',
            password='Pass@12345',
            role=User.UserRole.CUSTOMER,
            username='restock-customer',
        )
        self.category = Category.objects.create(name='Restock Category', is_active=True)
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name='Restock Product',
            description='Restock test product',
            price=Decimal('11.00'),
            stock_quantity=3,
            is_active=True,
        )

    def test_seller_can_open_dedicated_restock_dashboard(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('catalog:seller_restock_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Dedicated stock page with ML-based expected restock date prediction')
        self.assertContains(response, self.product.name)

    def test_customer_cannot_open_restock_dashboard(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('catalog:seller_restock_dashboard'))
        self.assertEqual(response.status_code, 302)

    def test_restock_prediction_fields_are_attached(self):
        attach_restock_predictions([self.product], reorder_level=5)
        self.assertTrue(hasattr(self.product, 'predicted_restock_date'))
        self.assertTrue(hasattr(self.product, 'predicted_stockout_date'))
        self.assertTrue(hasattr(self.product, 'predicted_daily_demand'))
