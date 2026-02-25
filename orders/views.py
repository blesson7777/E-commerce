from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Count
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone

from accounts.decorators import role_required
from accounts.models import User
from analytics.services import report_booking_created_event
from analytics.services import report_cancellation_anomaly_for_booking
from analytics.services import report_failed_payment_event
from catalog.cart import cart_snapshot
from catalog.cart import get_cart
from catalog.cart import save_cart
from catalog.delivery_prediction import predict_delivery_for_product
from catalog.models import Product
from locations.models import Location
from orders.forms import BookingCreateForm
from orders.forms import BookingCancellationImpactForm
from orders.forms import BookingCancelForm
from orders.forms import CartCheckoutForm
from orders.forms import BookingStatusForm
from orders.forms import TransactionForm
from orders.models import Booking
from orders.models import BookingItem
from orders.models import Transaction
from support.models import Feedback


CART_CHECKOUT_PENDING_BOOKINGS_SESSION_KEY = 'cart_checkout_pending_booking_ids'


def _format_address_with_service_area(shipping_address, delivery_location):
    state_name = (
        delivery_location.district.state.name
        if delivery_location.district_id and delivery_location.district.state_id
        else ''
    )
    return (
        f'{shipping_address}\n'
        f'Pincode: {delivery_location.postal_code} - '
        f'{delivery_location.name}, {delivery_location.district.name}, {state_name}'
    )


def _extract_raw_shipping_address(shipping_address):
    value = (shipping_address or '').strip()
    if not value:
        return ''
    if '\nPincode:' in value:
        base = value.split('\nPincode:', 1)[0].strip()
        return base or value
    return value


def _save_customer_address_if_missing(customer, shipping_address, delivery_location):
    if not customer or not delivery_location:
        return

    raw_shipping_address = _extract_raw_shipping_address(shipping_address)
    if not raw_shipping_address:
        return

    existing = (
        customer.saved_addresses.filter(
            address__iexact=raw_shipping_address,
            location=delivery_location,
        ).first()
    )
    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.save(update_fields=['is_active'])
        return

    has_active_default = customer.saved_addresses.filter(is_active=True, is_default=True).exists()
    label = 'Home' if not has_active_default else f'Address {customer.saved_addresses.count() + 1}'
    customer.saved_addresses.create(
        label=label,
        address=raw_shipping_address,
        location=delivery_location,
        is_default=not has_active_default,
        is_active=True,
    )


def _sync_saved_addresses_from_previous_bookings(customer, limit=25):
    recent_bookings = (
        Booking.objects.select_related('delivery_location')
        .filter(customer=customer, delivery_location__isnull=False)
        .exclude(shipping_address='')
        .order_by('-booked_at')[:limit]
    )
    for booking in recent_bookings:
        _save_customer_address_if_missing(customer, booking.shipping_address, booking.delivery_location)


def _requested_cart_quantities(request):
    quantities = {}
    for product_id, quantity in get_cart(request).items():
        try:
            pid = int(product_id)
            qty = int(quantity)
        except (TypeError, ValueError):
            continue
        if pid > 0 and qty > 0:
            quantities[pid] = qty
    return quantities


def _get_cart_checkout_pending_booking_ids(request):
    raw_ids = request.session.get(CART_CHECKOUT_PENDING_BOOKINGS_SESSION_KEY, [])
    if not isinstance(raw_ids, list):
        raw_ids = []
    cleaned = []
    seen = set()
    for value in raw_ids:
        try:
            booking_id = int(value)
        except (TypeError, ValueError):
            continue
        if booking_id <= 0 or booking_id in seen:
            continue
        seen.add(booking_id)
        cleaned.append(booking_id)
    if cleaned != raw_ids:
        request.session[CART_CHECKOUT_PENDING_BOOKINGS_SESSION_KEY] = cleaned
        request.session.modified = True
    return cleaned


def _set_cart_checkout_pending_booking_ids(request, booking_ids):
    cleaned = []
    seen = set()
    for value in booking_ids:
        try:
            booking_id = int(value)
        except (TypeError, ValueError):
            continue
        if booking_id <= 0 or booking_id in seen:
            continue
        seen.add(booking_id)
        cleaned.append(booking_id)
    if cleaned:
        request.session[CART_CHECKOUT_PENDING_BOOKINGS_SESSION_KEY] = cleaned
    else:
        request.session.pop(CART_CHECKOUT_PENDING_BOOKINGS_SESSION_KEY, None)
    request.session.modified = True


def _generate_transaction_reference():
    for _ in range(5):
        reference = uuid4().hex[:12].upper()
        if not Transaction.objects.filter(transaction_reference=reference).exists():
            return reference
    return uuid4().hex[:12].upper()


def _realtime_payload_from_request(request, **extra):
    payload = {
        'ip_address': (request.META.get('HTTP_X_FORWARDED_FOR') or request.META.get('REMOTE_ADDR') or '').split(',')[0].strip(),
        'device_fingerprint': request.META.get('HTTP_USER_AGENT', ''),
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _should_simulate_payment_failure(request, form, method):
    flag = str(request.POST.get('simulate_failure') or '').strip().lower()
    if flag in {'1', 'true', 'yes', 'on'}:
        return True
    if method == Transaction.PaymentMethod.CARD:
        card_number = str(form.cleaned_data.get('card_number') or '').replace(' ', '')
        return card_number.endswith('0000')
    if method == Transaction.PaymentMethod.UPI:
        upi_id = str(form.cleaned_data.get('upi_id') or '').strip().lower()
        return upi_id.startswith('fail@')
    return False


def _is_seller_suspended(seller_user):
    try:
        profile = seller_user.seller_profile
        return bool(
            profile.is_suspended
            or profile.verification_status == 'rejected'
        )
    except ObjectDoesNotExist:
        return False


def _suspension_block_message():
    return 'Seller operations are currently frozen/terminated after risk review.'


def _status_transition_map_for_user(user):
    if user.role == User.UserRole.ADMIN:
        return {
            Booking.BookingStatus.PENDING: {
                Booking.BookingStatus.PENDING,
                Booking.BookingStatus.CANCELLED,
            },
            Booking.BookingStatus.CONFIRMED: {
                Booking.BookingStatus.PENDING,
                Booking.BookingStatus.CONFIRMED,
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.CANCELLED,
                Booking.BookingStatus.DELIVERED,
            },
            Booking.BookingStatus.SHIPPED: {
                Booking.BookingStatus.CONFIRMED,
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.CANCELLED,
                Booking.BookingStatus.DELIVERED,
            },
            Booking.BookingStatus.OUT_FOR_DELIVERY: {
                Booking.BookingStatus.CONFIRMED,
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.CANCELLED,
                Booking.BookingStatus.DELIVERED,
            },
            Booking.BookingStatus.DELIVERED: {Booking.BookingStatus.DELIVERED},
            Booking.BookingStatus.CANCELLED: {
                Booking.BookingStatus.CANCELLED,
                Booking.BookingStatus.PENDING,
                Booking.BookingStatus.CONFIRMED,
            },
        }
    if user.role == User.UserRole.SELLER:
        return {
            Booking.BookingStatus.PENDING: {
                Booking.BookingStatus.PENDING,
                Booking.BookingStatus.CANCELLED,
            },
            Booking.BookingStatus.CONFIRMED: {
                Booking.BookingStatus.CONFIRMED,
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.CANCELLED,
            },
            Booking.BookingStatus.SHIPPED: {
                Booking.BookingStatus.SHIPPED,
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.DELIVERED,
                Booking.BookingStatus.CANCELLED,
            },
            Booking.BookingStatus.OUT_FOR_DELIVERY: {
                Booking.BookingStatus.OUT_FOR_DELIVERY,
                Booking.BookingStatus.DELIVERED,
                Booking.BookingStatus.CANCELLED,
            },
            Booking.BookingStatus.DELIVERED: {Booking.BookingStatus.DELIVERED},
            Booking.BookingStatus.CANCELLED: {Booking.BookingStatus.CANCELLED},
        }
    return {}


def _is_status_transition_allowed(user, current_status, new_status):
    mapping = _status_transition_map_for_user(user)
    allowed = mapping.get(current_status, {current_status})
    return new_status in allowed


def _apply_stock_changes_for_status_transition(booking, old_status, new_status):
    if old_status == new_status:
        return True, ''

    items = list(
        booking.items.select_related('product').select_for_update().order_by('id')
    )
    if not items:
        return True, ''

    if (
        old_status == Booking.BookingStatus.CANCELLED
        and new_status != Booking.BookingStatus.CANCELLED
    ):
        insufficient_items = []
        inactive_items = []
        for item in items:
            if not item.product.is_active:
                inactive_items.append(item.product.name)
                continue
            if item.quantity > item.product.stock_quantity:
                insufficient_items.append(
                    f'{item.product.name} (needed {item.quantity}, available {item.product.stock_quantity})'
                )
        if inactive_items:
            return (
                False,
                (
                    'Cannot reopen this booking because some products are unavailable: '
                    f'{", ".join(inactive_items[:5])}.'
                ),
            )
        if insufficient_items:
            return (
                False,
                (
                    'Cannot reopen this booking due to low stock: '
                    f'{", ".join(insufficient_items[:5])}.'
                ),
            )
        for item in items:
            product = item.product
            product.stock_quantity -= item.quantity
            product.save(update_fields=['stock_quantity'])
        return True, ''

    if (
        old_status != Booking.BookingStatus.CANCELLED
        and new_status == Booking.BookingStatus.CANCELLED
    ):
        for item in items:
            product = item.product
            product.stock_quantity += item.quantity
            product.save(update_fields=['stock_quantity'])
        return True, ''

    return True, ''


def _public_delivery_allowed_statuses(current_status):
    mapping = {
        Booking.BookingStatus.SHIPPED: {Booking.BookingStatus.OUT_FOR_DELIVERY},
        Booking.BookingStatus.OUT_FOR_DELIVERY: {Booking.BookingStatus.DELIVERED},
        Booking.BookingStatus.DELIVERED: {Booking.BookingStatus.DELIVERED},
    }
    return mapping.get(current_status, set())


def _safe_public_delivery_next_url(request):
    candidate = request.POST.get('next') or request.GET.get('next')
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse('orders:public_delivery_status_update')


def _refund_paid_transaction_if_cancelled(booking):
    refundable_methods = {
        Transaction.PaymentMethod.CARD,
        Transaction.PaymentMethod.UPI,
        Transaction.PaymentMethod.WALLET,
        Transaction.PaymentMethod.NET_BANKING,
    }
    refundable_transaction = (
        booking.transactions.select_for_update()
        .filter(
            status=Transaction.TransactionStatus.SUCCESS,
            payment_method__in=refundable_methods,
        )
        .order_by('-paid_at', '-created_at')
        .first()
    )
    if not refundable_transaction:
        return None
    refundable_transaction.status = Transaction.TransactionStatus.REFUNDED
    refundable_transaction.save(update_fields=['status'])
    return refundable_transaction


def _is_customer_cancellation_allowed(status):
    return status in {
        Booking.BookingStatus.PENDING,
        Booking.BookingStatus.CONFIRMED,
    }


def _booking_detail_context(
    request,
    booking,
    cancel_form=None,
    cancellation_impact_form=None,
):
    can_customer_cancel = (
        request.user.role == User.UserRole.CUSTOMER
        and booking.customer_id == request.user.id
        and _is_customer_cancellation_allowed(booking.status)
    )
    can_admin_review_cancellation = (
        request.user.role == User.UserRole.ADMIN
        and booking.status == Booking.BookingStatus.CANCELLED
        and booking.cancelled_by_role == User.UserRole.CUSTOMER
        and bool((booking.cancellation_reason or '').strip())
    )

    if cancel_form is None and can_customer_cancel:
        cancel_form = BookingCancelForm()
    if cancellation_impact_form is None and can_admin_review_cancellation:
        impact_initial = booking.cancellation_impact
        if impact_initial not in {
            Booking.CancellationImpact.NO_IMPACT,
            Booking.CancellationImpact.NEGATIVE_IMPACT,
        }:
            impact_initial = Booking.CancellationImpact.NO_IMPACT
        cancellation_impact_form = BookingCancellationImpactForm(
            initial={
                'cancellation_impact': impact_initial,
                'cancellation_impact_note': booking.cancellation_impact_note,
            }
        )

    reviewed_product_ids = set()
    if (
        request.user.role == User.UserRole.CUSTOMER
        and booking.customer_id == request.user.id
        and booking.status == Booking.BookingStatus.DELIVERED
    ):
        reviewed_product_ids = set(
            Feedback.objects.filter(
                customer=request.user,
                booking=booking,
                product__isnull=False,
            ).values_list('product_id', flat=True)
        )

    return {
        'booking': booking,
        'can_customer_cancel': can_customer_cancel,
        'can_admin_review_cancellation': can_admin_review_cancellation,
        'cancellation_reason_available': bool((booking.cancellation_reason or '').strip()),
        'cancel_form': cancel_form,
        'cancellation_impact_form': cancellation_impact_form,
        'reviewed_product_ids': reviewed_product_ids,
    }


@role_required(User.UserRole.CUSTOMER)
def create_booking(request, product_id):
    _sync_saved_addresses_from_previous_bookings(request.user)
    product = (
        Product.objects.select_related(
            'category',
            'seller',
            'seller__seller_profile',
            'location',
            'location__district',
            'location__district__state',
        ).prefetch_related(
            'serviceable_states',
            'serviceable_districts',
            'serviceable_locations',
        )
        .filter(
            id=product_id,
            is_active=True,
            seller__is_active=True,
        )
        .first()
    )
    if not product:
        messages.error(request, 'This product is no longer available for booking.')
        return redirect('accounts:dashboard')
    if not product.category_id or not product.category.is_active:
        messages.error(request, 'This product category is non-listed now. Booking is unavailable.')
        return redirect('accounts:dashboard')
    if _is_seller_suspended(product.seller):
        messages.error(request, 'This seller is currently frozen. Booking is temporarily unavailable.')
        return redirect('catalog:product_detail', product_id=product.id)

    delivery_prediction = predict_delivery_for_product(product)

    saved_address_rows = []
    saved_addresses = (
        request.user.saved_addresses.select_related('location', 'location__district', 'location__district__state')
        .filter(is_active=True, location__isnull=False)
        .order_by('-is_default', 'label', '-updated_at')
    )
    for saved_address in saved_addresses:
        saved_address_rows.append(
            {
                'address': saved_address,
                'is_serviceable': product.is_serviceable_for_location(saved_address.location),
            }
        )

    pincode_check = None
    check_pincode = (request.GET.get('check_pincode') or '').strip()
    if check_pincode:
        check_location = (
            Location.objects.select_related('district', 'district__state')
            .filter(
                postal_code__iexact=check_pincode,
                is_active=True,
                district__is_active=True,
                district__state__is_active=True,
            )
            .order_by('district__state__name', 'district__name', 'name')
            .first()
        )
        if not check_location:
            pincode_check = {
                'postal_code': check_pincode,
                'is_serviceable': False,
                'message': 'This pincode is currently unavailable for delivery.',
            }
        else:
            is_serviceable = product.is_serviceable_for_location(check_location)
            pincode_check = {
                'postal_code': check_location.postal_code,
                'location': check_location,
                'is_serviceable': is_serviceable,
                'message': (
                    'Product is serviceable for this pincode.'
                    if is_serviceable
                    else 'Product is not serviceable for this pincode.'
                ),
            }

    if request.method == 'POST':
        form = BookingCreateForm(request.POST, user=request.user)
        if form.is_valid():
            quantity = form.cleaned_data['quantity']
            delivery_location = form.cleaned_data['resolved_delivery_location']
            shipping_address = form.cleaned_data['resolved_shipping_address']
            is_previous_address = form.cleaned_data.get('resolved_is_previous_address', False)
            raw_shipping_address = shipping_address
            with transaction.atomic():
                locked_product = (
                    Product.objects.select_for_update()
                    .select_related(
                        'category',
                        'seller',
                        'seller__seller_profile',
                        'location',
                        'location__district',
                        'location__district__state',
                    )
                    .filter(
                        id=product.id,
                        is_active=True,
                        seller__is_active=True,
                    )
                    .first()
                )
                if not locked_product:
                    form.add_error(None, 'This product is no longer available for booking.')
                elif not locked_product.category_id or not locked_product.category.is_active:
                    form.add_error(None, 'This product category is non-listed now. Booking is unavailable.')
                elif _is_seller_suspended(locked_product.seller):
                    form.add_error(None, 'This seller is currently frozen. Booking is temporarily unavailable.')
                elif not locked_product.is_serviceable_for_location(delivery_location):
                    form.add_error(
                        None,
                        (
                            'This product is not serviceable for the selected pincode because the state, district, '
                            'or pincode is currently unavailable.'
                        ),
                    )
                elif quantity > locked_product.stock_quantity:
                    form.add_error('quantity', 'Quantity exceeds available stock.')
                else:
                    address_with_service_area = _format_address_with_service_area(
                        raw_shipping_address,
                        delivery_location,
                    )
                    booking = Booking.objects.create(
                        customer=request.user,
                        seller=locked_product.seller,
                        delivery_location=delivery_location,
                        shipping_address=address_with_service_area,
                        total_amount=Decimal(locked_product.price) * quantity,
                    )
                    BookingItem.objects.create(
                        booking=booking,
                        product=locked_product,
                        quantity=quantity,
                        unit_price=locked_product.price,
                    )
                    locked_product.stock_quantity -= quantity
                    locked_product.save(update_fields=['stock_quantity'])
                    realtime_payload = _realtime_payload_from_request(
                        request,
                        customer_id=request.user.id,
                        booking_channel='single_product',
                    )
                    transaction.on_commit(
                        lambda booking_id=booking.id, payload=realtime_payload: report_booking_created_event(
                            booking=Booking.objects.select_related('seller', 'customer').get(id=booking_id),
                            payload=payload,
                        )
                    )
                    if not is_previous_address:
                        _save_customer_address_if_missing(request.user, raw_shipping_address, delivery_location)
                    return redirect('orders:create_transaction', booking_id=booking.id)
    else:
        form = BookingCreateForm(user=request.user, initial={'quantity': 1})

    return render(
        request,
        'orders/booking_form.html',
        {
            'form': form,
            'product': product,
            'predicted_delivery_days': delivery_prediction.days,
            'predicted_delivery_date': delivery_prediction.expected_date,
            'predicted_delivery_is_fallback': delivery_prediction.is_fallback,
            'saved_address_rows': saved_address_rows,
            'pincode_check': pincode_check,
            'check_pincode': check_pincode,
        },
    )


@role_required(User.UserRole.CUSTOMER)
def cart_checkout(request):
    _sync_saved_addresses_from_previous_bookings(request.user)
    snapshot = cart_snapshot(request)
    if snapshot['cart_item_count'] <= 0:
        messages.info(request, 'Your cart is empty. Add products before checkout.')
        return redirect('home')
    checkout_blocked = snapshot.get('cart_checkout_blocked', False)

    if request.method == 'POST':
        form = CartCheckoutForm(request.POST, user=request.user)
        if checkout_blocked:
            form.add_error(
                None,
                'Remove unavailable products from your cart before checkout.',
            )
        elif form.is_valid():
            delivery_location = form.cleaned_data['resolved_delivery_location']
            shipping_address = form.cleaned_data['resolved_shipping_address']
            is_previous_address = form.cleaned_data.get('resolved_is_previous_address', False)
            raw_shipping_address = shipping_address
            if not is_previous_address:
                shipping_address = _format_address_with_service_area(shipping_address, delivery_location)

            requested_quantities = _requested_cart_quantities(request)
            if not requested_quantities:
                messages.info(request, 'Your cart was empty at checkout time. Add products and try again.')
                return redirect('home')

            validation_errors = []
            seller_item_map = {}
            created_booking_ids = []

            with transaction.atomic():
                products = (
                    Product.objects.select_for_update()
                    .select_related(
                        'category',
                        'seller',
                        'seller__seller_profile',
                        'location',
                        'location__district',
                        'location__district__state',
                    )
                    .prefetch_related(
                        'serviceable_states',
                        'serviceable_districts__state',
                        'serviceable_locations__district__state',
                    )
                    .filter(id__in=requested_quantities.keys(), is_active=True)
                )
                product_map = {product.id: product for product in products}

                for product_id, quantity in requested_quantities.items():
                    product = product_map.get(product_id)
                    if not product:
                        validation_errors.append('One or more products in your cart are no longer available.')
                        continue
                    if not product.category_id or not product.category.is_active:
                        validation_errors.append(
                            f'{product.name} cannot be booked because its category is non-listed now.'
                        )
                        continue
                    if _is_seller_suspended(product.seller):
                        validation_errors.append(
                            f'{product.name} cannot be booked because seller operations are frozen.'
                        )
                        continue
                    if quantity > product.stock_quantity:
                        validation_errors.append(
                            f'{product.name} has only {product.stock_quantity} item(s) left in stock.'
                        )
                        continue
                    if not product.is_serviceable_for_location(delivery_location):
                        validation_errors.append(
                            f'{product.name} is not serviceable for pincode {delivery_location.postal_code}.'
                        )
                        continue

                    if product.seller_id not in seller_item_map:
                        seller_item_map[product.seller_id] = {
                            'seller': product.seller,
                            'items': [],
                        }
                    seller_item_map[product.seller_id]['items'].append((product, quantity))

                if not validation_errors:
                    for grouped in seller_item_map.values():
                        seller = grouped['seller']
                        items = grouped['items']
                        booking_total = sum(
                            (item.price * quantity for item, quantity in items),
                            Decimal('0.00'),
                        )
                        booking = Booking.objects.create(
                            customer=request.user,
                            seller=seller,
                            delivery_location=delivery_location,
                            shipping_address=shipping_address,
                            total_amount=booking_total,
                        )
                        created_booking_ids.append(booking.id)
                        realtime_payload = _realtime_payload_from_request(
                            request,
                            customer_id=request.user.id,
                            booking_channel='cart_checkout',
                        )
                        transaction.on_commit(
                            lambda booking_id=booking.id, payload=realtime_payload: report_booking_created_event(
                                booking=Booking.objects.select_related('seller', 'customer').get(id=booking_id),
                                payload=payload,
                            )
                        )
                        BookingItem.objects.bulk_create(
                            [
                                BookingItem(
                                    booking=booking,
                                    product=item,
                                    quantity=quantity,
                                    unit_price=item.price,
                                )
                                for item, quantity in items
                            ]
                        )
                        for item, quantity in items:
                            item.stock_quantity -= quantity
                            item.save(update_fields=['stock_quantity'])

            if validation_errors:
                unique_errors = []
                for error_message in validation_errors:
                    if error_message not in unique_errors:
                        unique_errors.append(error_message)
                for error_message in unique_errors[:8]:
                    form.add_error(None, error_message)
            else:
                if not is_previous_address:
                    _save_customer_address_if_missing(request.user, raw_shipping_address, delivery_location)
                _set_cart_checkout_pending_booking_ids(request, created_booking_ids)
                save_cart(request, {})
                messages.success(
                    request,
                    (
                        f'Created {len(created_booking_ids)} booking(s). '
                        'Continue to payment to confirm all bookings.'
                    ),
                )
                return redirect('orders:cart_checkout_payment')
    else:
        form = CartCheckoutForm(user=request.user)

    return render(
        request,
        'orders/cart_checkout.html',
        {
            'form': form,
            'cart_items': snapshot['cart_items'],
            'cart_unavailable_items': snapshot.get('cart_unavailable_items', []),
            'cart_item_count': snapshot['cart_item_count'],
            'cart_available_item_count': snapshot.get('cart_available_item_count', 0),
            'cart_unavailable_count': snapshot.get('cart_unavailable_count', 0),
            'cart_total_amount': snapshot['cart_total_amount'],
            'cart_checkout_blocked': checkout_blocked,
        },
    )


@role_required(User.UserRole.CUSTOMER)
def cart_checkout_payment(request):
    pending_booking_ids = _get_cart_checkout_pending_booking_ids(request)
    if not pending_booking_ids:
        messages.info(
            request,
            'No grouped checkout bookings are pending payment. Add items to cart and checkout first.',
        )
        return redirect('orders:booking_list')

    bookings = list(
        Booking.objects.select_related('seller')
        .prefetch_related('transactions', 'items__product')
        .filter(id__in=pending_booking_ids, customer=request.user)
        .order_by('-booked_at')
    )
    if not bookings:
        _set_cart_checkout_pending_booking_ids(request, [])
        messages.info(
            request,
            'Grouped checkout booking references were not found. Start checkout again.',
        )
        return redirect('orders:booking_list')

    booking_map = {booking.id: booking for booking in bookings}
    ordered_bookings = [booking_map[booking_id] for booking_id in pending_booking_ids if booking_id in booking_map]

    payable_bookings = []
    blocked_reasons = []
    for booking in ordered_bookings:
        if booking.status != Booking.BookingStatus.PENDING:
            blocked_reasons.append(
                f'Booking #{booking.id} is already {booking.get_status_display()} and cannot be paid again.'
            )
            continue
        has_successful_payment = any(
            txn.status == Transaction.TransactionStatus.SUCCESS
            for txn in booking.transactions.all()
        )
        if has_successful_payment:
            blocked_reasons.append(f'Booking #{booking.id} already has a successful payment.')
            continue
        if _is_seller_suspended(booking.seller):
            blocked_reasons.append(
                (
                    f'Booking #{booking.id} cannot be paid because seller '
                    f'{booking.seller.display_name} is currently frozen.'
                )
            )
            continue
        payable_bookings.append(booking)

    total_payable_amount = sum((booking.total_amount for booking in payable_bookings), Decimal('0.00'))
    if not payable_bookings:
        _set_cart_checkout_pending_booking_ids(request, [])

    if request.method == 'POST':
        form = TransactionForm(request.POST)
        if blocked_reasons:
            for message_text in blocked_reasons[:8]:
                form.add_error(None, message_text)
        elif not payable_bookings:
            form.add_error(None, 'No payable bookings are available in this grouped checkout.')
        elif form.is_valid():
            method = form.cleaned_data['payment_method']
            is_cod = method == Transaction.PaymentMethod.COD
            booking_ids_to_pay = [booking.id for booking in payable_bookings]
            payment_errors = []
            with transaction.atomic():
                locked_bookings = {
                    booking.id: booking
                    for booking in (
                        Booking.objects.select_for_update()
                        .select_related('seller')
                        .prefetch_related('transactions')
                        .filter(id__in=booking_ids_to_pay, customer=request.user)
                    )
                }
                payable_locked_bookings = []
                for booking_id in booking_ids_to_pay:
                    booking = locked_bookings.get(booking_id)
                    if booking is None:
                        payment_errors.append(f'Booking #{booking_id} was not found during payment.')
                        continue
                    if booking.status != Booking.BookingStatus.PENDING:
                        payment_errors.append(
                            f'Booking #{booking.id} is now {booking.get_status_display()} and cannot be paid.'
                        )
                        continue
                    has_successful_payment = booking.transactions.filter(
                        status=Transaction.TransactionStatus.SUCCESS
                    ).exists()
                    if has_successful_payment:
                        payment_errors.append(f'Booking #{booking.id} already has successful payment.')
                        continue
                    if _is_seller_suspended(booking.seller):
                        payment_errors.append(
                            (
                                f'Booking #{booking.id} cannot be paid because seller '
                                f'{booking.seller.display_name} is frozen.'
                            )
                        )
                        continue
                    payable_locked_bookings.append(booking)

                if payment_errors:
                    for message_text in payment_errors[:8]:
                        form.add_error(None, message_text)
                else:
                    should_fail = (not is_cod) and _should_simulate_payment_failure(
                        request=request,
                        form=form,
                        method=method,
                    )
                    if should_fail:
                        for booking in payable_locked_bookings:
                            failed_tx = Transaction.objects.create(
                                booking=booking,
                                amount=booking.total_amount,
                                payment_method=method,
                                status=Transaction.TransactionStatus.FAILED,
                                transaction_reference=_generate_transaction_reference(),
                                paid_at=None,
                            )
                            payment_handle = ''
                            if method == Transaction.PaymentMethod.UPI:
                                payment_handle = str(form.cleaned_data.get('upi_id') or '').strip()
                            elif method == Transaction.PaymentMethod.CARD:
                                card_number = str(form.cleaned_data.get('card_number') or '').replace(' ', '')
                                payment_handle = (
                                    f'card_ending_{card_number[-4:]}' if len(card_number) >= 4 else ''
                                )
                            report_failed_payment_event(
                                transaction_obj=failed_tx,
                                payload=_realtime_payload_from_request(
                                    request,
                                    payment_handle=payment_handle,
                                    failure_reason='gateway_declined',
                                ),
                            )
                        form.add_error(
                            None,
                            'Grouped payment failed for all selected bookings. Retry with another method.',
                        )
                        return render(
                            request,
                            'orders/cart_transaction_form.html',
                            {
                                'form': form,
                                'bookings': ordered_bookings,
                                'payable_bookings': payable_bookings,
                                'blocked_reasons': blocked_reasons,
                                'total_payable_amount': total_payable_amount,
                            },
                            status=400,
                        )
                    for booking in payable_locked_bookings:
                        transaction_obj = Transaction.objects.create(
                            booking=booking,
                            amount=booking.total_amount,
                            payment_method=method,
                            status=Transaction.TransactionStatus.SUCCESS,
                            transaction_reference=_generate_transaction_reference(),
                            paid_at=None if is_cod else timezone.now(),
                        )
                        booking.status = Booking.BookingStatus.CONFIRMED
                        booking.save(update_fields=['status'])

                    _set_cart_checkout_pending_booking_ids(request, [])
                    messages.success(
                        request,
                        (
                            f'Payment recorded for {len(payable_locked_bookings)} booking(s). '
                            'All bookings are now confirmed.'
                        ),
                    )
                    if not is_cod:
                        messages.success(request, 'Payment successful.')
                    return redirect('orders:booking_list')
    else:
        form = TransactionForm()

    return render(
        request,
        'orders/cart_transaction_form.html',
        {
            'form': form,
            'bookings': ordered_bookings,
            'payable_bookings': payable_bookings,
            'blocked_reasons': blocked_reasons,
            'total_payable_amount': total_payable_amount,
        },
    )


def _booking_queryset_for_user(user):
    related_fields = (
        'customer',
        'seller',
        'delivery_location',
        'delivery_location__district',
        'delivery_location__district__state',
        'anomaly_incident',
        'anomaly_incident__snapshot',
    )
    if user.role == User.UserRole.ADMIN:
        return Booking.objects.select_related(*related_fields)
    if user.role == User.UserRole.SELLER:
        return Booking.objects.select_related(*related_fields).filter(seller=user)
    return Booking.objects.select_related(*related_fields).filter(customer=user)


@role_required(User.UserRole.ADMIN, User.UserRole.SELLER, User.UserRole.CUSTOMER)
def booking_list(request):
    bookings = list(_booking_queryset_for_user(request.user))
    resume_group_payment_count = 0
    seller_shipping_delay_count = 0
    shipping_delay_cutoff = timezone.now() - timedelta(days=2)

    if request.user.role == User.UserRole.SELLER:
        for booking in bookings:
            booking.is_shipping_delay_warning = bool(
                booking.status == Booking.BookingStatus.CONFIRMED
                and booking.booked_at
                and booking.booked_at < shipping_delay_cutoff
            )
            if booking.is_shipping_delay_warning:
                seller_shipping_delay_count += 1
    else:
        for booking in bookings:
            booking.is_shipping_delay_warning = False

    if request.user.role == User.UserRole.CUSTOMER:
        pending_booking_ids = _get_cart_checkout_pending_booking_ids(request)
        if pending_booking_ids:
            pending_checkout_bookings = (
                Booking.objects.filter(
                    id__in=pending_booking_ids,
                    customer=request.user,
                    status=Booking.BookingStatus.PENDING,
                )
                .prefetch_related('transactions')
            )
            for booking in pending_checkout_bookings:
                has_successful_payment = any(
                    txn.status == Transaction.TransactionStatus.SUCCESS
                    for txn in booking.transactions.all()
                )
                if not has_successful_payment:
                    resume_group_payment_count += 1

    return render(
        request,
        'orders/booking_list.html',
        {
            'bookings': bookings,
            'resume_group_payment_count': resume_group_payment_count,
            'seller_shipping_delay_count': seller_shipping_delay_count,
        },
    )


@role_required(User.UserRole.ADMIN, User.UserRole.SELLER, User.UserRole.CUSTOMER)
def booking_detail(request, booking_id):
    booking = get_object_or_404(
        _booking_queryset_for_user(request.user).prefetch_related('items__product', 'transactions'),
        id=booking_id,
    )
    return render(request, 'orders/booking_detail.html', _booking_detail_context(request, booking))


@role_required(User.UserRole.ADMIN)
def cancellation_monitor(request):
    show_filter = (request.GET.get('filter') or 'pending').strip().lower()
    valid_filters = {'pending', 'reviewed', 'all'}
    if show_filter not in valid_filters:
        show_filter = 'pending'

    queryset = (
        Booking.objects.select_related('customer', 'seller')
        .filter(
            status=Booking.BookingStatus.CANCELLED,
            cancelled_by_role=User.UserRole.CUSTOMER,
        )
        .order_by('-cancelled_at', '-booked_at')
    )
    if show_filter == 'pending':
        queryset = queryset.filter(cancellation_impact=Booking.CancellationImpact.NOT_REVIEWED)
    elif show_filter == 'reviewed':
        queryset = queryset.exclude(cancellation_impact=Booking.CancellationImpact.NOT_REVIEWED)

    bookings = list(queryset[:180])
    all_customer_cancelled = Booking.objects.filter(
        status=Booking.BookingStatus.CANCELLED,
        cancelled_by_role=User.UserRole.CUSTOMER,
    )
    context = {
        'bookings': bookings,
        'selected_filter': show_filter,
        'pending_count': all_customer_cancelled.filter(
            cancellation_impact=Booking.CancellationImpact.NOT_REVIEWED
        ).count(),
        'reviewed_count': all_customer_cancelled.exclude(
            cancellation_impact=Booking.CancellationImpact.NOT_REVIEWED
        ).count(),
        'all_count': all_customer_cancelled.count(),
    }
    return render(request, 'orders/cancellation_monitor.html', context)


@role_required(User.UserRole.CUSTOMER)
def cancel_booking(request, booking_id):
    if request.method != 'POST':
        return redirect('orders:booking_detail', booking_id=booking_id)

    form = BookingCancelForm(request.POST)
    with transaction.atomic():
        booking = get_object_or_404(
            Booking.objects.select_for_update()
            .select_related(
                'customer',
                'seller',
                'delivery_location',
                'delivery_location__district',
                'delivery_location__district__state',
                'anomaly_incident',
                'cancellation_reviewed_by',
            )
            .prefetch_related('items__product', 'transactions'),
            id=booking_id,
            customer=request.user,
        )
        if booking.status == Booking.BookingStatus.CANCELLED:
            messages.info(request, f'Booking #{booking.id} is already cancelled.')
            return redirect('orders:booking_detail', booking_id=booking.id)
        if not _is_customer_cancellation_allowed(booking.status):
            messages.error(
                request,
                'You can cancel this order only before it reaches the shipped status.',
            )
            return redirect('orders:booking_detail', booking_id=booking.id)
        if not form.is_valid():
            context = _booking_detail_context(request, booking, cancel_form=form)
            return render(request, 'orders/booking_detail.html', context, status=400)

        is_ok, error_message = _apply_stock_changes_for_status_transition(
            booking=booking,
            old_status=booking.status,
            new_status=Booking.BookingStatus.CANCELLED,
        )
        if not is_ok:
            form.add_error(None, error_message)
            context = _booking_detail_context(request, booking, cancel_form=form)
            return render(request, 'orders/booking_detail.html', context, status=400)

        booking.status = Booking.BookingStatus.CANCELLED
        booking.cancellation_reason = form.cleaned_data['cancellation_reason']
        booking.cancelled_at = timezone.now()
        booking.cancelled_by_role = request.user.role
        booking.cancellation_impact = Booking.CancellationImpact.NOT_REVIEWED
        booking.cancellation_impact_note = ''
        booking.cancellation_reviewed_at = None
        booking.cancellation_reviewed_by = None
        booking.anomaly_reported_at = None
        booking.anomaly_incident = None
        booking.save(
            update_fields=[
                'status',
                'cancellation_reason',
                'cancelled_at',
                'cancelled_by_role',
                'cancellation_impact',
                'cancellation_impact_note',
                'cancellation_reviewed_at',
                'cancellation_reviewed_by',
                'anomaly_reported_at',
                'anomaly_incident',
            ]
        )
        refunded_transaction = _refund_paid_transaction_if_cancelled(booking)
        _snapshot, customer_cancellation_incident = report_cancellation_anomaly_for_booking(
            booking=booking,
            force_high_risk=False,
        )
        if customer_cancellation_incident and booking.anomaly_incident_id is None:
            booking.anomaly_incident = customer_cancellation_incident
            booking.anomaly_reported_at = timezone.now()
            booking.save(update_fields=['anomaly_incident', 'anomaly_reported_at'])
        if refunded_transaction:
            messages.success(
                request,
                (
                    f'Booking #{booking.id} cancelled successfully. '
                    f'?{refunded_transaction.amount} refunded to the customer.'
                ),
            )
        else:
            messages.success(request, f'Booking #{booking.id} cancelled successfully.')
    return redirect('orders:booking_detail', booking_id=booking.id)


@role_required(User.UserRole.ADMIN)
def review_booking_cancellation(request, booking_id):
    if request.method != 'POST':
        return redirect('orders:booking_detail', booking_id=booking_id)

    form = BookingCancellationImpactForm(request.POST)
    with transaction.atomic():
        booking = get_object_or_404(
            _booking_queryset_for_user(request.user)
            .select_for_update()
            .select_related(
                'seller',
                'seller__seller_profile',
                'anomaly_incident',
                'cancellation_reviewed_by',
            )
            .prefetch_related('items__product', 'transactions'),
            id=booking_id,
        )
        if booking.status != Booking.BookingStatus.CANCELLED:
            messages.error(request, 'Only cancelled bookings can be reviewed for cancellation impact.')
            return redirect('orders:booking_detail', booking_id=booking.id)
        if booking.cancelled_by_role != User.UserRole.CUSTOMER:
            messages.info(
                request,
                (
                    'This cancellation was not customer-initiated. '
                    'Seller cancellations are auto-marked high risk.'
                ),
            )
            return redirect('orders:booking_detail', booking_id=booking.id)
        if not (booking.cancellation_reason or '').strip():
            messages.error(request, 'Cancellation reason is required before admin impact review.')
            return redirect('orders:booking_detail', booking_id=booking.id)
        if not form.is_valid():
            context = _booking_detail_context(request, booking, cancellation_impact_form=form)
            return render(request, 'orders/booking_detail.html', context, status=400)

        cancellation_impact = form.cleaned_data['cancellation_impact']
        cancellation_impact_note = form.cleaned_data['cancellation_impact_note']
        raise_anomaly = (
            cancellation_impact == Booking.CancellationImpact.NEGATIVE_IMPACT
            and booking.anomaly_incident_id is None
        )
        update_fields = [
            'cancellation_impact',
            'cancellation_impact_note',
            'cancellation_reviewed_at',
            'cancellation_reviewed_by',
        ]

        booking.cancellation_impact = cancellation_impact
        booking.cancellation_impact_note = cancellation_impact_note
        booking.cancellation_reviewed_at = timezone.now()
        booking.cancellation_reviewed_by = request.user

        if raise_anomaly:
            _snapshot, incident = report_cancellation_anomaly_for_booking(
                booking=booking,
                admin_note=cancellation_impact_note,
            )
            booking.anomaly_incident = incident
            booking.anomaly_reported_at = timezone.now()
            update_fields.extend(['anomaly_incident', 'anomaly_reported_at'])

        booking.save(update_fields=update_fields)
        if raise_anomaly:
            messages.success(
                request,
                (
                    f'Cancellation marked as negative impact. Seller anomaly incident '
                    f'#{booking.anomaly_incident_id} has been raised.'
                ),
            )
        else:
            messages.success(request, 'Cancellation impact updated.')
    return redirect('orders:booking_detail', booking_id=booking.id)


@role_required(User.UserRole.CUSTOMER)
def transaction_success(request, booking_id, transaction_id):
    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'seller'),
        id=booking_id,
        customer=request.user,
    )
    transaction_obj = get_object_or_404(
        Transaction.objects.select_related('booking'),
        id=transaction_id,
        booking=booking,
        status=Transaction.TransactionStatus.SUCCESS,
    )
    return render(
        request,
        'orders/payment_success.html',
        {
            'booking': booking,
            'transaction': transaction_obj,
            'is_cod': transaction_obj.payment_method == Transaction.PaymentMethod.COD,
        },
    )


def public_delivery_status_update(request):
    if request.method == 'POST':
        booking_id_raw = (request.POST.get('booking_id') or '').strip()
        target_status = (request.POST.get('target_status') or request.POST.get('status') or '').strip()
        if not booking_id_raw.isdigit():
            messages.error(request, 'Enter a valid booking ID.')
            return redirect(_safe_public_delivery_next_url(request))

        if target_status not in {
            Booking.BookingStatus.OUT_FOR_DELIVERY,
            Booking.BookingStatus.DELIVERED,
        }:
            messages.error(request, 'Select a valid delivery status action.')
            return redirect(_safe_public_delivery_next_url(request))

        with transaction.atomic():
            booking = (
                Booking.objects.select_for_update()
                .select_related(
                    'customer',
                    'seller',
                    'delivery_location',
                    'delivery_location__district',
                    'delivery_location__district__state',
                )
                .filter(id=int(booking_id_raw))
                .first()
            )
            if not booking:
                messages.error(request, 'Booking not found.')
                return redirect(_safe_public_delivery_next_url(request))
            if _is_seller_suspended(booking.seller):
                messages.error(
                    request,
                    (
                        f'Cannot update booking #{booking.id} because seller operations are frozen '
                        'pending risk review.'
                    ),
                )
                return redirect(_safe_public_delivery_next_url(request))

            allowed_statuses = _public_delivery_allowed_statuses(booking.status)
            if target_status not in allowed_statuses:
                messages.error(
                    request,
                    (
                        f'Cannot update booking #{booking.id} from '
                        f'"{booking.get_status_display()}" to this status.'
                    ),
                )
            elif target_status == booking.status:
                messages.info(request, f'Booking #{booking.id} is already {booking.get_status_display()}.')
            else:
                booking.status = target_status
                booking.save(update_fields=['status'])
                messages.success(request, f'Booking #{booking.id} updated to {booking.get_status_display()}.')
        return redirect(_safe_public_delivery_next_url(request))

    query = ' '.join((request.GET.get('q') or '').split())
    selected_status = (request.GET.get('status') or 'all').strip()
    valid_status_filters = {
        Booking.BookingStatus.PENDING,
        Booking.BookingStatus.CONFIRMED,
        Booking.BookingStatus.SHIPPED,
        Booking.BookingStatus.OUT_FOR_DELIVERY,
        Booking.BookingStatus.DELIVERED,
        Booking.BookingStatus.CANCELLED,
        'all',
    }
    if selected_status not in valid_status_filters:
        selected_status = 'all'

    query_filter = Q()
    if query:
        query_filter |= (
            Q(customer__first_name__icontains=query)
            | Q(customer__last_name__icontains=query)
            | Q(customer__email__icontains=query)
            | Q(seller__first_name__icontains=query)
            | Q(seller__last_name__icontains=query)
            | Q(seller__email__icontains=query)
            | Q(tracking_number__icontains=query)
            | Q(delivery_location__postal_code__icontains=query)
        )
        if query.isdigit():
            query_filter |= Q(id=int(query))

    summary_queryset = Booking.objects.all()
    if query:
        summary_queryset = summary_queryset.filter(query_filter)

    bookings_queryset = (
        Booking.objects.select_related(
            'customer',
            'seller',
            'delivery_location',
            'delivery_location__district',
            'delivery_location__district__state',
        )
        .annotate(item_count=Count('items', distinct=True))
        .order_by('-booked_at')
    )
    if query:
        bookings_queryset = bookings_queryset.filter(query_filter)
    if selected_status != 'all':
        bookings_queryset = bookings_queryset.filter(status=selected_status)

    bookings = bookings_queryset[:120]
    status_filters = [
        {'value': 'all', 'label': 'All'},
        {'value': Booking.BookingStatus.CONFIRMED, 'label': 'Confirmed'},
        {'value': Booking.BookingStatus.SHIPPED, 'label': 'Shipped'},
        {'value': Booking.BookingStatus.OUT_FOR_DELIVERY, 'label': 'Out for Delivery'},
        {'value': Booking.BookingStatus.DELIVERED, 'label': 'Delivered'},
        {'value': Booking.BookingStatus.PENDING, 'label': 'Pending'},
        {'value': Booking.BookingStatus.CANCELLED, 'label': 'Cancelled'},
    ]

    context = {
        'bookings': bookings,
        'query': query,
        'selected_status': selected_status,
        'status_filters': status_filters,
        'total_count': summary_queryset.count(),
        'ready_count': summary_queryset.filter(
            status=Booking.BookingStatus.SHIPPED
        ).count(),
        'out_for_delivery_count': summary_queryset.filter(
            status=Booking.BookingStatus.OUT_FOR_DELIVERY
        ).count(),
        'delivered_count': summary_queryset.filter(
            status=Booking.BookingStatus.DELIVERED
        ).count(),
    }
    return render(request, 'orders/public_delivery_status_update.html', context)


@role_required(User.UserRole.ADMIN, User.UserRole.SELLER, User.UserRole.CUSTOMER)
def booking_receipt(request, booking_id):
    booking = get_object_or_404(
        _booking_queryset_for_user(request.user).prefetch_related('items__product', 'transactions'),
        id=booking_id,
    )
    if booking.status not in {
        Booking.BookingStatus.SHIPPED,
        Booking.BookingStatus.OUT_FOR_DELIVERY,
        Booking.BookingStatus.DELIVERED,
    }:
        messages.info(request, 'Receipt will be available once the booking is shipped.')
        return redirect('orders:booking_detail', booking_id=booking.id)

    successful_transaction = (
        booking.transactions.filter(status=Transaction.TransactionStatus.SUCCESS)
        .order_by('-paid_at', '-created_at')
        .first()
    )
    try:
        seller_store_name = (booking.seller.seller_profile.store_name or '').strip()
    except ObjectDoesNotExist:
        seller_store_name = ''
    if not seller_store_name:
        seller_store_name = booking.seller.display_name

    invoice_number = f'NN-{booking.booked_at:%Y%m%d}-{booking.id:05d}'
    return render(
        request,
        'orders/booking_receipt.html',
        {
            'booking': booking,
            'line_items': booking.items.all(),
            'successful_transaction': successful_transaction,
            'seller_store_name': seller_store_name,
            'invoice_number': invoice_number,
        },
    )


@role_required(User.UserRole.SELLER, User.UserRole.ADMIN)
def update_booking_status(request, booking_id):
    if request.method == 'POST':
        with transaction.atomic():
            booking = get_object_or_404(
                _booking_queryset_for_user(request.user).select_for_update(),
                id=booking_id,
            )
            if _is_seller_suspended(booking.seller):
                messages.error(request, _suspension_block_message())
                return redirect('orders:booking_detail', booking_id=booking.id)
            old_status = booking.status
            action = (request.POST.get('action') or '').strip()
            form_payload = request.POST.copy()
            if action == 'mark_shipped':
                form_payload['status'] = Booking.BookingStatus.SHIPPED
            elif action == 'cancel_booking':
                form_payload['status'] = Booking.BookingStatus.CANCELLED

            form = BookingStatusForm(form_payload, instance=booking, user=request.user)
            if form.is_valid():
                new_status = form.cleaned_data['status']

                if not _is_status_transition_allowed(request.user, old_status, new_status):
                    form.add_error('status', 'This status transition is not allowed for your role.')
                else:
                    is_ok, error_message = _apply_stock_changes_for_status_transition(
                        booking=booking,
                        old_status=old_status,
                        new_status=new_status,
                    )
                    if not is_ok:
                        form.add_error(None, error_message)
                    else:
                        booking.status = new_status
                        booking.tracking_number = form.cleaned_data.get('tracking_number') or ''
                        booking.expected_delivery_date = form.cleaned_data.get('expected_delivery_date')
                        update_fields = [
                            'status',
                            'tracking_number',
                            'expected_delivery_date',
                        ]
                        if (
                            new_status == Booking.BookingStatus.CANCELLED
                            and old_status != Booking.BookingStatus.CANCELLED
                        ):
                            booking.cancelled_at = timezone.now()
                            booking.cancelled_by_role = request.user.role
                            if request.user.role == User.UserRole.SELLER:
                                seller_reason = (
                                    form.cleaned_data.get('seller_cancellation_reason_text')
                                    or f'Cancelled by {request.user.get_role_display()} during status update.'
                                )
                                seller_ack_note = (
                                    form.cleaned_data.get('seller_cancellation_ack_note') or ''
                                ).strip()
                                booking.cancellation_reason = seller_reason
                                booking.cancellation_impact = Booking.CancellationImpact.NEGATIVE_IMPACT
                                internal_notes = [
                                    'Auto-marked high risk because seller cancelled the order.',
                                ]
                                if seller_ack_note:
                                    internal_notes.append(f'Seller acknowledgement: {seller_ack_note}')
                                booking.cancellation_impact_note = ' '.join(internal_notes)
                                booking.cancellation_reviewed_at = timezone.now()
                                booking.cancellation_reviewed_by = None
                            else:
                                if not (booking.cancellation_reason or '').strip():
                                    booking.cancellation_reason = (
                                        f'Cancelled by {request.user.get_role_display()} during status update.'
                                    )
                                booking.cancellation_impact = Booking.CancellationImpact.NOT_REVIEWED
                                booking.cancellation_impact_note = ''
                                booking.cancellation_reviewed_at = None
                                booking.cancellation_reviewed_by = None
                            booking.anomaly_reported_at = None
                            booking.anomaly_incident = None
                            update_fields.extend(
                                [
                                    'cancelled_at',
                                    'cancelled_by_role',
                                    'cancellation_reason',
                                    'cancellation_impact',
                                    'cancellation_impact_note',
                                    'cancellation_reviewed_at',
                                    'cancellation_reviewed_by',
                                    'anomaly_reported_at',
                                    'anomaly_incident',
                                ]
                            )
                        refunded_transaction = None
                        auto_high_risk_incident = None
                        booking.save(update_fields=update_fields)
                        if (
                            new_status == Booking.BookingStatus.CANCELLED
                            and old_status != Booking.BookingStatus.CANCELLED
                        ):
                            refunded_transaction = _refund_paid_transaction_if_cancelled(booking)
                            force_high_risk = request.user.role == User.UserRole.SELLER
                            _snapshot, auto_high_risk_incident = report_cancellation_anomaly_for_booking(
                                booking=booking,
                                admin_note=booking.cancellation_impact_note,
                                force_high_risk=force_high_risk,
                            )
                            if auto_high_risk_incident:
                                booking.anomaly_incident = auto_high_risk_incident
                                booking.anomaly_reported_at = timezone.now()
                                booking.save(update_fields=['anomaly_incident', 'anomaly_reported_at'])
                        if auto_high_risk_incident and refunded_transaction:
                            if request.user.role == User.UserRole.SELLER:
                                messages.warning(
                                    request,
                                    'Seller cancellation acknowledged. This action contributes to your risk profile.',
                                )
                            messages.success(
                                request,
                                (
                                    f'Booking #{booking.id} updated to {booking.get_status_display()}. '
                                    f'?{refunded_transaction.amount} refunded. Seller marked high risk '
                                    f'(incident #{auto_high_risk_incident.id}).'
                                ),
                            )
                        elif auto_high_risk_incident:
                            if request.user.role == User.UserRole.SELLER:
                                messages.warning(
                                    request,
                                    'Seller cancellation acknowledged. This action contributes to your risk profile.',
                                )
                            messages.success(
                                request,
                                (
                                    f'Booking #{booking.id} updated to {booking.get_status_display()}. '
                                    f'Seller marked high risk (incident #{auto_high_risk_incident.id}).'
                                ),
                            )
                        elif refunded_transaction:
                            messages.success(
                                request,
                                (
                                    f'Booking #{booking.id} updated to {booking.get_status_display()}. '
                                    f'?{refunded_transaction.amount} refunded to the customer.'
                                ),
                            )
                        else:
                            messages.success(request, f'Booking #{booking.id} updated to {booking.get_status_display()}.')
                        return redirect('orders:booking_detail', booking_id=booking.id)
    else:
        booking = get_object_or_404(_booking_queryset_for_user(request.user), id=booking_id)
        form = BookingStatusForm(instance=booking, user=request.user)

    return render(
        request,
        'orders/booking_status_form.html',
        {
            'form': form,
            'booking': booking,
            'is_confirmed_booking': booking.status == Booking.BookingStatus.CONFIRMED,
            'can_cancel_from_current_status': _is_status_transition_allowed(
                request.user,
                booking.status,
                Booking.BookingStatus.CANCELLED,
            ),
        },
    )


@role_required(User.UserRole.SELLER, User.UserRole.ADMIN)
def confirm_booking_delivered(request, booking_id):
    if request.method != 'POST':
        return redirect('orders:booking_detail', booking_id=booking_id)

    with transaction.atomic():
        booking = get_object_or_404(
            _booking_queryset_for_user(request.user).select_for_update(),
            id=booking_id,
        )
        if _is_seller_suspended(booking.seller):
            messages.error(request, _suspension_block_message())
            return redirect('orders:booking_detail', booking_id=booking.id)
        old_status = booking.status
        new_status = Booking.BookingStatus.DELIVERED
        if not _is_status_transition_allowed(request.user, old_status, new_status):
            messages.error(
                request,
                f'Cannot mark booking #{booking.id} as delivered from {booking.get_status_display()}.',
            )
            return redirect('orders:booking_detail', booking_id=booking.id)

        is_ok, error_message = _apply_stock_changes_for_status_transition(
            booking=booking,
            old_status=old_status,
            new_status=new_status,
        )
        if not is_ok:
            messages.error(request, error_message)
            return redirect('orders:booking_detail', booking_id=booking.id)

        booking.status = new_status
        booking.save(update_fields=['status'])
        messages.success(request, f'Booking #{booking.id} marked as delivered.')
        return redirect('orders:booking_detail', booking_id=booking.id)


@role_required(User.UserRole.CUSTOMER)
def create_transaction(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id, customer=request.user)
    if _is_seller_suspended(booking.seller):
        if booking.status != Booking.BookingStatus.CANCELLED:
            booking.status = Booking.BookingStatus.CANCELLED
            booking.cancelled_at = timezone.now()
            booking.cancelled_by_role = User.UserRole.ADMIN
            booking.cancellation_reason = (
                'Booking auto-cancelled because seller operations are frozen pending risk review.'
            )
            booking.cancellation_impact = Booking.CancellationImpact.NOT_REVIEWED
            booking.cancellation_impact_note = ''
            booking.cancellation_reviewed_at = None
            booking.cancellation_reviewed_by = None
            booking.anomaly_reported_at = None
            booking.anomaly_incident = None
            booking.save(
                update_fields=[
                    'status',
                    'cancelled_at',
                    'cancelled_by_role',
                    'cancellation_reason',
                    'cancellation_impact',
                    'cancellation_impact_note',
                    'cancellation_reviewed_at',
                    'cancellation_reviewed_by',
                    'anomaly_reported_at',
                    'anomaly_incident',
                ]
            )
        messages.error(
            request,
            'Payment is blocked because this seller is currently frozen. The booking has been cancelled.',
        )
        return redirect('orders:booking_detail', booking_id=booking.id)
    successful_transaction = (
        booking.transactions.filter(status=Transaction.TransactionStatus.SUCCESS)
        .order_by('-paid_at', '-created_at')
        .first()
    )
    if successful_transaction:
        if successful_transaction.payment_method == Transaction.PaymentMethod.COD:
            messages.info(request, 'Order is already placed with cash on delivery for this booking.')
        else:
            messages.info(request, 'Payment is already completed for this booking.')
        return redirect(
            'orders:transaction_success',
            booking_id=booking.id,
            transaction_id=successful_transaction.id,
        )
    if booking.status != Booking.BookingStatus.PENDING:
        messages.info(request, 'Payment can be made only for bookings awaiting confirmation.')
        return redirect('orders:booking_detail', booking_id=booking.id)
    if request.method == 'POST':
        form = TransactionForm(request.POST)
        if form.is_valid():
            method = form.cleaned_data['payment_method']
            is_cod = method == Transaction.PaymentMethod.COD
            if not is_cod and _should_simulate_payment_failure(request=request, form=form, method=method):
                payment_handle = ''
                if method == Transaction.PaymentMethod.UPI:
                    payment_handle = str(form.cleaned_data.get('upi_id') or '').strip()
                elif method == Transaction.PaymentMethod.CARD:
                    card_number = str(form.cleaned_data.get('card_number') or '').replace(' ', '')
                    payment_handle = f'card_ending_{card_number[-4:]}' if len(card_number) >= 4 else ''
                failed_tx = Transaction.objects.create(
                    booking=booking,
                    amount=booking.total_amount,
                    payment_method=method,
                    status=Transaction.TransactionStatus.FAILED,
                    transaction_reference=uuid4().hex[:12].upper(),
                    paid_at=None,
                )
                report_failed_payment_event(
                    transaction_obj=failed_tx,
                    payload=_realtime_payload_from_request(
                        request,
                        payment_handle=payment_handle,
                        failure_reason='gateway_declined',
                    ),
                )
                messages.error(request, 'Payment failed. Please retry with another method or updated details.')
                return redirect('orders:create_transaction', booking_id=booking.id)
            transaction_obj = Transaction.objects.create(
                booking=booking,
                amount=booking.total_amount,
                payment_method=method,
                status=Transaction.TransactionStatus.SUCCESS,
                transaction_reference=uuid4().hex[:12].upper(),
                paid_at=None if is_cod else timezone.now(),
            )
            booking.status = Booking.BookingStatus.CONFIRMED
            booking.save(update_fields=['status'])
            messages.success(request, 'Order successful. Booking has been confirmed.')
            if not is_cod:
                messages.success(request, 'Payment successful.')
            return redirect(
                'orders:transaction_success',
                booking_id=booking.id,
                transaction_id=transaction_obj.id,
            )
    else:
        form = TransactionForm()

    return render(request, 'orders/transaction_form.html', {'form': form, 'booking': booking})


@role_required(User.UserRole.ADMIN, User.UserRole.CUSTOMER, User.UserRole.SELLER)
def transaction_list(request):
    if request.user.role == User.UserRole.ADMIN:
        transactions = Transaction.objects.select_related('booking', 'booking__customer', 'booking__seller').all()
    elif request.user.role == User.UserRole.SELLER:
        transactions = Transaction.objects.select_related('booking').filter(booking__seller=request.user)
    else:
        transactions = Transaction.objects.select_related('booking').filter(booking__customer=request.user)
    return render(request, 'orders/transaction_list.html', {'transactions': transactions})


@role_required(User.UserRole.ADMIN, User.UserRole.CUSTOMER, User.UserRole.SELLER)
def transaction_detail(request, transaction_id):
    if request.user.role == User.UserRole.ADMIN:
        transaction = get_object_or_404(Transaction.objects.select_related('booking'), id=transaction_id)
    elif request.user.role == User.UserRole.SELLER:
        transaction = get_object_or_404(
            Transaction.objects.select_related('booking'),
            id=transaction_id,
            booking__seller=request.user,
        )
    else:
        transaction = get_object_or_404(
            Transaction.objects.select_related('booking'),
            id=transaction_id,
            booking__customer=request.user,
        )
    return render(request, 'orders/transaction_detail.html', {'transaction': transaction})

# Create your views here.


