import secrets
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator
from django.db.models import Avg
from django.db.models import Count
from django.db.models import Q
from django.db.models import Sum
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from accounts.decorators import role_required
from accounts.forms import AccountDeletionForm
from accounts.forms import CustomerAddressForm
from accounts.forms import ForgotPasswordOTPRequestForm
from accounts.forms import ForgotPasswordOTPVerifyForm
from accounts.forms import CustomerSignUpForm
from accounts.forms import ProfileUpdateForm
from accounts.forms import SellerSignUpForm
from accounts.forms import StyledAuthenticationForm
from accounts.models import CustomerAddress
from accounts.models import SellerProfile
from accounts.models import User
from analytics.models import SellerRiskIncident
from analytics.models import SellerRiskSnapshot
from catalog.delivery_prediction import attach_delivery_predictions
from catalog.delivery_prediction import predict_delivery_for_product
from catalog.models import Category
from catalog.models import Product
from catalog.restock_prediction import attach_restock_predictions
from config.context_processors import build_role_notifications
from config.context_processors import set_notifications_dismissed_for_user
from orders.models import Booking
from orders.models import Transaction
from support.models import Complaint


FORGOT_PASSWORD_SESSION_KEY = 'password_reset_otp_state'
FORGOT_PASSWORD_OTP_EXPIRY_MINUTES = 10
FORGOT_PASSWORD_MAX_ATTEMPTS = 5


def _generate_numeric_otp():
    return f'{secrets.randbelow(10 ** 6):06d}'


def _parse_iso_datetime(raw_value):
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _mask_email_address(email):
    local_part, separator, domain_part = (email or '').partition('@')
    if not separator:
        return email
    if len(local_part) <= 2:
        masked_local_part = f'{local_part[:1]}*'
    else:
        middle = '*' * max(1, len(local_part) - 2)
        masked_local_part = f'{local_part[0]}{middle}{local_part[-1]}'
    return f'{masked_local_part}@{domain_part}'


def _get_password_reset_state(request):
    state = request.session.get(FORGOT_PASSWORD_SESSION_KEY) or {}
    expires_at = _parse_iso_datetime(state.get('expires_at'))
    if not state or not expires_at:
        request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)
        return None
    if timezone.now() > expires_at:
        request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)
        return None
    return state


def _parse_positive_int(raw_value):
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _seller_display_name(seller_user):
    try:
        store_name = (seller_user.seller_profile.store_name or '').strip()
    except SellerProfile.DoesNotExist:
        store_name = ''
    return store_name or seller_user.display_name


def _active_seller_relation_filter(prefix='seller'):
    return (
        (
            Q(**{f'{prefix}__seller_profile__is_suspended': False})
            & ~Q(**{f'{prefix}__seller_profile__verification_status': SellerProfile.VerificationStatus.REJECTED})
        )
        | Q(**{f'{prefix}__seller_profile__isnull': True})
    )


def _active_seller_user_filter():
    return (
        (
            Q(seller_profile__is_suspended=False)
            & ~Q(seller_profile__verification_status=SellerProfile.VerificationStatus.REJECTED)
        )
    ) | Q(seller_profile__isnull=True)


def _storefront_base_url_for_request(request):
    if request and request.user.is_authenticated and request.user.role == User.UserRole.CUSTOMER:
        return reverse('accounts:dashboard')
    return reverse('home')


def _safe_redirect_target(request, fallback='accounts:dashboard'):
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
    return reverse(fallback)


def _build_storefront_url(request, **params):
    base_url = _storefront_base_url_for_request(request)
    cleaned_params = {}
    for key, value in params.items():
        if value in (None, ''):
            continue
        cleaned_params[key] = value
    if not cleaned_params:
        return base_url
    return f'{base_url}?{urlencode(cleaned_params)}'


def _extract_raw_shipping_address(shipping_address):
    value = (shipping_address or '').strip()
    if not value:
        return ''
    if '\nPincode:' in value:
        base = value.split('\nPincode:', 1)[0].strip()
        return base or value
    return value


def _sync_saved_addresses_from_bookings(user, limit=25):
    if not user.is_authenticated or user.role != User.UserRole.CUSTOMER:
        return

    recent_bookings = (
        Booking.objects.select_related('delivery_location')
        .filter(customer=user, delivery_location__isnull=False)
        .exclude(shipping_address='')
        .order_by('-booked_at')[:limit]
    )

    has_active_default = user.saved_addresses.filter(is_active=True, is_default=True).exists()
    for booking in recent_bookings:
        raw_address = _extract_raw_shipping_address(booking.shipping_address)
        if not raw_address:
            continue
        exists = user.saved_addresses.filter(
            address__iexact=raw_address,
            location=booking.delivery_location,
        ).exists()
        if exists:
            continue
        label = 'Home' if not has_active_default else f'Address {user.saved_addresses.count() + 1}'
        user.saved_addresses.create(
            label=label,
            address=raw_address,
            location=booking.delivery_location,
            is_default=not has_active_default,
            is_active=True,
        )
        has_active_default = True


def _storefront_context(user=None, request=None):
    available_products = (
        Product.objects.select_related(
            'category',
            'seller',
            'seller__seller_profile',
            'location',
            'location__district',
            'location__district__state',
        )
        .prefetch_related('serviceable_states', 'serviceable_districts', 'serviceable_locations')
        .filter(is_active=True)
        .filter(category__is_active=True)
        .filter(stock_quantity__gt=0)
        .filter(_active_seller_relation_filter('seller'))
        .filter(
            Q(location__isnull=False)
            | Q(serviceable_states__isnull=False)
            | Q(serviceable_districts__isnull=False)
            | Q(serviceable_locations__isnull=False)
        )
        .distinct()
        .annotate(
            average_rating=Avg('feedbacks__rating'),
            rating_count=Count('feedbacks', distinct=True),
        )
    )
    selected_category_id = _parse_positive_int(request.GET.get('category')) if request else None
    active_category = None
    if selected_category_id:
        active_category = Category.objects.filter(id=selected_category_id, is_active=True).first()
        if active_category is None:
            selected_category_id = None

    filtered_products = available_products
    if selected_category_id:
        filtered_products = filtered_products.filter(category_id=selected_category_id)

    featured_categories = (
        Category.objects.filter(is_active=True)
        .annotate(
            product_count=Count(
                'products',
                filter=Q(
                    products__is_active=True,
                )
                & _active_seller_relation_filter('products__seller'),
            )
        )
        .filter(product_count__gt=0)
        .order_by('-product_count', 'name')[:12]
    )

    category_products = filtered_products.order_by('-updated_at')
    products_paginator = Paginator(category_products, 10)
    page_number = request.GET.get('page') if request else None
    featured_products_page = products_paginator.get_page(page_number)
    featured_products = list(featured_products_page.object_list)
    latest_products = list(filtered_products.order_by('-updated_at')[:8])
    attach_delivery_predictions(featured_products + latest_products)
    featured_products_page.object_list = featured_products

    if request:
        cart_query = request.GET.copy()
        cart_query['cart'] = 'open'
        cart_next_url = f'{request.path}?{cart_query.urlencode()}'
    else:
        cart_next_url = '/?cart=open'

    context = {
        'featured_categories': featured_categories,
        'featured_products': featured_products_page,
        'featured_products_total': category_products.count(),
        'available_products_total': available_products.count(),
        'latest_products': latest_products,
        'active_category_id': selected_category_id,
        'active_category': active_category,
        'cart_next_url': cart_next_url,
        'seller_count': User.objects.filter(role=User.UserRole.SELLER).filter(_active_seller_user_filter()).count(),
    }

    if user and user.is_authenticated and user.role == User.UserRole.CUSTOMER:
        context.update(
            {
                'my_booking_count': Booking.objects.filter(customer=user).count(),
                'my_active_order_count': Booking.objects.filter(
                    customer=user,
                    status__in=[Booking.BookingStatus.CONFIRMED, Booking.BookingStatus.SHIPPED],
                ).count(),
                'my_completed_order_count': Booking.objects.filter(
                    customer=user,
                    status=Booking.BookingStatus.DELIVERED,
                ).count(),
            }
        )
    else:
        context.update(
            {
                'my_booking_count': 0,
                'my_active_order_count': 0,
                'my_completed_order_count': 0,
            }
        )

    return context


def home(request):
    if request.user.is_authenticated:
        target_url = reverse('accounts:dashboard')
        if request.GET:
            target_url = f'{target_url}?{request.GET.urlencode()}'
        return redirect(target_url)
    return render(request, 'accounts/customer_dashboard.html', _storefront_context(request=request))


class UserLoginView(LoginView):
    template_name = 'accounts/login.html'
    form_class = StyledAuthenticationForm


def password_reset_otp_request(request):
    if request.method == 'GET':
        request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)

    if request.method == 'POST':
        form = ForgotPasswordOTPRequestForm(request.POST)
        if form.is_valid():
            user = form.user
            otp_code = _generate_numeric_otp()
            expires_at = timezone.now() + timedelta(minutes=FORGOT_PASSWORD_OTP_EXPIRY_MINUTES)
            request.session[FORGOT_PASSWORD_SESSION_KEY] = {
                'user_id': user.id,
                'email': user.email,
                'otp_code': otp_code,
                'expires_at': expires_at.isoformat(),
                'attempts': 0,
            }
            context = {
                'user': user,
                'otp_code': otp_code,
                'expiry_minutes': FORGOT_PASSWORD_OTP_EXPIRY_MINUTES,
            }
            message = EmailMultiAlternatives(
                subject='Nature Nest password reset OTP',
                body=render_to_string('accounts/emails/password_reset_otp.txt', context),
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[user.email],
            )
            message.attach_alternative(
                render_to_string('accounts/emails/password_reset_otp.html', context),
                'text/html',
            )
            try:
                message.send(fail_silently=False)
            except Exception:
                request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)
                messages.error(
                    request,
                    'We could not send the OTP email right now. Please check SMTP settings and try again.',
                )
                return render(request, 'accounts/password_reset_form.html', {'form': form})
            return redirect('accounts:password_reset_done')
    else:
        form = ForgotPasswordOTPRequestForm()

    return render(request, 'accounts/password_reset_form.html', {'form': form})


def password_reset_otp_done(request):
    reset_state = _get_password_reset_state(request)
    if not reset_state:
        messages.error(request, 'OTP session expired. Please request a new OTP.')
        return redirect('accounts:password_reset')

    return render(
        request,
        'accounts/password_reset_done.html',
        {
            'masked_email': _mask_email_address(reset_state.get('email', '')),
            'expiry_minutes': FORGOT_PASSWORD_OTP_EXPIRY_MINUTES,
        },
    )


def password_reset_otp_confirm(request):
    reset_state = _get_password_reset_state(request)
    if not reset_state:
        messages.error(request, 'OTP session expired. Please request a new OTP.')
        return redirect('accounts:password_reset')

    user = User.objects.filter(
        id=reset_state.get('user_id'),
        email__iexact=reset_state.get('email'),
        is_active=True,
    ).first()
    if not user:
        request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)
        messages.error(request, 'Account not found for this OTP session. Please request a new OTP.')
        return redirect('accounts:password_reset')

    attempts = int(reset_state.get('attempts') or 0)
    attempts_left = FORGOT_PASSWORD_MAX_ATTEMPTS - attempts
    if attempts_left <= 0:
        request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)
        messages.error(request, 'Too many failed OTP attempts. Please request a new OTP.')
        return redirect('accounts:password_reset')

    if request.method == 'POST':
        form = ForgotPasswordOTPVerifyForm(request.POST, user=user)
        if form.is_valid():
            if form.cleaned_data['otp'] != str(reset_state.get('otp_code', '')):
                attempts += 1
                reset_state['attempts'] = attempts
                request.session[FORGOT_PASSWORD_SESSION_KEY] = reset_state
                attempts_left = FORGOT_PASSWORD_MAX_ATTEMPTS - attempts
                if attempts_left <= 0:
                    request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)
                    messages.error(request, 'Too many failed OTP attempts. Please request a new OTP.')
                    return redirect('accounts:password_reset')
                form.add_error('otp', f'Invalid OTP. {attempts_left} attempt(s) left.')
            else:
                user.set_password(form.cleaned_data['new_password1'])
                user.save(update_fields=['password'])
                request.session.pop(FORGOT_PASSWORD_SESSION_KEY, None)
                return redirect('accounts:password_reset_complete')
    else:
        form = ForgotPasswordOTPVerifyForm(user=user)

    return render(
        request,
        'accounts/password_reset_confirm.html',
        {
            'form': form,
            'masked_email': _mask_email_address(user.email),
            'expiry_minutes': FORGOT_PASSWORD_OTP_EXPIRY_MINUTES,
            'attempts_left': attempts_left,
        },
    )


def password_reset_otp_complete(request):
    return render(request, 'accounts/password_reset_complete.html')


@login_required
def mark_all_notifications_read(request):
    if request.method != 'POST':
        return redirect(_safe_redirect_target(request))

    notifications = build_role_notifications(request.user)
    set_notifications_dismissed_for_user(request, request.user, notifications)
    messages.success(request, 'All notifications marked as read.')
    return redirect(_safe_redirect_target(request))


@role_required(User.UserRole.SELLER)
def acknowledge_seller_risk_action(request):
    if request.method != 'POST':
        return redirect('accounts:dashboard')

    incident_id = _parse_positive_int(request.POST.get('incident_id'))
    if not incident_id:
        messages.error(request, 'Invalid risk incident reference.')
        return redirect('accounts:dashboard')

    incident = get_object_or_404(
        SellerRiskIncident,
        id=incident_id,
        seller=request.user,
    )
    incident.seller_acknowledged_at = timezone.now()
    incident.save(update_fields=['seller_acknowledged_at', 'updated_at'])
    messages.success(request, 'Risk action notice acknowledged.')
    return redirect('accounts:dashboard')


def customer_signup(request):
    if request.method == 'POST':
        form = CustomerSignUpForm(request.POST, request.FILES)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('accounts:dashboard')
    else:
        form = CustomerSignUpForm()
    return render(request, 'accounts/signup_customer.html', {'form': form})


def seller_signup(request):
    if request.method == 'POST':
        form = SellerSignUpForm(request.POST, request.FILES)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('accounts:dashboard')
    else:
        form = SellerSignUpForm()
    return render(request, 'accounts/signup_seller.html', {'form': form})


@login_required
def dashboard(request):
    user = request.user
    template_name = 'accounts/dashboard.html'

    if user.role == User.UserRole.ADMIN:
        successful_transactions_qs = Transaction.objects.filter(
            status=Transaction.TransactionStatus.SUCCESS
        )
        refunded_transactions_qs = Transaction.objects.filter(
            status=Transaction.TransactionStatus.REFUNDED
        )
        gross_revenue = successful_transactions_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        refunded_revenue = refunded_transactions_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        pending_complaints = Complaint.objects.filter(
            status__in=[Complaint.ComplaintStatus.OPEN, Complaint.ComplaintStatus.IN_PROGRESS]
        ).count()
        active_incidents = SellerRiskIncident.objects.filter(is_active=True).count()

        top_sellers = list(
            User.objects.select_related('seller_profile')
            .filter(role=User.UserRole.SELLER)
            .annotate(
                booking_count=Count('seller_bookings', distinct=True),
                revenue_total=Sum(
                    'seller_bookings__transactions__amount',
                    filter=Q(seller_bookings__transactions__status=Transaction.TransactionStatus.SUCCESS),
                ),
            )
            .filter(booking_count__gt=0)
            .order_by('-revenue_total', '-booking_count')[:8]
        )
        spotlight_rows = []
        for seller in top_sellers:
            seller_revenue_total = seller.revenue_total or Decimal('0.00')
            spotlight_rows.append(
                {
                    'title': _seller_display_name(seller),
                    'meta': f'{seller.booking_count} booking(s)',
                    'value': f'₹{seller_revenue_total:.2f}',
                    'url': f"{reverse('catalog:product_list')}?{urlencode({'seller': seller.id})}",
                }
            )

        recent_bookings = list(
            Booking.objects.select_related('customer', 'seller').order_by('-booked_at')[:8]
        )
        recent_transactions = list(
            Transaction.objects.select_related('booking', 'booking__customer', 'booking__seller')
            .order_by('-created_at')[:8]
        )

        context = {
            'role_title': 'Admin Dashboard',
            'metrics': [
                {
                    'label': 'Total Bookings',
                    'value': Booking.objects.count(),
                    'icon': 'shopping-bag',
                    'tone': 'primary',
                },
                {
                    'label': 'Successful Payments',
                    'value': successful_transactions_qs.count(),
                    'icon': 'credit-card',
                    'tone': 'success',
                },
                {
                    'label': 'Gross Revenue',
                    'value': f'₹{gross_revenue:.2f}',
                    'icon': 'dollar-sign',
                    'tone': 'success',
                },
                {
                    'label': 'Open Complaints',
                    'value': pending_complaints,
                    'icon': 'message-square',
                    'tone': 'warning',
                },
                {
                    'label': 'Active Risk Incidents',
                    'value': active_incidents,
                    'icon': 'shield',
                    'tone': 'danger',
                },
                {
                    'label': 'Refunded Amount',
                    'value': f'₹{refunded_revenue:.2f}',
                    'icon': 'rotate-ccw',
                    'tone': 'warning',
                },
            ],
            'dashboard_status_rows': [
                {
                    'label': 'Pending',
                    'count': Booking.objects.filter(status=Booking.BookingStatus.PENDING).count(),
                    'tone': 'pending',
                },
                {
                    'label': 'Confirmed',
                    'count': Booking.objects.filter(status=Booking.BookingStatus.CONFIRMED).count(),
                    'tone': 'confirmed',
                },
                {
                    'label': 'Shipped',
                    'count': Booking.objects.filter(status=Booking.BookingStatus.SHIPPED).count(),
                    'tone': 'shipped',
                },
                {
                    'label': 'Out for Delivery',
                    'count': Booking.objects.filter(status=Booking.BookingStatus.OUT_FOR_DELIVERY).count(),
                    'tone': 'out',
                },
                {
                    'label': 'Delivered',
                    'count': Booking.objects.filter(status=Booking.BookingStatus.DELIVERED).count(),
                    'tone': 'delivered',
                },
                {
                    'label': 'Cancelled',
                    'count': Booking.objects.filter(status=Booking.BookingStatus.CANCELLED).count(),
                    'tone': 'cancelled',
                },
            ],
            'dashboard_spotlight_title': 'Top Sellers by Revenue',
            'dashboard_spotlight_rows': spotlight_rows,
            'dashboard_recent_bookings': recent_bookings,
            'dashboard_recent_transactions': recent_transactions,
            'dashboard_secondary_title': 'Risk Queue Snapshot',
            'dashboard_secondary_rows': [
                {
                    'title': 'Flagged Sellers',
                    'meta': 'Latest ML verification flags',
                    'value': SellerRiskSnapshot.objects.filter(is_flagged=True).count(),
                    'url': reverse('analytics:verification_results'),
                },
                {
                    'title': 'Incidents Pending Fine',
                    'meta': 'Frozen sellers awaiting fine payment',
                    'value': SellerRiskIncident.objects.filter(
                        is_active=True,
                        status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
                    ).count(),
                    'url': reverse('analytics:risk_incident_queue'),
                },
                {
                    'title': 'Appeals in Review',
                    'meta': 'Appealed incidents awaiting admin decision',
                    'value': SellerRiskIncident.objects.filter(
                        is_active=True,
                        status__in=[
                            SellerRiskIncident.IncidentStatus.APPEALED,
                            SellerRiskIncident.IncidentStatus.UNDER_REVIEW,
                        ],
                    ).count(),
                    'url': reverse('analytics:risk_incident_queue'),
                },
            ],
        }
        return render(request, template_name, context)

    if user.role == User.UserRole.SELLER:
        seller_bookings_qs = Booking.objects.filter(seller=user)
        shipping_delay_cutoff = timezone.now() - timedelta(days=2)
        delayed_shipping_qs = seller_bookings_qs.filter(
            status=Booking.BookingStatus.CONFIRMED,
            booked_at__lt=shipping_delay_cutoff,
        )
        delayed_shipping_count = delayed_shipping_qs.count()
        delayed_shipping_rows = list(
            delayed_shipping_qs.select_related('customer').order_by('booked_at')[:5]
        )
        successful_transactions_qs = Transaction.objects.filter(
            booking__seller=user,
            status=Transaction.TransactionStatus.SUCCESS,
        )
        seller_revenue_total = successful_transactions_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        low_stock_count = Product.objects.filter(
            seller=user,
            is_active=True,
            stock_quantity__lte=5,
        ).count()
        out_of_stock_count = Product.objects.filter(
            seller=user,
            is_active=True,
            stock_quantity__lte=0,
        ).count()
        category_non_listed_product_count = Product.objects.filter(
            seller=user,
            category__is_active=False,
        ).count()
        seller_profile = SellerProfile.objects.filter(user=user).first()
        seller_risk_incident = (
            SellerRiskIncident.objects.filter(seller=user, is_active=True)
            .order_by('-created_at')
            .first()
        )
        if seller_risk_incident is None:
            seller_risk_incident = (
                SellerRiskIncident.objects.filter(seller=user)
                .order_by('-created_at')
                .first()
            )

        seller_account_terminated = bool(
            seller_profile
            and seller_profile.verification_status == SellerProfile.VerificationStatus.REJECTED
        )
        seller_account_frozen = bool(
            seller_profile
            and seller_profile.is_suspended
            and not seller_account_terminated
        )

        seller_risk_popup = None
        if seller_account_terminated or seller_account_frozen:
            show_popup = True
            if seller_risk_incident and seller_risk_incident.seller_acknowledged_at:
                show_popup = False
            if show_popup:
                status_label = 'Terminated' if seller_account_terminated else 'Frozen'
                status_message = (
                    'Your seller account is terminated. Selling operations are permanently blocked until admin reversal.'
                    if seller_account_terminated
                    else 'Your seller account is frozen. Selling operations are blocked until risk review completes.'
                )
                incident_reason = (
                    (seller_risk_incident.incident_reason or '').strip()
                    if seller_risk_incident
                    else (seller_profile.suspension_note if seller_profile else '')
                )
                seller_risk_popup = {
                    'status_label': status_label,
                    'status_tone': 'terminated' if seller_account_terminated else 'frozen',
                    'message': status_message,
                    'incident_id': seller_risk_incident.id if seller_risk_incident else None,
                    'incident_reason': incident_reason,
                    'incident_status': (
                        seller_risk_incident.get_status_display()
                        if seller_risk_incident
                        else ''
                    ),
                }
        seller_unlock_banner = None
        if (
            seller_account_frozen
            and not seller_account_terminated
            and seller_risk_incident
            and seller_risk_incident.is_active
            and seller_risk_incident.fine_amount > 0
            and seller_risk_incident.fine_paid_at is None
        ):
            seller_unlock_banner = {
                'fine_amount': seller_risk_incident.fine_amount,
                'incident_id': seller_risk_incident.id,
                'incident_status': seller_risk_incident.get_status_display(),
            }

        top_products = list(
            Product.objects.filter(seller=user)
            .select_related('category')
            .annotate(
                booking_count=Count(
                    'booking_items__booking',
                    filter=~Q(booking_items__booking__status=Booking.BookingStatus.CANCELLED),
                    distinct=True,
                ),
                sold_units=Sum(
                    'booking_items__quantity',
                    filter=~Q(booking_items__booking__status=Booking.BookingStatus.CANCELLED),
                ),
            )
            .filter(booking_count__gt=0)
            .order_by('-sold_units', '-booking_count')[:8]
        )
        spotlight_rows = []
        for product in top_products:
            sold_units = product.sold_units or 0
            spotlight_rows.append(
                {
                    'title': product.name,
                    'meta': f'{product.booking_count} booking(s) · {product.category.name}',
                    'value': f'{sold_units} units',
                    'url': reverse('catalog:seller_product_edit', args=[product.id]),
                }
            )

        restock_watch_products = list(
            Product.objects.filter(seller=user, is_active=True, stock_quantity__lte=10)
            .select_related('category')
            .order_by('stock_quantity', '-updated_at')[:8]
        )
        attach_restock_predictions(restock_watch_products, reorder_level=5)
        secondary_rows = [
            {
                'title': product.name,
                'meta': (
                    f'Stock {product.stock_quantity} · '
                    f'Predicted stockout {product.predicted_stockout_date:%b %d, %Y}'
                ),
                'value': f'Restock by {product.predicted_restock_date:%b %d, %Y}',
                'url': reverse('catalog:seller_restock_dashboard'),
            }
            for product in restock_watch_products
        ]

        recent_bookings = list(
            seller_bookings_qs.select_related('customer', 'seller').order_by('-booked_at')[:8]
        )
        for booking in recent_bookings:
            booking.is_shipping_delay_warning = bool(
                booking.status == Booking.BookingStatus.CONFIRMED
                and booking.booked_at
                and booking.booked_at < shipping_delay_cutoff
            )
        recent_transactions = list(
            Transaction.objects.select_related('booking', 'booking__customer', 'booking__seller')
            .filter(booking__seller=user)
            .order_by('-created_at')[:8]
        )

        context = {
            'role_title': 'Seller Dashboard',
            'metrics': [
                {
                    'label': 'My Products',
                    'value': Product.objects.filter(seller=user).count(),
                    'icon': 'package',
                    'tone': 'primary',
                },
                {
                    'label': 'My Bookings',
                    'value': seller_bookings_qs.count(),
                    'icon': 'shopping-cart',
                    'tone': 'success',
                },
                {
                    'label': 'Pending Payments',
                    'value': seller_bookings_qs.filter(status=Booking.BookingStatus.PENDING).count(),
                    'icon': 'clock',
                    'tone': 'warning',
                },
                {
                    'label': 'Ready to Ship',
                    'value': seller_bookings_qs.filter(status=Booking.BookingStatus.CONFIRMED).count(),
                    'icon': 'truck',
                    'tone': 'primary',
                },
                {
                    'label': 'Shipping Delays >2d',
                    'value': delayed_shipping_count,
                    'icon': 'alert-triangle',
                    'tone': 'danger',
                },
                {
                    'label': 'Low Stock Alerts',
                    'value': low_stock_count,
                    'icon': 'alert-triangle',
                    'tone': 'warning',
                },
                {
                    'label': 'Gross Sales',
                    'value': f'₹{seller_revenue_total:.2f}',
                    'icon': 'dollar-sign',
                    'tone': 'success',
                },
            ],
            'category_non_listed_product_count': category_non_listed_product_count,
            'dashboard_status_rows': [
                {
                    'label': 'Pending',
                    'count': seller_bookings_qs.filter(status=Booking.BookingStatus.PENDING).count(),
                    'tone': 'pending',
                },
                {
                    'label': 'Confirmed',
                    'count': seller_bookings_qs.filter(status=Booking.BookingStatus.CONFIRMED).count(),
                    'tone': 'confirmed',
                },
                {
                    'label': 'Shipped',
                    'count': seller_bookings_qs.filter(status=Booking.BookingStatus.SHIPPED).count(),
                    'tone': 'shipped',
                },
                {
                    'label': 'Out for Delivery',
                    'count': seller_bookings_qs.filter(status=Booking.BookingStatus.OUT_FOR_DELIVERY).count(),
                    'tone': 'out',
                },
                {
                    'label': 'Delivered',
                    'count': seller_bookings_qs.filter(status=Booking.BookingStatus.DELIVERED).count(),
                    'tone': 'delivered',
                },
                {
                    'label': 'Cancelled',
                    'count': seller_bookings_qs.filter(status=Booking.BookingStatus.CANCELLED).count(),
                    'tone': 'cancelled',
                },
            ],
            'dashboard_spotlight_title': 'Top Products by Demand',
            'dashboard_spotlight_rows': spotlight_rows,
            'dashboard_recent_bookings': recent_bookings,
            'dashboard_recent_transactions': recent_transactions,
            'dashboard_secondary_title': 'Critical Restock Watch',
            'dashboard_secondary_rows': secondary_rows,
            'dashboard_out_of_stock_count': out_of_stock_count,
            'seller_delay_shipping_count': delayed_shipping_count,
            'seller_delay_shipping_rows': delayed_shipping_rows,
            'seller_risk_popup': seller_risk_popup,
            'seller_account_terminated': seller_account_terminated,
            'seller_unlock_banner': seller_unlock_banner,
        }
        return render(request, template_name, context)

    context = _storefront_context(user=user, request=request)
    return render(request, 'accounts/customer_dashboard.html', context)


@login_required
def profile_view(request):
    user = request.user
    stats = []

    if user.role == User.UserRole.ADMIN:
        stats = [
            ('Total Sellers', User.objects.filter(role=User.UserRole.SELLER).count()),
            ('Total Customers', User.objects.filter(role=User.UserRole.CUSTOMER).count()),
            ('Flagged Sellers', SellerRiskSnapshot.objects.filter(is_flagged=True).count()),
        ]
    elif user.role == User.UserRole.SELLER:
        stats = [
            ('Products', Product.objects.filter(seller=user).count()),
            ('Bookings', Booking.objects.filter(seller=user).count()),
            ('Pending', Booking.objects.filter(seller=user, status=Booking.BookingStatus.PENDING).count()),
        ]
    else:
        stats = [
            ('Bookings', Booking.objects.filter(customer=user).count()),
            ('Completed', Booking.objects.filter(customer=user, status=Booking.BookingStatus.DELIVERED).count()),
            ('Complaints', Complaint.objects.filter(customer=user).count()),
            ('Saved Addresses', CustomerAddress.objects.filter(customer=user, is_active=True).count()),
        ]

    return render(
        request,
        'accounts/profile.html',
        {
            'profile_user': user,
            'profile_stats': stats,
        },
    )


@login_required
def profile_update(request):
    if request.method == 'POST':
        form = ProfileUpdateForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully.')
            return redirect('accounts:profile')
    else:
        form = ProfileUpdateForm(instance=request.user)
    return render(request, 'accounts/profile_update.html', {'form': form})


@role_required(User.UserRole.CUSTOMER)
def manage_addresses(request):
    _sync_saved_addresses_from_bookings(request.user)
    addresses = request.user.saved_addresses.all()
    if request.method == 'POST':
        form = CustomerAddressForm(request.POST)
        if form.is_valid():
            address = form.save(commit=False)
            address.customer = request.user
            if not request.user.saved_addresses.filter(is_default=True, is_active=True).exists():
                address.is_default = True
            address.save()
            messages.success(request, 'Address saved successfully.')
            return redirect('accounts:manage_addresses')
    else:
        form = CustomerAddressForm()

    return render(
        request,
        'accounts/manage_addresses.html',
        {
            'form': form,
            'addresses': addresses,
        },
    )


@role_required(User.UserRole.CUSTOMER)
def edit_address(request, address_id):
    address = get_object_or_404(CustomerAddress, id=address_id, customer=request.user)
    if request.method == 'POST':
        form = CustomerAddressForm(request.POST, instance=address)
        if form.is_valid():
            form.save()
            messages.success(request, 'Address updated successfully.')
            return redirect('accounts:manage_addresses')
    else:
        form = CustomerAddressForm(instance=address)

    return render(
        request,
        'accounts/manage_addresses.html',
        {
            'form': form,
            'addresses': request.user.saved_addresses.all(),
            'editing_address': address,
        },
    )


@role_required(User.UserRole.CUSTOMER)
def toggle_address_status(request, address_id):
    if request.method != 'POST':
        return redirect('accounts:manage_addresses')

    address = get_object_or_404(CustomerAddress, id=address_id, customer=request.user)
    address.is_active = request.POST.get('is_active') == 'on'

    if not address.is_active and address.is_default:
        address.is_default = False
        replacement_default = (
            request.user.saved_addresses.filter(is_active=True).exclude(id=address.id).first()
        )
        if replacement_default:
            replacement_default.is_default = True
            replacement_default.save(update_fields=['is_default'])

    address.save(update_fields=['is_active', 'is_default'])
    messages.success(request, 'Address status updated.')
    return redirect('accounts:manage_addresses')


@role_required(User.UserRole.CUSTOMER)
def set_default_address(request, address_id):
    if request.method != 'POST':
        return redirect('accounts:manage_addresses')

    address = get_object_or_404(CustomerAddress, id=address_id, customer=request.user)
    if not address.is_active:
        address.is_active = True
    address.is_default = True
    address.save(update_fields=['is_active', 'is_default'])
    messages.success(request, 'Default address updated.')
    return redirect('accounts:manage_addresses')


@role_required(User.UserRole.CUSTOMER, User.UserRole.SELLER)
def delete_account(request):
    if request.method == 'POST':
        form = AccountDeletionForm(request.POST, user=request.user)
        if form.is_valid():
            user = request.user
            user_label = user.display_name
            logout(request)
            user.delete()
            messages.success(request, f'Account for {user_label} has been deleted.')
            return redirect('home')
    else:
        form = AccountDeletionForm(user=request.user)

    return render(request, 'accounts/account_delete_confirm.html', {'form': form})


@login_required
def promote_to_seller(request):
    if request.user.role != User.UserRole.CUSTOMER:
        messages.info(request, 'Only customers can request seller access.')
        return redirect('accounts:dashboard')

    request.user.role = User.UserRole.SELLER
    request.user.save(update_fields=['role'])
    default_store_name = f'{request.user.display_name} Store'
    SellerProfile.objects.get_or_create(
        user=request.user,
        defaults={'store_name': default_store_name},
    )
    messages.success(request, 'Your account is now a seller account.')
    return redirect('accounts:dashboard')


@role_required(User.UserRole.ADMIN)
def admin_add_seller(request):
    if request.method == 'POST':
        form = SellerSignUpForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Seller account created.')
            return redirect('accounts:admin_add_seller')
    else:
        form = SellerSignUpForm()
    return render(request, 'accounts/admin_add_seller.html', {'form': form})


def _catalog_search_filter(query):
    normalized = ' '.join((query or '').split())
    if not normalized:
        return Q()
    return (
        Q(name__icontains=normalized)
        | Q(description__icontains=normalized)
        | Q(category__name__icontains=normalized)
        | Q(seller__first_name__icontains=normalized)
        | Q(seller__last_name__icontains=normalized)
        | Q(seller__email__icontains=normalized)
        | Q(seller__seller_profile__store_name__icontains=normalized)
    )


def search_results(request):
    raw_query = request.GET.get('q') or ''
    query = ' '.join(raw_query.split())
    query_lower = query.lower()
    selected_seller_id = _parse_positive_int(request.GET.get('seller'))

    base_products = (
        Product.objects.select_related(
            'category',
            'seller',
            'seller__seller_profile',
            'location',
            'location__district',
            'location__district__state',
        )
        .prefetch_related('serviceable_states', 'serviceable_districts', 'serviceable_locations')
        .filter(is_active=True)
        .filter(category__is_active=True)
        .filter(stock_quantity__gt=0)
        .filter(_active_seller_relation_filter('seller'))
        .filter(
            Q(location__isnull=False)
            | Q(serviceable_states__isnull=False)
            | Q(serviceable_districts__isnull=False)
            | Q(serviceable_locations__isnull=False)
        )
        .distinct()
    )

    if query:
        query_matched_products = base_products.filter(_catalog_search_filter(query)).distinct()
    else:
        query_matched_products = base_products.order_by('-updated_at')

    out_of_stock_match_count = 0
    if query:
        out_of_stock_match_count = (
            Product.objects.select_related('category', 'seller')
            .filter(is_active=True, category__is_active=True, stock_quantity__lte=0)
            .filter(_active_seller_relation_filter('seller'))
            .filter(
                Q(location__isnull=False)
                | Q(serviceable_states__isnull=False)
                | Q(serviceable_districts__isnull=False)
                | Q(serviceable_locations__isnull=False)
            )
            .filter(_catalog_search_filter(query))
            .distinct()
            .count()
        )

    seller_counts = dict(
        query_matched_products.values('seller_id').annotate(total=Count('id')).values_list('seller_id', 'total')
    )
    seller_regions = {}
    for seller_id, district_name, state_name in query_matched_products.filter(location__isnull=False).values_list(
        'seller_id',
        'location__district__name',
        'location__district__state__name',
    ).distinct():
        if not seller_id:
            continue
        region_label = ''
        if district_name and state_name:
            region_label = f'{district_name}, {state_name}'
        elif state_name:
            region_label = state_name
        elif district_name:
            region_label = district_name
        if not region_label:
            continue
        bucket = seller_regions.setdefault(seller_id, [])
        if region_label not in bucket:
            bucket.append(region_label)

    seller_users = (
        User.objects.select_related('seller_profile')
        .filter(role=User.UserRole.SELLER, id__in=seller_counts.keys())
        .filter(_active_seller_user_filter())
    )
    seller_results = []
    seller_map = {}
    matched_seller_ids = set(query_matched_products.values_list('seller_id', flat=True))
    for seller_user in sorted(seller_users, key=lambda item: _seller_display_name(item).lower()):
        regions = seller_regions.get(seller_user.id, [])
        region_text = ', '.join(regions[:2]) if regions else 'No base region configured'
        if len(regions) > 2:
            region_text = f'{region_text} +{len(regions) - 2} more'
        item = {
            'id': seller_user.id,
            'name': _seller_display_name(seller_user),
            'product_count': seller_counts.get(seller_user.id, 0),
            'region_text': region_text,
            'url': f"{reverse('accounts:search_results')}?{urlencode({'q': query, 'seller': seller_user.id})}" if query else f"{reverse('accounts:search_results')}?{urlencode({'seller': seller_user.id})}",
        }
        if query and not (
            query_lower in item['name'].lower()
            or query_lower in region_text.lower()
            or seller_user.id in matched_seller_ids
        ):
            continue
        seller_results.append(item)
        seller_map[seller_user.id] = item

    filtered_products = query_matched_products
    if selected_seller_id:
        if selected_seller_id in seller_map:
            filtered_products = filtered_products.filter(seller_id=selected_seller_id)
        else:
            selected_seller_id = None

    if request.GET:
        cart_query = request.GET.copy()
    else:
        cart_query = request.GET.copy()
    cart_query['cart'] = 'open'
    cart_next_url = f'{request.path}?{cart_query.urlencode()}'

    active_seller = seller_map.get(selected_seller_id)
    products = list(filtered_products.order_by('-updated_at')[:40])
    attach_delivery_predictions(products)
    context = {
        'search_query': query,
        'active_seller_id': selected_seller_id,
        'active_seller': active_seller,
        'seller_results': sorted(
            seller_results,
            key=lambda item: (-item['product_count'], item['name'].lower()),
        )[:12],
        'products': products,
        'product_count': filtered_products.count(),
        'cart_next_url': cart_next_url,
        'out_of_stock_match_count': out_of_stock_match_count,
    }
    return render(request, 'accounts/search_results.html', context)


def dashboard_product_preview(request, product_id):
    product = get_object_or_404(
        Product.objects.select_related(
            'category',
            'seller',
            'seller__seller_profile',
            'location',
            'location__district',
            'location__district__state',
        )
        .annotate(
            average_rating=Avg('feedbacks__rating'),
            rating_count=Count('feedbacks', distinct=True),
        )
        .filter(_active_seller_relation_filter('seller'))
        .filter(category__is_active=True),
        id=product_id,
        is_active=True,
        stock_quantity__gt=0,
        seller__is_active=True,
    )

    if product.location and product.location.district_id and product.location.district.state_id:
        region = f'{product.location.district.name}, {product.location.district.state.name}'
    elif product.location and product.location.district_id:
        region = product.location.district.name
    else:
        region = 'Not specified'

    detail_url = reverse('catalog:product_detail', args=[product.id])
    book_url = reverse('orders:create_booking', args=[product.id])
    add_to_cart_url = reverse('catalog:cart_add', args=[product.id])
    base_url = _storefront_base_url_for_request(request)
    login_url = reverse('accounts:login')

    is_customer = request.user.is_authenticated and request.user.role == User.UserRole.CUSTOMER
    delivery_prediction = predict_delivery_for_product(product)
    product_data = {
        'id': product.id,
        'name': product.name,
        'category': product.category.name if product.category_id else '',
        'seller': _seller_display_name(product.seller),
        'region': region,
        'price': f'{product.price:.2f}',
        'stock': product.stock_quantity,
        'weight': str(product.weight) if product.weight is not None else '',
        'size': product.size or '',
        'description': product.description or '',
        'photo_url': product.photo.url if product.photo else '',
        'average_rating': (
            round(float(product.average_rating), 2)
            if product.average_rating is not None
            else None
        ),
        'rating_count': int(product.rating_count or 0),
        'detail_url': detail_url,
        'seller_catalog_url': f"{reverse('catalog:product_list')}?{urlencode({'seller': product.seller_id})}",
        'book_url': book_url,
        'add_to_cart_url': add_to_cart_url,
        'cart_next_url': _build_storefront_url(request, q=request.GET.get('q'), seller=request.GET.get('seller'), category=request.GET.get('category'), cart='open'),
        'is_customer': is_customer,
        'login_book_url': f'{login_url}?next={book_url}',
        'login_add_url': f'{login_url}?next={base_url}',
        'csrf_token': get_token(request),
        'predicted_delivery_days': delivery_prediction.days,
        'predicted_delivery_date': delivery_prediction.expected_date.isoformat(),
        'predicted_delivery_is_fallback': delivery_prediction.is_fallback,
    }

    return JsonResponse({'product': product_data})


def search_suggestions(request):
    raw_query = (request.GET.get('q') or '').strip()
    query = ' '.join(raw_query.split())
    if not query:
        return JsonResponse({'items': []})

    try:
        limit = int(request.GET.get('limit', 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 20))

    query_lower = query.lower()
    suggestions = {}

    def score_term(value, base_score):
        term = (value or '').strip()
        if not term:
            return 0
        term_lower = term.lower()
        if term_lower == query_lower:
            return base_score + 120
        if term_lower.startswith(query_lower):
            return base_score + 90
        if query_lower in term_lower:
            return base_score + 55
        return 0

    def add_candidate(label, category, base_score, url='', item_type='generic'):
        clean_label = ' '.join((label or '').split())
        if not clean_label:
            return
        score = score_term(clean_label, base_score)
        if score <= 0:
            return
        key = f'{category}:{clean_label.lower()}:{url}'
        existing = suggestions.get(key)
        if existing is None or score > existing['score']:
            suggestions[key] = {
                'value': clean_label,
                'label': clean_label,
                'category': category,
                'score': score,
                'url': url,
                'type': item_type,
            }

    storefront_base_url = reverse('accounts:search_results')

    def storefront_url(**params):
        cleaned = {}
        for key, value in params.items():
            if value in (None, ''):
                continue
            cleaned[key] = value
        if not cleaned:
            return storefront_base_url
        return f'{storefront_base_url}?{urlencode(cleaned)}'

    product_matches = (
        Product.objects.select_related('category', 'seller', 'seller__seller_profile')
        .filter(is_active=True)
        .filter(category__is_active=True)
        .filter(stock_quantity__gt=0)
        .filter(_active_seller_relation_filter('seller'))
        .filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(category__name__icontains=query)
            | Q(seller__first_name__icontains=query)
            | Q(seller__last_name__icontains=query)
            | Q(seller__email__icontains=query)
            | Q(seller__seller_profile__store_name__icontains=query)
        )
        .distinct()[:30]
    )
    for product in product_matches:
        add_candidate(
            product.name,
            'Product',
            90,
            url=reverse('catalog:product_detail', args=[product.id]),
            item_type='product',
        )

    seller_matches = (
        User.objects.select_related('seller_profile')
        .filter(role=User.UserRole.SELLER)
        .filter(_active_seller_user_filter())
        .annotate(
            active_product_count=Count(
                'products',
                filter=Q(products__is_active=True, products__category__is_active=True)
                & Q(products__stock_quantity__gt=0)
                & _active_seller_relation_filter('products__seller'),
                distinct=True,
            )
        )
        .filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
            | Q(seller_profile__store_name__icontains=query)
            | (
                Q(
                    products__name__icontains=query,
                    products__is_active=True,
                    products__category__is_active=True,
                    products__stock_quantity__gt=0,
                )
                & _active_seller_relation_filter('products__seller')
            )
            | (
                Q(
                    products__description__icontains=query,
                    products__is_active=True,
                    products__category__is_active=True,
                    products__stock_quantity__gt=0,
                )
                & _active_seller_relation_filter('products__seller')
            )
        )
        .distinct()[:20]
    )
    for seller in seller_matches:
        add_candidate(
            _seller_display_name(seller),
            'Seller',
            85,
            url=storefront_url(seller=seller.id, q=query),
            item_type='seller',
        )

    for category in Category.objects.filter(is_active=True, name__icontains=query).order_by('name')[:12]:
        add_candidate(
            category.name,
            'Category',
            72,
            url=storefront_url(category=category.id, q=query),
            item_type='category',
        )

    if request.user.is_authenticated and request.user.role in {User.UserRole.ADMIN, User.UserRole.SELLER}:
        for value in Complaint.objects.filter(subject__istartswith=query).values_list('subject', flat=True)[:10]:
            add_candidate(value, 'Complaint', 55, item_type='complaint')
        for value in Transaction.objects.filter(transaction_reference__istartswith=query).values_list(
            'transaction_reference',
            flat=True,
        )[:10]:
            add_candidate(value, 'Transaction', 58, item_type='transaction')

    ranked = sorted(
        suggestions.values(),
        key=lambda item: (-item['score'], item['category'], item['value']),
    )[:limit]

    return JsonResponse(
        {
            'items': [
                {
                    'value': item['value'],
                    'label': item['label'],
                    'category': item['category'],
                    'url': item['url'],
                    'type': item['type'],
                }
                for item in ranked
            ]
        }
    )
