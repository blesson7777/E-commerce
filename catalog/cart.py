from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist

from catalog.models import Product


CART_SESSION_KEY = 'cart_items'


def _sanitize_quantity(value, default=1):
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = default
    return max(0, quantity)


def get_cart(request):
    raw = request.session.get(CART_SESSION_KEY, {})
    if not isinstance(raw, dict):
        raw = {}

    cleaned = {}
    for product_id, quantity in raw.items():
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            continue
        qty = _sanitize_quantity(quantity)
        if qty > 0:
            cleaned[str(pid)] = qty

    if cleaned != raw:
        request.session[CART_SESSION_KEY] = cleaned
        request.session.modified = True

    return cleaned


def save_cart(request, cart):
    request.session[CART_SESSION_KEY] = cart
    request.session.modified = True


def add_product(request, product, quantity=1):
    cart = get_cart(request)
    key = str(product.id)
    current = cart.get(key, 0)
    max_stock = max(product.stock_quantity, 0)
    requested_quantity = _sanitize_quantity(quantity, default=1)
    limited_by_stock = False
    final_quantity = 0
    if max_stock <= 0:
        cart.pop(key, None)
    else:
        final_quantity = min(current + requested_quantity, max_stock)
        limited_by_stock = (current + requested_quantity) > max_stock
        cart[key] = final_quantity
    save_cart(request, cart)
    return {
        'previous_quantity': current,
        'quantity': final_quantity,
        'requested_quantity': requested_quantity,
        'max_stock': max_stock,
        'limited_by_stock': limited_by_stock,
    }


def update_product_quantity(request, product, quantity):
    cart = get_cart(request)
    key = str(product.id)
    qty = _sanitize_quantity(quantity)
    current = cart.get(key, 0)
    max_stock = max(product.stock_quantity, 0)
    limited_by_stock = False
    final_quantity = 0
    if qty <= 0 or max_stock <= 0:
        cart.pop(key, None)
    else:
        final_quantity = min(qty, max_stock)
        limited_by_stock = qty > max_stock
        cart[key] = final_quantity
    save_cart(request, cart)
    return {
        'previous_quantity': current,
        'quantity': final_quantity,
        'requested_quantity': qty,
        'max_stock': max_stock,
        'limited_by_stock': limited_by_stock,
    }


def remove_product(request, product_id):
    cart = get_cart(request)
    cart.pop(str(product_id), None)
    save_cart(request, cart)


def cart_snapshot(request):
    cart = get_cart(request)
    if not cart:
        return {
            'cart_items': [],
            'cart_unavailable_items': [],
            'cart_item_count': 0,
            'cart_available_item_count': 0,
            'cart_unavailable_count': 0,
            'cart_total_amount': Decimal('0.00'),
            'cart_checkout_blocked': False,
        }

    product_ids = [int(pid) for pid in cart.keys()]
    products = Product.objects.select_related('seller', 'category').filter(id__in=product_ids)
    product_map = {product.id: product for product in products}

    available_items = []
    unavailable_items = []
    total_amount = Decimal('0.00')
    total_quantity = 0
    available_quantity = 0
    normalized_cart = {}

    def _unavailable_reason(product):
        if not product:
            return 'This product is no longer available.'
        if not product.is_active:
            return 'This product is currently unavailable.'
        if not product.category_id or not product.category.is_active:
            return 'This product category is non-listed now.'
        try:
            profile = product.seller.seller_profile
            seller_suspended = bool(
                profile.is_suspended
                or profile.verification_status == 'rejected'
            )
        except ObjectDoesNotExist:
            seller_suspended = False
        if seller_suspended:
            return 'This seller is currently frozen/terminated.'
        if product.stock_quantity <= 0:
            return 'This product is out of stock right now.'
        return ''

    for pid_str, requested_qty in cart.items():
        pid = int(pid_str)
        product = product_map.get(pid)
        quantity = _sanitize_quantity(requested_qty, default=1)
        if quantity <= 0:
            continue

        reason = _unavailable_reason(product)
        if reason:
            unavailable_items.append(
                {
                    'product_id': pid,
                    'name': product.name if product else f'Item #{pid}',
                    'category': (
                        product.category.name
                        if product and product.category_id
                        else ''
                    ),
                    'seller': product.seller.display_name if product and product.seller_id else '',
                    'quantity': quantity,
                    'unit_price': product.price if product else Decimal('0.00'),
                    'subtotal': (product.price * quantity) if product else Decimal('0.00'),
                    'reason': reason,
                }
            )
            normalized_cart[pid_str] = quantity
            total_quantity += quantity
            continue

        max_stock = max(product.stock_quantity, 0)
        quantity = min(quantity, max_stock)
        if quantity <= 0:
            unavailable_items.append(
                {
                    'product_id': pid,
                    'name': product.name,
                    'category': product.category.name if product.category_id else '',
                    'seller': product.seller.display_name if product.seller_id else '',
                    'quantity': _sanitize_quantity(requested_qty, default=1),
                    'unit_price': product.price,
                    'subtotal': Decimal('0.00'),
                    'reason': 'This product is out of stock right now.',
                }
            )
            normalized_cart[pid_str] = _sanitize_quantity(requested_qty, default=1)
            total_quantity += _sanitize_quantity(requested_qty, default=1)
            continue

        subtotal = product.price * quantity
        available_items.append(
            {
                'product': product,
                'quantity': quantity,
                'unit_price': product.price,
                'subtotal': subtotal,
            }
        )
        normalized_cart[pid_str] = quantity
        total_amount += subtotal
        total_quantity += quantity
        available_quantity += quantity

    if normalized_cart != cart:
        save_cart(request, normalized_cart)

    return {
        'cart_items': available_items,
        'cart_unavailable_items': unavailable_items,
        'cart_item_count': total_quantity,
        'cart_available_item_count': available_quantity,
        'cart_unavailable_count': len(unavailable_items),
        'cart_total_amount': total_amount,
        'cart_checkout_blocked': bool(unavailable_items),
    }
