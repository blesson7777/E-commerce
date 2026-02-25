from datetime import timedelta

from django.contrib import messages
from django.db.models import Avg
from django.db.models import Count
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import role_required
from accounts.models import User
from analytics.models import RiskRealtimeEvent
from analytics.models import SellerRiskIncident
from analytics.models import SellerRiskSnapshot
from analytics.services import calculate_seller_risk
from analytics.services import score_realtime_event
from catalog.models import Product
from orders.models import Booking
from support.forms import ComplaintActionForm
from support.forms import ComplaintForm
from support.forms import FeedbackForm
from support.models import Complaint
from support.models import Feedback


def _parse_positive_int(raw_value):
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _complaint_seller(complaint):
    if complaint.product_id and complaint.product and complaint.product.seller_id:
        return complaint.product.seller
    if complaint.booking_id and complaint.booking and complaint.booking.seller_id:
        return complaint.booking.seller
    return None


def _resolve_feedback_lock_targets(delivered_bookings, raw_booking, raw_product):
    if not raw_booking.isdigit():
        return None, None

    booking_id = int(raw_booking)
    booking = (
        delivered_bookings.prefetch_related('items__product')
        .filter(id=booking_id)
        .first()
    )
    if booking is None:
        return None, None

    booking_products = {}
    for item in booking.items.all():
        if item.product_id and item.product_id not in booking_products:
            booking_products[item.product_id] = item.product

    if raw_product.isdigit():
        product_id = int(raw_product)
        selected_product = booking_products.get(product_id)
        if selected_product:
            return booking, selected_product

    if len(booking_products) == 1:
        return booking, next(iter(booking_products.values()))

    return None, None


def _process_one_star_feedback_anomaly(feedback):
    seller = None
    if feedback.product_id and feedback.product and feedback.product.seller_id:
        seller = feedback.product.seller
    elif feedback.booking_id and feedback.booking and feedback.booking.seller_id:
        seller = feedback.booking.seller
    if seller is None:
        return None, None

    snapshot = calculate_seller_risk(seller)
    if snapshot is None:
        return None, None

    anomaly_note = (
        f'1-star feedback signal from booking #{feedback.booking_id} '
        f'for product #{feedback.product_id}.'
    )
    risk_factors = list(snapshot.risk_factors or [])
    if anomaly_note not in risk_factors:
        risk_factors.append(anomaly_note)
        snapshot.risk_factors = risk_factors[:12]
        snapshot.save(update_fields=['risk_factors'])

    incident = (
        SellerRiskIncident.objects.filter(seller=seller, is_active=True)
        .order_by('-created_at')
        .first()
    )
    return snapshot, incident


@role_required(User.UserRole.CUSTOMER)
def complaint_create(request):
    if request.method == 'POST':
        form = ComplaintForm(request.POST)
        if form.is_valid():
            complaint = form.save(commit=False)
            complaint.customer = request.user
            complaint.save()
            return redirect('support:complaint_list')
    else:
        form = ComplaintForm()
    return render(request, 'support/complaint_form.html', {'form': form})


@role_required(User.UserRole.ADMIN, User.UserRole.CUSTOMER, User.UserRole.SELLER)
def complaint_list(request):
    complaints_qs = Complaint.objects.select_related(
        'customer',
        'product',
        'product__seller',
        'booking',
        'booking__seller',
        'risk_snapshot',
        'risk_incident',
    )
    if request.user.role == User.UserRole.ADMIN:
        complaints = list(complaints_qs.all())
    elif request.user.role == User.UserRole.SELLER:
        complaints = list(
            complaints_qs.filter(
                Q(product__seller=request.user) | Q(booking__seller=request.user)
            )
        )
    else:
        complaints = list(complaints_qs.filter(customer=request.user))

    for complaint in complaints:
        complaint.target_seller = _complaint_seller(complaint)

    return render(
        request,
        'support/complaint_list.html',
        {
            'complaints': complaints,
            'can_take_action': request.user.role == User.UserRole.ADMIN,
        },
    )


@role_required(User.UserRole.ADMIN, User.UserRole.CUSTOMER, User.UserRole.SELLER)
def complaint_detail(request, complaint_id):
    complaint = get_object_or_404(
        Complaint.objects.select_related(
            'customer',
            'product',
            'product__seller',
            'booking',
            'booking__seller',
            'anomaly_marked_by',
            'risk_snapshot',
            'risk_incident',
        ),
        id=complaint_id,
    )
    target_seller = _complaint_seller(complaint)

    if request.user.role == User.UserRole.CUSTOMER and complaint.customer_id != request.user.id:
        return HttpResponseForbidden('You do not have permission to view this complaint.')
    if request.user.role == User.UserRole.SELLER and (target_seller is None or target_seller.id != request.user.id):
        return HttpResponseForbidden('You do not have permission to view this complaint.')

    if request.method == 'POST':
        if request.user.role != User.UserRole.ADMIN:
            return HttpResponseForbidden('Only admin users can take complaint actions.')

        form = ComplaintActionForm(request.POST, instance=complaint)
        if form.is_valid():
            complaint = form.save(commit=False)
            now = timezone.now()
            if complaint.status in {Complaint.ComplaintStatus.RESOLVED, Complaint.ComplaintStatus.CLOSED}:
                complaint.resolved_at = now
            else:
                complaint.resolved_at = None

            mark_anomaly = bool(form.cleaned_data.get('mark_anomaly'))
            run_ml_check = bool(form.cleaned_data.get('run_ml_check'))
            anomaly_note = (form.cleaned_data.get('anomaly_note') or '').strip()

            complaint.is_anomaly = mark_anomaly
            complaint.anomaly_note = anomaly_note
            if mark_anomaly:
                complaint.anomaly_marked_at = now
                complaint.anomaly_marked_by = request.user
            else:
                complaint.anomaly_marked_at = None
                complaint.anomaly_marked_by = None
                complaint.ml_scored_at = None
                complaint.risk_snapshot = None
                complaint.risk_incident = None

            snapshot = None
            incident = None
            if mark_anomaly and run_ml_check and target_seller is not None:
                payload = {
                    'event_source': 'complaint_manual_review',
                    'complaint_id': complaint.id,
                    'booking_id': complaint.booking_id,
                    'product_id': complaint.product_id,
                    'status': complaint.status,
                    'subject': complaint.subject,
                }
                snapshot, incident, _event = score_realtime_event(
                    seller=target_seller,
                    event_type=RiskRealtimeEvent.EventType.MANUAL_REVIEW,
                    payload=payload,
                    force_freeze=False,
                    incident_note=anomaly_note or f'Complaint anomaly marked (#{complaint.id}).',
                )
                complaint.ml_scored_at = now
                complaint.risk_snapshot = snapshot
                complaint.risk_incident = incident
            elif mark_anomaly and run_ml_check and target_seller is None:
                messages.warning(
                    request,
                    'Complaint anomaly marked, but ML scoring skipped because seller could not be resolved.',
                )

            complaint.save()

            if mark_anomaly and run_ml_check and snapshot is not None:
                messages.success(
                    request,
                    (
                        f'Complaint updated. Anomaly marked and ML scored '
                        f'(risk={snapshot.risk_score:.2f}, label={snapshot.get_classification_label_display()}).'
                    ),
                )
                if incident:
                    messages.warning(
                        request,
                        f'Seller is in risk incident queue (incident #{incident.id}).',
                    )
            elif mark_anomaly:
                messages.success(request, 'Complaint updated and marked as anomaly.')
            else:
                messages.success(request, 'Complaint action updated successfully.')
            return redirect('support:complaint_detail', complaint_id=complaint.id)
    else:
        form = ComplaintActionForm(instance=complaint) if request.user.role == User.UserRole.ADMIN else None

    return render(
        request,
        'support/complaint_detail.html',
        {
            'complaint': complaint,
            'target_seller': target_seller,
            'form': form,
            'can_take_action': request.user.role == User.UserRole.ADMIN,
        },
    )


@role_required(User.UserRole.ADMIN)
def complaint_update_status(request, complaint_id):
    return redirect('support:complaint_detail', complaint_id=complaint_id)


@role_required(User.UserRole.CUSTOMER)
def feedback_create(request):
    delivered_bookings = Booking.objects.filter(
        customer=request.user,
        status=Booking.BookingStatus.DELIVERED,
    )
    if not delivered_bookings.exists():
        messages.info(request, 'Feedback can be submitted only after your order is delivered.')
        return redirect('orders:booking_list')

    raw_booking = (request.GET.get('booking') or '').strip()
    raw_product = (request.GET.get('product') or '').strip()
    locked_booking, locked_product = _resolve_feedback_lock_targets(
        delivered_bookings,
        raw_booking,
        raw_product,
    )

    initial_booking_id = None
    initial_product_id = None
    if locked_booking and locked_product:
        initial_booking_id = locked_booking.id
        initial_product_id = locked_product.id
    elif raw_booking.isdigit():
        booking_id = int(raw_booking)
        if delivered_bookings.filter(id=booking_id).exists():
            initial_booking_id = booking_id

    initial_data = {}
    if initial_booking_id:
        initial_data['booking'] = initial_booking_id

    if initial_product_id:
        initial_data['product'] = initial_product_id
    elif raw_product.isdigit():
        product_id = int(raw_product)
        valid_product_qs = delivered_bookings.filter(items__product_id=product_id)
        if initial_booking_id:
            valid_product_qs = valid_product_qs.filter(id=initial_booking_id)
        if valid_product_qs.exists():
            initial_data['product'] = product_id

    locked_booking_id = locked_booking.id if locked_booking else None
    locked_product_id = locked_product.id if locked_product else None

    if request.method == 'POST':
        form = FeedbackForm(
            request.POST,
            user=request.user,
            initial_booking_id=initial_booking_id,
            locked_booking_id=locked_booking_id,
            locked_product_id=locked_product_id,
        )
        if form.is_valid():
            feedback = form.save(commit=False)
            feedback.customer = request.user
            feedback.save()
            if feedback.rating == 1:
                snapshot, incident = _process_one_star_feedback_anomaly(feedback)
                if (
                    snapshot
                    and snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.HIGH
                    and incident
                ):
                    messages.warning(
                        request,
                        (
                            '1-star review flagged as anomaly signal. '
                            f'Seller moved to risk queue (incident #{incident.id}).'
                        ),
                    )
                else:
                    messages.info(
                        request,
                        '1-star review recorded as anomaly signal for seller verification.',
                    )
            messages.success(request, 'Thanks for your feedback.')
            return redirect('support:feedback_list')
    else:
        form = FeedbackForm(
            user=request.user,
            initial=initial_data,
            initial_booking_id=initial_booking_id,
            locked_booking_id=locked_booking_id,
            locked_product_id=locked_product_id,
        )
    return render(
        request,
        'support/feedback_form.html',
        {
            'form': form,
            'locked_feedback_target': bool(locked_booking and locked_product),
            'locked_booking': locked_booking,
            'locked_product': locked_product,
        },
    )


@role_required(User.UserRole.ADMIN, User.UserRole.CUSTOMER, User.UserRole.SELLER)
def feedback_list(request):
    role = request.user.role
    selected_product_id = _parse_positive_int(request.GET.get('product'))
    selected_product = None
    if selected_product_id:
        selected_product = (
            Product.objects.select_related('seller', 'seller__seller_profile')
            .filter(id=selected_product_id)
            .first()
        )

    if request.user.role == User.UserRole.ADMIN:
        feedbacks = Feedback.objects.select_related(
            'customer',
            'product',
            'product__seller',
            'booking',
        ).all()
    elif request.user.role == User.UserRole.SELLER:
        feedbacks = Feedback.objects.select_related(
            'customer',
            'product',
            'booking',
        ).filter(product__seller=request.user)
    else:
        feedbacks = Feedback.objects.select_related('product', 'product__seller', 'booking')
        if selected_product_id:
            feedbacks = feedbacks.filter(product_id=selected_product_id)
        else:
            feedbacks = feedbacks.filter(customer=request.user)

    if selected_product_id and request.user.role in {User.UserRole.ADMIN, User.UserRole.SELLER}:
        feedbacks = feedbacks.filter(product_id=selected_product_id)

    feedbacks = feedbacks.order_by('-created_at')
    summary = feedbacks.aggregate(
        total=Count('id'),
        avg_rating=Avg('rating'),
        five_star=Count('id', filter=Q(rating=5)),
        one_star=Count('id', filter=Q(rating=1)),
        low_rating=Count('id', filter=Q(rating__lte=2)),
    )
    recent_count = feedbacks.filter(
        created_at__gte=timezone.now() - timedelta(days=7)
    ).count()
    role_heading = {
        User.UserRole.ADMIN: 'Platform Feedback Control',
        User.UserRole.SELLER: 'Seller Feedback Insights',
        User.UserRole.CUSTOMER: 'My Feedback Timeline',
    }.get(role, 'Feedback')
    if selected_product:
        role_heading = f'Reviews: {selected_product.name}'

    context = {
        'feedbacks': feedbacks,
        'feedback_count': summary.get('total') or 0,
        'avg_rating': summary.get('avg_rating') or 0,
        'five_star_count': summary.get('five_star') or 0,
        'one_star_count': summary.get('one_star') or 0,
        'low_rating_count': summary.get('low_rating') or 0,
        'recent_count': recent_count,
        'role_heading': role_heading,
        'selected_product': selected_product,
    }
    return render(request, 'support/feedback_list.html', context)

# Create your views here.
