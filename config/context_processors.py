from django.conf import settings

from accounts.models import SellerProfile
from analytics.models import SellerRiskIncident
from orders.models import Booking
from orders.models import BookingItem
from orders.models import Transaction
from support.models import Complaint
from support.models import Feedback
from catalog.models import Product
from catalog.cart import cart_snapshot


NOTIFICATION_DISMISS_SESSION_KEY = 'ui_notifications_dismissed'


def _notification_signature(notification):
    return (
        f"{notification.get('icon', '')}|"
        f"{notification.get('title', '')}|"
        f"{notification.get('description', '')}|"
        f"{notification.get('url', '')}|"
        f"{notification.get('count', 0)}"
    )


def _notification_user_key(user):
    return f'{user.id}:{user.role}'


def _dismissed_signatures_for_user(request, user):
    dismissed_map = request.session.get(NOTIFICATION_DISMISS_SESSION_KEY, {})
    if not isinstance(dismissed_map, dict):
        return set()
    raw = dismissed_map.get(_notification_user_key(user), [])
    if not isinstance(raw, list):
        return set()
    return {str(value) for value in raw}


def set_notifications_dismissed_for_user(request, user, notifications):
    dismissed_map = request.session.get(NOTIFICATION_DISMISS_SESSION_KEY, {})
    if not isinstance(dismissed_map, dict):
        dismissed_map = {}
    dismissed_map[_notification_user_key(user)] = [
        _notification_signature(notification) for notification in notifications
    ]
    request.session[NOTIFICATION_DISMISS_SESSION_KEY] = dismissed_map
    request.session.modified = True


def build_role_notifications(user):
    notifications = []

    if user.role == 'admin':
        pending_bookings = Booking.objects.filter(status=Booking.BookingStatus.PENDING).count()
        open_complaints = Complaint.objects.filter(
            status__in=[Complaint.ComplaintStatus.OPEN, Complaint.ComplaintStatus.IN_PROGRESS]
        ).count()
        flagged_sellers = SellerProfile.objects.filter(
            verification_status=SellerProfile.VerificationStatus.FLAGGED
        ).count()
        open_risk_incidents = SellerRiskIncident.objects.filter(is_active=True).count()
        pending_cancellation_reviews = Booking.objects.filter(
            status=Booking.BookingStatus.CANCELLED,
            cancelled_by_role='customer',
            cancellation_impact=Booking.CancellationImpact.NOT_REVIEWED,
        ).count()
        refunded_transactions = Transaction.objects.filter(
            status=Transaction.TransactionStatus.REFUNDED
        ).count()

        if pending_bookings:
            notifications.append({
                'icon': 'shopping-cart',
                'title': 'Pending bookings',
                'description': f'{pending_bookings} bookings need review.',
                'url': '/orders/list/',
                'count': pending_bookings,
            })
        if open_complaints:
            notifications.append({
                'icon': 'alert-triangle',
                'title': 'Open complaints',
                'description': f'{open_complaints} complaints need attention.',
                'url': '/support/complaints/',
                'count': open_complaints,
            })
        if flagged_sellers:
            notifications.append({
                'icon': 'shield-off',
                'title': 'Flagged sellers',
                'description': f'{flagged_sellers} sellers are flagged by verification.',
                'url': '/analytics/seller-verification/results/',
                'count': flagged_sellers,
            })
        if open_risk_incidents:
            notifications.append({
                'icon': 'alert-octagon',
                'title': 'Risk incidents',
                'description': f'{open_risk_incidents} seller risk incidents need final decision.',
                'url': '/analytics/risk-incidents/',
                'count': open_risk_incidents,
            })
        if pending_cancellation_reviews:
            notifications.append({
                'icon': 'alert-circle',
                'title': 'Cancellation monitoring',
                'description': f'{pending_cancellation_reviews} customer cancellations need anomaly/ignore review.',
                'url': '/orders/cancellations/review/',
                'count': pending_cancellation_reviews,
            })
        if refunded_transactions:
            notifications.append({
                'icon': 'rotate-ccw',
                'title': 'Refunded payments',
                'description': f'{refunded_transactions} refunds were processed after cancellations.',
                'url': '/orders/transactions/',
                'count': refunded_transactions,
            })

    elif user.role == 'seller':
        seller_profile = SellerProfile.objects.filter(user=user).first()
        active_risk_incidents = SellerRiskIncident.objects.filter(seller=user, is_active=True).count()
        seller_is_terminated = bool(
            seller_profile
            and seller_profile.verification_status == SellerProfile.VerificationStatus.REJECTED
        )
        seller_is_suspended = bool(
            seller_profile
            and (seller_profile.is_suspended or seller_is_terminated)
        )
        category_non_listed_products = Product.objects.filter(
            seller=user,
            category__is_active=False,
        ).count()
        pending_orders = Booking.objects.filter(
            seller=user,
            status=Booking.BookingStatus.PENDING,
        ).count()
        in_progress_complaints = Complaint.objects.filter(
            booking__seller=user,
            status__in=[Complaint.ComplaintStatus.OPEN, Complaint.ComplaintStatus.IN_PROGRESS],
        ).count()
        low_stock_items = Product.objects.filter(seller=user, stock_quantity__lt=5, is_active=True).count()
        refunded_transactions = Transaction.objects.filter(
            booking__seller=user,
            status=Transaction.TransactionStatus.REFUNDED,
        ).count()

        if pending_orders:
            notifications.append({
                'icon': 'clock',
                'title': 'New bookings',
                'description': f'{pending_orders} bookings are waiting for action.',
                'url': '/orders/list/',
                'count': pending_orders,
            })
        if in_progress_complaints:
            notifications.append({
                'icon': 'message-circle',
                'title': 'Customer issues',
                'description': f'{in_progress_complaints} complaint threads are active.',
                'url': '/support/feedback/',
                'count': in_progress_complaints,
            })
        if low_stock_items:
            notifications.append({
                'icon': 'package',
                'title': 'Low stock alert',
                'description': f'{low_stock_items} products are below 5 units.',
                'url': '/catalog/seller/restocking/',
                'count': low_stock_items,
            })
        if category_non_listed_products:
            notifications.append({
                'icon': 'x-circle',
                'title': 'Category non-listed now',
                'description': (
                    f'{category_non_listed_products} product(s) are in categories turned Off '
                    'and hidden from booking/dashboard.'
                ),
                'url': '/catalog/seller/inventory/',
                'count': category_non_listed_products,
            })
        if active_risk_incidents:
            notifications.append({
                'icon': 'alert-octagon',
                'title': 'Risk incident action',
                'description': 'Pay fine or submit appeal for your active risk incident.',
                'url': '/analytics/seller-risk/incident/',
                'count': active_risk_incidents,
            })
        if seller_is_terminated:
            notifications.append({
                'icon': 'x-octagon',
                'title': 'Selling account terminated',
                'description': 'Seller operations are terminated. Contact admin for final review details.',
                'url': '/analytics/seller-risk/incident/',
                'count': 1,
            })
        elif seller_is_suspended:
            notifications.append({
                'icon': 'slash',
                'title': 'Selling account frozen',
                'description': 'Products and booking operations are paused pending admin review.',
                'url': '/analytics/seller-risk/incident/',
                'count': 1,
            })
        if refunded_transactions:
            notifications.append({
                'icon': 'rotate-ccw',
                'title': 'Refund updates',
                'description': f'{refunded_transactions} paid booking(s) were refunded after cancellation.',
                'url': '/orders/transactions/',
                'count': refunded_transactions,
            })

    else:
        unpaid_bookings_qs = Booking.objects.filter(
            customer=user,
            status=Booking.BookingStatus.PENDING,
        ).order_by('-booked_at')
        unpaid_count = unpaid_bookings_qs.count()
        first_unpaid = unpaid_bookings_qs.first()
        active_orders = Booking.objects.filter(
            customer=user,
            status__in=[
                Booking.BookingStatus.CONFIRMED,
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
            ],
        ).count()
        unresolved_complaints = Complaint.objects.filter(
            customer=user,
            status__in=[Complaint.ComplaintStatus.OPEN, Complaint.ComplaintStatus.IN_PROGRESS],
        ).count()
        delivered_item_pairs = set(
            BookingItem.objects.filter(
                booking__customer=user,
                booking__status=Booking.BookingStatus.DELIVERED,
            ).values_list('booking_id', 'product_id')
        )
        reviewed_item_pairs = set(
            Feedback.objects.filter(
                customer=user,
                booking__isnull=False,
                product__isnull=False,
            ).values_list('booking_id', 'product_id')
        )
        pending_review_pairs = sorted(delivered_item_pairs - reviewed_item_pairs)
        pending_reviews = len(pending_review_pairs)
        refunded_transactions_qs = Transaction.objects.filter(
            booking__customer=user,
            status=Transaction.TransactionStatus.REFUNDED,
        ).order_by('-created_at')
        refunded_count = refunded_transactions_qs.count()
        first_refunded = refunded_transactions_qs.first()

        if active_orders:
            notifications.append({
                'icon': 'truck',
                'title': 'Orders in transit',
                'description': f'{active_orders} orders are on the way.',
                'url': '/orders/list/',
                'count': active_orders,
            })
        if unpaid_count and first_unpaid:
            notifications.append({
                'icon': 'credit-card',
                'title': 'Payment pending',
                'description': f'{unpaid_count} booking(s) are waiting for payment confirmation.',
                'url': f'/orders/{first_unpaid.id}/pay/',
                'count': unpaid_count,
            })
        if unresolved_complaints:
            notifications.append({
                'icon': 'help-circle',
                'title': 'Support updates',
                'description': f'{unresolved_complaints} complaints are still open.',
                'url': '/support/complaints/',
                'count': unresolved_complaints,
            })
        if pending_reviews:
            first_booking_id, first_product_id = pending_review_pairs[0]
            notifications.append({
                'icon': 'star',
                'title': 'Review delivered items',
                'description': f'{pending_reviews} delivered item(s) are waiting for your feedback.',
                'url': f'/support/feedback/new/?booking={first_booking_id}&product={first_product_id}',
                'count': pending_reviews,
            })
        if refunded_count and first_refunded:
            notifications.append({
                'icon': 'rotate-ccw',
                'title': 'Refund processed',
                'description': f'{refunded_count} cancelled paid order(s) have refund updates.',
                'url': f'/orders/transactions/{first_refunded.id}/',
                'count': refunded_count,
            })

    return notifications


def ui_notifications(request):
    empty_cart = {
        'cart_items': [],
        'cart_unavailable_items': [],
        'cart_item_count': 0,
        'cart_available_item_count': 0,
        'cart_unavailable_count': 0,
        'cart_total_amount': 0,
        'cart_checkout_blocked': False,
    }

    if not request.user.is_authenticated:
        return {
            'ui_notifications': [],
            'ui_notification_count': 0,
            'use_cdn_assets': bool(getattr(settings, 'USE_CDN_ASSETS', True)),
            **empty_cart,
        }

    user = request.user
    raw_notifications = build_role_notifications(user)
    dismissed_signatures = _dismissed_signatures_for_user(request, user)
    notifications = [
        notification
        for notification in raw_notifications
        if _notification_signature(notification) not in dismissed_signatures
    ]

    notification_count = sum(item['count'] for item in notifications)
    if user.role == 'customer':
        cart_data = cart_snapshot(request)
    else:
        cart_data = empty_cart

    return {
        'ui_notifications': notifications,
        'ui_notification_count': notification_count,
        'use_cdn_assets': bool(getattr(settings, 'USE_CDN_ASSETS', True)),
        **cart_data,
    }
