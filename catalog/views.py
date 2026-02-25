from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Avg
from django.db.models import Count
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.shortcuts import resolve_url
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from accounts.decorators import role_required
from accounts.models import SellerProfile
from accounts.models import User
from catalog.delivery_prediction import attach_delivery_predictions
from catalog.delivery_prediction import predict_delivery_for_product
from catalog.restock_prediction import attach_restock_predictions
from catalog.cart import add_product
from catalog.cart import cart_snapshot
from catalog.cart import remove_product
from catalog.cart import update_product_quantity
from catalog.forms import CategoryForm
from catalog.forms import ProductForm
from catalog.models import Category
from catalog.models import Product
from locations.models import District
from locations.models import Location
from locations.models import State


def _safe_next_url(request, fallback='accounts:dashboard'):
    candidate = (
        request.POST.get('next')
        or request.GET.get('next')
        or request.META.get('HTTP_REFERER')
    )
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return resolve_url(fallback)


def _customer_profile_location(user):
    try:
        return user.customer_profile.location
    except ObjectDoesNotExist:
        return None


def _parse_quantity(value, default=1):
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = default
    return max(0, quantity)


def _parse_positive_int(raw_value):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _seller_display_name(seller_user):
    try:
        store_name = (seller_user.seller_profile.store_name or '').strip()
    except ObjectDoesNotExist:
        store_name = ''
    return store_name or seller_user.display_name


def _seller_catalog_url(seller_id):
    return f"{reverse('catalog:product_list')}?{urlencode({'seller': seller_id})}"


def _seller_is_suspended(seller_user):
    try:
        profile = seller_user.seller_profile
        return bool(
            profile.is_suspended
            or profile.verification_status == SellerProfile.VerificationStatus.REJECTED
        )
    except ObjectDoesNotExist:
        return False


def _seller_suspension_guard(request):
    if (
        request.user.is_authenticated
        and request.user.role == User.UserRole.SELLER
        and _seller_is_suspended(request.user)
    ):
        messages.error(
            request,
            'Your seller account is frozen/terminated due to risk review. Selling actions are disabled.',
        )
        return redirect('analytics:seller_risk_incident')
    return None


def _customer_visible_seller_filter(prefix='seller'):
    return (
        (
            Q(**{f'{prefix}__seller_profile__is_suspended': False})
            & ~Q(**{f'{prefix}__seller_profile__verification_status': SellerProfile.VerificationStatus.REJECTED})
        )
        | Q(**{f'{prefix}__seller_profile__isnull': True})
    )


def _is_ajax_request(request):
    return (
        request.headers.get('x-requested-with') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('accept', '')
    )


def _cart_payload(request):
    snapshot = cart_snapshot(request)
    return {
        'item_count': snapshot['cart_item_count'],
        'available_item_count': snapshot.get('cart_available_item_count', snapshot['cart_item_count']),
        'unavailable_count': snapshot.get('cart_unavailable_count', 0),
        'checkout_blocked': snapshot.get('cart_checkout_blocked', False),
        'total_amount': f"{snapshot['cart_total_amount']:.2f}",
        'items': [
            {
                'product_id': item['product'].id,
                'name': item['product'].name,
                'category': item['product'].category.name if item['product'].category_id else '',
                'seller': item['product'].seller.display_name if item['product'].seller_id else '',
                'unit_price': f"{item['unit_price']:.2f}",
                'subtotal': f"{item['subtotal']:.2f}",
                'quantity': item['quantity'],
                'max_quantity': item['product'].stock_quantity,
                'detail_url': reverse('catalog:product_detail', args=[item['product'].id]),
                'update_url': reverse('catalog:cart_update', args=[item['product'].id]),
                'remove_url': reverse('catalog:cart_remove', args=[item['product'].id]),
            }
            for item in snapshot['cart_items']
        ],
        'unavailable_items': [
            {
                'product_id': item['product_id'],
                'name': item['name'],
                'category': item['category'],
                'seller': item['seller'],
                'quantity': item['quantity'],
                'unit_price': f"{item['unit_price']:.2f}",
                'subtotal': f"{item['subtotal']:.2f}",
                'reason': item['reason'],
                'detail_url': reverse('catalog:product_detail', args=[item['product_id']]),
                'remove_url': reverse('catalog:cart_remove', args=[item['product_id']]),
            }
            for item in snapshot.get('cart_unavailable_items', [])
        ],
    }


@role_required(User.UserRole.ADMIN, User.UserRole.SELLER)
def category_list_create(request):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category added successfully.')
            return redirect('catalog:category_list')
    else:
        form = CategoryForm()

    context = {
        'form': form,
        'categories': Category.objects.all(),
    }
    return render(request, 'catalog/category_list.html', context)


@role_required(User.UserRole.ADMIN, User.UserRole.SELLER)
def category_delete(request, category_id):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    if request.method != 'POST':
        return redirect('catalog:category_list')

    category = get_object_or_404(Category, id=category_id)
    try:
        category.delete()
        messages.success(request, 'Category deleted.')
    except ProtectedError:
        messages.error(request, 'Cannot delete this category because products are linked to it.')
    return redirect('catalog:category_list')


@role_required(User.UserRole.ADMIN, User.UserRole.SELLER)
def category_edit(request, category_id):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    category = get_object_or_404(Category, id=category_id)
    if request.method == 'POST':
        form = CategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category updated successfully.')
            return redirect('catalog:category_list')
    else:
        form = CategoryForm(instance=category)
    return render(request, 'catalog/category_form.html', {'form': form, 'category': category})


@role_required(User.UserRole.ADMIN, User.UserRole.SELLER)
def category_toggle_availability(request, category_id):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    if request.method != 'POST':
        return redirect('catalog:category_list')

    category = get_object_or_404(Category, id=category_id)
    category.is_active = request.POST.get('is_active') == 'on'
    category.save(update_fields=['is_active'])
    state_label = 'On' if category.is_active else 'Off'
    messages.success(request, f'Category "{category.name}" is now {state_label}.')
    return redirect('catalog:category_list')


def product_list(request):
    query = ' '.join((request.GET.get('q') or '').split())
    selected_category_id = _parse_positive_int(request.GET.get('category'))
    selected_seller_id = _parse_positive_int(request.GET.get('seller'))
    is_seller_user = (
        request.user.is_authenticated
        and request.user.role == User.UserRole.SELLER
    )
    is_admin_user = (
        request.user.is_authenticated
        and request.user.role == User.UserRole.ADMIN
    )
    seller_is_suspended = is_seller_user and _seller_is_suspended(request.user)
    if is_seller_user:
        selected_seller_id = request.user.id

    products_qs = (
        Product.objects.select_related(
            'category',
            'seller',
            'seller__seller_profile',
            'location',
            'location__district',
            'location__district__state',
        )
        .prefetch_related('serviceable_states', 'serviceable_districts', 'serviceable_locations')
    )
    if is_admin_user:
        base_products = products_qs
    elif is_seller_user:
        base_products = products_qs.filter(seller=request.user)
    else:
        base_products = (
            products_qs.filter(is_active=True)
            .filter(category__is_active=True)
            .filter(stock_quantity__gt=0)
            .filter(_customer_visible_seller_filter('seller'))
            .filter(
                Q(location__isnull=False)
                | Q(serviceable_states__isnull=False)
                | Q(serviceable_districts__isnull=False)
                | Q(serviceable_locations__isnull=False)
            )
            .distinct()
        )

    if query:
        base_products = base_products.filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(category__name__icontains=query)
            | Q(seller__first_name__icontains=query)
            | Q(seller__last_name__icontains=query)
            | Q(seller__email__icontains=query)
            | Q(seller__seller_profile__store_name__icontains=query)
        ).distinct()

    out_of_stock_match_count = 0
    if query and not is_admin_user and not is_seller_user:
        out_of_stock_match_count = (
            products_qs.filter(
                is_active=True,
                category__is_active=True,
                stock_quantity__lte=0,
            )
            .filter(_customer_visible_seller_filter('seller'))
            .filter(
                Q(name__icontains=query)
                | Q(description__icontains=query)
                | Q(category__name__icontains=query)
                | Q(seller__first_name__icontains=query)
                | Q(seller__last_name__icontains=query)
                | Q(seller__email__icontains=query)
                | Q(seller__seller_profile__store_name__icontains=query)
            )
            .distinct()
            .count()
        )

    if selected_category_id:
        base_products = base_products.filter(category_id=selected_category_id)
    if selected_seller_id:
        base_products = base_products.filter(seller_id=selected_seller_id)

    if is_admin_user:
        categories = (
            Category.objects.annotate(product_count=Count('products', distinct=True))
            .filter(product_count__gt=0)
            .order_by('name')
        )
        seller_options = (
            User.objects.select_related('seller_profile')
            .filter(role=User.UserRole.SELLER)
            .annotate(product_count=Count('products', distinct=True))
            .filter(product_count__gt=0)
        )
    elif is_seller_user:
        categories = (
            Category.objects.filter(products__seller=request.user)
            .annotate(
                product_count=Count(
                    'products',
                    filter=Q(products__seller=request.user),
                    distinct=True,
                )
            )
            .filter(product_count__gt=0)
            .order_by('name')
            .distinct()
        )
        seller_options = (
            User.objects.select_related('seller_profile')
            .filter(id=request.user.id)
            .annotate(
                product_count=Count(
                    'products',
                    filter=Q(products__seller=request.user),
                    distinct=True,
                )
            )
        )
    else:
        categories = (
            Category.objects.filter(is_active=True)
            .annotate(
                product_count=Count(
                    'products',
                    filter=Q(products__is_active=True)
                    & _customer_visible_seller_filter('products__seller'),
                    distinct=True,
                )
            )
            .filter(product_count__gt=0)
            .order_by('name')
        )
        seller_options = (
            User.objects.select_related('seller_profile')
            .filter(role=User.UserRole.SELLER)
            .filter(
                (
                    Q(seller_profile__is_suspended=False)
                    & ~Q(seller_profile__verification_status=SellerProfile.VerificationStatus.REJECTED)
                )
                | Q(seller_profile__isnull=True)
            )
            .annotate(
                product_count=Count(
                    'products',
                    filter=Q(products__is_active=True)
                    & _customer_visible_seller_filter('products__seller'),
                    distinct=True,
                )
            )
            .filter(product_count__gt=0)
        )

    sellers = sorted(
        (
            {
                'id': seller.id,
                'name': _seller_display_name(seller),
                'product_count': seller.product_count,
            }
            for seller in seller_options
        ),
        key=lambda item: item['name'].lower(),
    )
    active_seller = next(
        (item for item in sellers if item['id'] == selected_seller_id),
        None,
    )

    cart_query = request.GET.copy()
    cart_query['cart'] = 'open'
    cart_next_url = f'{request.path}?{cart_query.urlencode()}'
    products = list(base_products.order_by('-updated_at')[:60])
    attach_delivery_predictions(products)

    context = {
        'products': products,
        'product_count': base_products.count(),
        'query': query,
        'categories': categories,
        'sellers': sellers,
        'active_category_id': selected_category_id,
        'active_seller_id': selected_seller_id,
        'active_seller': active_seller,
        'cart_next_url': cart_next_url,
        'manage_next_url': request.get_full_path(),
        'is_seller_user': is_seller_user,
        'is_admin_user': is_admin_user,
        'seller_is_suspended': seller_is_suspended,
        'out_of_stock_match_count': out_of_stock_match_count,
    }
    return render(request, 'catalog/product_list.html', context)


def product_detail(request, product_id):
    product_qs = Product.objects.select_related(
        'category',
        'seller',
        'seller__seller_profile',
        'location',
        'location__district',
        'location__district__state',
    ).prefetch_related(
        'serviceable_states',
        'serviceable_districts__state',
        'serviceable_locations__district__state',
    )
    if request.user.is_authenticated and request.user.role == User.UserRole.ADMIN:
        product_lookup = product_qs
    elif request.user.is_authenticated and request.user.role == User.UserRole.SELLER:
        product_lookup = product_qs.filter(Q(is_active=True) | Q(seller=request.user))
    else:
        product_lookup = (
            product_qs.filter(is_active=True, category__is_active=True)
            .filter(_customer_visible_seller_filter('seller'))
        )

    product = get_object_or_404(product_lookup, id=product_id)
    customer_location = None
    if request.user.is_authenticated and request.user.role == User.UserRole.CUSTOMER:
        customer_location = _customer_profile_location(request.user)

    is_booking_available = True
    serviceability_note = ''
    if customer_location:
        is_booking_available = product.is_serviceable_for_location(customer_location)
        if not is_booking_available:
            serviceability_note = (
                f'Not serviceable for your saved pincode {customer_location.postal_code}. '
                'Choose another pincode while booking.'
            )
    elif request.user.is_authenticated and request.user.role == User.UserRole.CUSTOMER:
        serviceability_note = 'Choose a delivery pincode while booking to confirm serviceability.'
    if request.user.is_authenticated and request.user.role == User.UserRole.CUSTOMER and _seller_is_suspended(product.seller):
        is_booking_available = False
        serviceability_note = (
            'This seller is currently frozen/terminated after risk review. '
            'Booking is temporarily unavailable.'
        )

    saved_address_checks = []
    serviceable_saved_addresses = []
    non_serviceable_saved_addresses = []
    if request.user.is_authenticated and request.user.role == User.UserRole.CUSTOMER:
        saved_addresses = request.user.saved_addresses.select_related(
            'location',
            'location__district',
            'location__district__state',
        ).filter(is_active=True, location__isnull=False)
        for saved_address in saved_addresses:
            is_serviceable = product.is_serviceable_for_location(saved_address.location)
            row = {
                'address': saved_address,
                'location': saved_address.location,
                'is_serviceable': is_serviceable,
            }
            saved_address_checks.append(row)
            if is_serviceable:
                serviceable_saved_addresses.append(row)
            else:
                non_serviceable_saved_addresses.append(row)

    checked_pincode = (request.GET.get('check_pincode') or '').strip()
    pincode_check_result = None
    if checked_pincode:
        matching_locations = list(
            Location.objects.select_related('district', 'district__state')
            .filter(
                postal_code__iexact=checked_pincode,
                is_active=True,
                district__is_active=True,
                district__state__is_active=True,
            )
            .order_by('district__state__name', 'district__name', 'name')
        )
        match_rows = [
            {
                'location': location,
                'is_serviceable': product.is_serviceable_for_location(location),
            }
            for location in matching_locations
        ]
        pincode_check_result = {
            'pincode': checked_pincode,
            'has_active_location': bool(matching_locations),
            'is_any_serviceable': any(item['is_serviceable'] for item in match_rows),
            'matches': match_rows,
        }

    delivery_prediction = predict_delivery_for_product(product)
    rating_summary = product.feedbacks.aggregate(
        average_rating=Avg('rating'),
        rating_count=Count('id'),
    )
    average_rating = rating_summary.get('average_rating')
    rating_count = rating_summary.get('rating_count') or 0

    return render(
        request,
        'catalog/product_detail.html',
        {
            'product': product,
            'seller_display_name': _seller_display_name(product.seller),
            'seller_catalog_url': _seller_catalog_url(product.seller_id),
            'customer_location': customer_location,
            'is_booking_available': is_booking_available,
            'serviceability_note': serviceability_note,
            'serviceable_states': product.serviceable_states.filter(is_active=True),
            'serviceable_districts': product.serviceable_districts.filter(
                is_active=True,
                state__is_active=True,
            ),
            'serviceable_locations': product.serviceable_locations.filter(
                is_active=True,
                district__is_active=True,
                district__state__is_active=True,
            ),
            'saved_address_checks': saved_address_checks,
            'serviceable_saved_addresses': serviceable_saved_addresses,
            'non_serviceable_saved_addresses': non_serviceable_saved_addresses,
            'checked_pincode': checked_pincode,
            'pincode_check_result': pincode_check_result,
            'predicted_delivery_days': delivery_prediction.days,
            'predicted_delivery_date': delivery_prediction.expected_date,
            'predicted_delivery_is_fallback': delivery_prediction.is_fallback,
            'average_rating': average_rating,
            'rating_count': rating_count,
            'reviews_url': f"{reverse('support:feedback_list')}?{urlencode({'product': product.id})}",
        },
    )


@role_required(User.UserRole.CUSTOMER)
def cart_add(request, product_id):
    target_url = _safe_next_url(request)
    if request.method != 'POST':
        return redirect(target_url)

    product = (
        Product.objects.select_related(
            'seller',
            'seller__seller_profile',
            'location',
            'location__district',
            'location__district__state',
        ).prefetch_related(
            'serviceable_states',
            'serviceable_districts__state',
            'serviceable_locations__district__state',
        )
        .filter(
            id=product_id,
            is_active=True,
            category__is_active=True,
            seller__is_active=True,
        )
        .first()
    )
    if not product:
        if _is_ajax_request(request):
            return JsonResponse(
                {
                    'ok': False,
                    'message': 'This product category is non-listed now. Product is unavailable.',
                    'cart': _cart_payload(request),
                },
                status=400,
            )
        messages.error(request, 'This product category is non-listed now. Product is unavailable.')
        return redirect(target_url)
    quantity = _parse_quantity(request.POST.get('quantity'), default=1)

    if _seller_is_suspended(product.seller):
        if _is_ajax_request(request):
            return JsonResponse(
                {
                    'ok': False,
                    'message': 'This seller is currently frozen/terminated. Product is unavailable for booking/cart.',
                    'cart': _cart_payload(request),
                },
                status=400,
            )
        messages.error(request, 'This seller is currently frozen/terminated. Product is unavailable.')
        return redirect(target_url)

    if product.stock_quantity <= 0:
        if _is_ajax_request(request):
            return JsonResponse(
                {
                    'ok': False,
                    'message': f'{product.name} is out of stock right now.',
                    'cart': _cart_payload(request),
                },
                status=400,
            )
        messages.error(request, f'{product.name} is out of stock right now.')
        return redirect(target_url)

    customer_location = _customer_profile_location(request.user)
    if customer_location and not product.is_serviceable_for_location(customer_location):
        if _is_ajax_request(request):
            return JsonResponse(
                {
                    'ok': False,
                    'message': (
                        f'{product.name} is not serviceable for your saved pincode '
                        f'{customer_location.postal_code}.'
                    ),
                    'cart': _cart_payload(request),
                },
                status=400,
            )
        messages.error(
            request,
            (
                f'{product.name} is not serviceable for your saved pincode '
                f'{customer_location.postal_code}.'
            ),
        )
        return redirect(target_url)

    cart_result = add_product(request, product, quantity=quantity or 1)
    max_stock = cart_result.get('max_stock', max(product.stock_quantity, 0))
    unit_label = 'item' if max_stock == 1 else 'items'
    if cart_result.get('limited_by_stock'):
        warning_message = (
            f'Only {max_stock} {unit_label} available for {product.name}. '
            'You cannot add more quantity right now.'
        )
        if _is_ajax_request(request):
            return JsonResponse(
                {
                    'ok': True,
                    'warning': True,
                    'message': warning_message,
                    'cart': _cart_payload(request),
                }
            )
        messages.warning(request, warning_message)
        return redirect(target_url)
    if _is_ajax_request(request):
        return JsonResponse(
            {
                'ok': True,
                'message': f'{product.name} was added to your cart.',
                'cart': _cart_payload(request),
            }
        )
    messages.success(request, f'{product.name} was added to your cart.')
    return redirect(target_url)


@role_required(User.UserRole.CUSTOMER)
def cart_update(request, product_id):
    target_url = _safe_next_url(request)
    if request.method != 'POST':
        return redirect(target_url)

    product = (
        Product.objects.select_related('seller', 'seller__seller_profile')
        .filter(id=product_id, is_active=True, category__is_active=True)
        .first()
    )
    if not product or _seller_is_suspended(product.seller):
        remove_product(request, product_id)
        if _is_ajax_request(request):
            return JsonResponse(
                {
                    'ok': False,
                    'message': 'This product is no longer available and was removed from your cart.',
                    'cart': _cart_payload(request),
                },
                status=400,
            )
        messages.info(request, 'This product is no longer available and was removed from your cart.')
        return redirect(target_url)
    quantity = _parse_quantity(request.POST.get('quantity'), default=1)

    cart_result = update_product_quantity(request, product, quantity)
    applied_quantity = cart_result.get('quantity', min(quantity, product.stock_quantity))
    max_stock = cart_result.get('max_stock', max(product.stock_quantity, 0))
    unit_label = 'item' if max_stock == 1 else 'items'
    limited_by_stock = cart_result.get('limited_by_stock', False)
    if _is_ajax_request(request):
        if quantity <= 0:
            message = f'{product.name} was removed from your cart.'
        elif limited_by_stock:
            message = (
                f'Only {max_stock} {unit_label} available for {product.name}. '
                f'Cart quantity kept at {applied_quantity}.'
            )
        else:
            message = f'Updated {product.name} quantity to {applied_quantity}.'
        return JsonResponse(
            {
                'ok': True,
                'warning': limited_by_stock,
                'message': message,
                'cart': _cart_payload(request),
            }
        )
    if quantity <= 0:
        messages.info(request, f'{product.name} was removed from your cart.')
    elif limited_by_stock:
        messages.warning(
            request,
            (
                f'Only {max_stock} {unit_label} available for {product.name}. '
                f'Cart quantity kept at {applied_quantity}.'
            ),
        )
    else:
        messages.success(request, f'Updated {product.name} quantity to {applied_quantity}.')
    return redirect(target_url)


@role_required(User.UserRole.CUSTOMER)
def cart_remove(request, product_id):
    target_url = _safe_next_url(request)
    if request.method != 'POST':
        return redirect(target_url)

    remove_product(request, product_id)
    if _is_ajax_request(request):
        return JsonResponse(
            {
                'ok': True,
                'message': 'Item removed from your cart.',
                'cart': _cart_payload(request),
            }
        )
    messages.info(request, 'Item removed from your cart.')
    return redirect(target_url)


@role_required(User.UserRole.SELLER)
def seller_inventory(request):
    seller_is_suspended = _seller_is_suspended(request.user)
    inventory_query = ' '.join((request.GET.get('q') or '').split())
    has_categories = Category.objects.filter(is_active=True).exists()
    has_service_areas = (
        State.objects.filter(is_active=True).exists()
        or District.objects.filter(is_active=True, state__is_active=True).exists()
        or Location.objects.filter(
            is_active=True,
            district__is_active=True,
            district__state__is_active=True,
        ).exists()
    )
    if request.method == 'GET' and not has_categories:
        messages.warning(
            request,
            'No active categories are available yet. Please ask an admin to create product categories first.',
        )
    if request.method == 'GET' and not has_service_areas:
        messages.warning(
            request,
            'No active service areas are available. Ask admin to enable states/districts/pincodes first.',
        )
    if request.method == 'GET' and seller_is_suspended:
        messages.error(
            request,
            'Your seller account is frozen/terminated. Product add/edit/delete and stock updates are disabled until review completes.',
        )
    category_non_listed_product_count = Product.objects.filter(
        seller=request.user,
        category__is_active=False,
    ).count()
    if request.method == 'GET' and category_non_listed_product_count:
        messages.warning(
            request,
            (
                f'{category_non_listed_product_count} product(s) are in categories turned Off. '
                'Category non-listed now for customer booking and dashboard listing.'
            ),
        )

    if request.method == 'POST':
        if seller_is_suspended:
            messages.error(request, 'Selling actions are disabled while your account is frozen/terminated.')
            return redirect('analytics:seller_risk_incident')
        form = ProductForm(request.POST, request.FILES)
        if form.is_valid():
            product = form.save(commit=False)
            product.seller = request.user
            product.save()
            form.save_m2m()
            messages.success(request, 'Product added to your inventory.')
            return redirect('catalog:seller_inventory')
    else:
        form = ProductForm()

    district_state_map = {
        district.id: district.state_id
        for district in form.fields['serviceable_districts'].queryset.only('id', 'state_id')
    }

    all_products = Product.objects.filter(seller=request.user)
    products = all_products.select_related('category')
    if inventory_query:
        products = products.filter(
            Q(name__icontains=inventory_query)
            | Q(description__icontains=inventory_query)
            | Q(category__name__icontains=inventory_query)
            | Q(serviceable_states__name__icontains=inventory_query)
            | Q(serviceable_districts__name__icontains=inventory_query)
            | Q(serviceable_locations__postal_code__icontains=inventory_query)
        ).distinct()

    products = products.annotate(
        serviceable_state_count=Count('serviceable_states', distinct=True),
        serviceable_district_count=Count('serviceable_districts', distinct=True),
        serviceable_location_count=Count('serviceable_locations', distinct=True),
    ).order_by('-updated_at')
    total_products = all_products.count()
    active_products = all_products.filter(is_active=True).count()
    context = {
        'form': form,
        'products': products,
        'inventory_query': inventory_query,
        'total_products': total_products,
        'active_products': active_products,
        'inactive_products': max(total_products - active_products, 0),
        'filtered_products': products.count(),
        'district_state_map': district_state_map,
        'has_categories': has_categories,
        'has_service_areas': has_service_areas,
        'seller_is_suspended': seller_is_suspended,
        'category_non_listed_product_count': category_non_listed_product_count,
    }
    return render(request, 'catalog/seller_inventory.html', context)


@role_required(User.UserRole.SELLER)
def seller_restock_dashboard(request):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    query = ' '.join((request.GET.get('q') or '').split())
    low_stock_threshold = 5
    products_qs = Product.objects.filter(seller=request.user).select_related('category')
    if query:
        products_qs = products_qs.filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(category__name__icontains=query)
        )
    products = list(products_qs.order_by('stock_quantity', '-updated_at')[:240])
    attach_restock_predictions(products, reorder_level=low_stock_threshold)

    low_stock_products = [product for product in products if product.stock_quantity <= low_stock_threshold]
    out_of_stock_products = [product for product in products if product.stock_quantity <= 0]

    context = {
        'products': products,
        'query': query,
        'low_stock_threshold': low_stock_threshold,
        'total_products': len(products),
        'low_stock_count': len(low_stock_products),
        'out_of_stock_count': len(out_of_stock_products),
    }
    return render(request, 'catalog/seller_restock_dashboard.html', context)


@role_required(User.UserRole.SELLER)
def seller_product_edit(request, product_id):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    product = get_object_or_404(Product, id=product_id, seller=request.user)
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, 'Product updated.')
            return redirect('catalog:seller_inventory')
    else:
        form = ProductForm(instance=product)
    district_state_map = {
        district.id: district.state_id
        for district in form.fields['serviceable_districts'].queryset.only('id', 'state_id')
    }
    return render(
        request,
        'catalog/seller_product_form.html',
        {
            'form': form,
            'product': product,
            'district_state_map': district_state_map,
        },
    )


@role_required(User.UserRole.SELLER)
def seller_product_delete(request, product_id):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    target_url = _safe_next_url(request, fallback='catalog:product_list')
    if request.method != 'POST':
        return redirect(target_url)

    product = get_object_or_404(Product, id=product_id, seller=request.user)
    try:
        product.delete()
        messages.success(request, 'Product removed from inventory.')
    except ProtectedError:
        messages.error(
            request,
            'Cannot delete this product because bookings/transactions are linked to it. Turn it Off instead.',
        )
    return redirect(target_url)


@role_required(User.UserRole.SELLER)
def seller_product_toggle_availability(request, product_id):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    target_url = _safe_next_url(request, fallback='catalog:product_list')
    if request.method != 'POST':
        return redirect(target_url)

    product = get_object_or_404(Product, id=product_id, seller=request.user)
    product.is_active = request.POST.get('is_active') == 'on'
    product.save(update_fields=['is_active'])
    state_label = 'On' if product.is_active else 'Off'
    messages.success(request, f'Product "{product.name}" is now {state_label}.')
    return redirect(target_url)


@role_required(User.UserRole.SELLER)
def seller_product_update_stock(request, product_id):
    suspended_response = _seller_suspension_guard(request)
    if suspended_response:
        return suspended_response

    target_url = _safe_next_url(request, fallback='catalog:product_list')
    if request.method != 'POST':
        return redirect(target_url)

    product = get_object_or_404(Product, id=product_id, seller=request.user)
    try:
        stock_quantity = int(request.POST.get('stock_quantity'))
    except (TypeError, ValueError):
        messages.error(request, 'Enter a valid stock quantity.')
        return redirect(target_url)

    if stock_quantity < 0:
        messages.error(request, 'Stock quantity cannot be negative.')
        return redirect(target_url)

    product.stock_quantity = stock_quantity
    product.save(update_fields=['stock_quantity'])
    messages.success(request, f'Stock updated for "{product.name}" to {product.stock_quantity}.')
    return redirect(target_url)


@role_required(User.UserRole.ADMIN)
def admin_product_toggle_availability(request, product_id):
    target_url = _safe_next_url(request, fallback='catalog:product_list')
    if request.method != 'POST':
        return redirect(target_url)

    product = get_object_or_404(Product, id=product_id)
    product.is_active = request.POST.get('is_active') == 'on'
    product.save(update_fields=['is_active'])
    state_label = 'On' if product.is_active else 'Off'
    messages.success(request, f'Product "{product.name}" is now {state_label}.')
    return redirect(target_url)


@role_required(User.UserRole.ADMIN)
def admin_product_delete(request, product_id):
    target_url = _safe_next_url(request, fallback='catalog:product_list')
    if request.method != 'POST':
        return redirect(target_url)

    product = get_object_or_404(Product, id=product_id)
    try:
        product.delete()
        messages.success(request, 'Product removed by admin.')
    except ProtectedError:
        messages.error(
            request,
            'Cannot delete this product because bookings/transactions are linked to it. Turn it Off instead.',
        )
    return redirect(target_url)

# Create your views here.
