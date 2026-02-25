from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import SellerProfile
from accounts.models import User
from analytics.models import RiskDriftSnapshot
from analytics.models import RiskModelBacktest
from analytics.models import RiskModelVersion
from analytics.models import RiskPerformanceDrift
from analytics.models import RiskRealtimeEvent
from analytics.models import SellerRiskIncident
from analytics.models import SellerRiskSnapshot
from catalog.models import Product
from orders.models import Booking
from orders.models import Transaction
from support.models import Complaint
from support.models import Feedback


MODEL_NAME = 'seller_fraud'
DEFAULT_THRESHOLD = 0.70
FALSE_FREEZE_COST = 6.0
MISSED_FRAUD_COST = 16.0
FEATURE_DRIFT_ALERT_THRESHOLD = 0.14
PERFORMANCE_DRIFT_ALERT_THRESHOLD = 0.12
MIN_LABELS_FOR_TRAINING = 18
TRAINING_COOLDOWN_HOURS = 8
AUTO_RETRAIN_LABEL_GROWTH = 10

TRAINABLE_FEATURES = [
    'complaint_ratio',
    'failed_transaction_ratio',
    'low_rating_ratio',
    'cancellation_ratio',
    'not_shipped_overdue_ratio',
    'stale_pending_ratio',
    'network_risk_score',
    'sequence_risk_score',
    'booking_volume_30d',
    'cancel_count_30d',
    'failed_payment_count_30d',
    'complaint_count_30d',
    'shared_phone_degree',
    'shared_address_degree',
    'shared_device_degree',
    'shared_ip_degree',
    'shared_payment_handle_degree',
    'cancel_spike_factor',
    'failed_payment_spike_factor',
    'event_booking_created',
    'event_booking_cancelled',
    'event_payment_failed',
    'anomaly_score_hint',
    'risk_velocity_hint',
]

POSITIVE_LABEL_STATUSES = {
    SellerRiskIncident.IncidentStatus.RESOLVED_FROZEN,
    SellerRiskIncident.IncidentStatus.RESOLVED_TERMINATED,
}
NEGATIVE_LABEL_STATUSES = {
    SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN,
}
TRAINABLE_LABEL_STATUSES = POSITIVE_LABEL_STATUSES | NEGATIVE_LABEL_STATUSES


def _clip(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _safe_ratio(numerator, denominator):
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def _sigmoid(value):
    bounded = _clip(value, low=-35.0, high=35.0)
    return 1.0 / (1.0 + math.exp(-bounded))


def _money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _normalize_count(value, scale):
    if scale <= 0:
        return 0.0
    return _clip(float(value) / float(scale))


def _feature_label(name):
    mapping = {
        'complaint_ratio': 'Complaint ratio',
        'failed_transaction_ratio': 'Failed transaction ratio',
        'low_rating_ratio': 'Low-rating ratio',
        'cancellation_ratio': 'Cancellation ratio',
        'not_shipped_overdue_ratio': 'Confirmed but not shipped (>2 days) ratio',
        'stale_pending_ratio': 'Stale pending ratio',
        'network_risk_score': 'Graph/network overlap risk',
        'sequence_risk_score': 'Behavior burst sequence risk',
        'booking_volume_30d': 'Booking volume (30d)',
        'cancel_count_30d': 'Cancellations (30d)',
        'failed_payment_count_30d': 'Failed payments (30d)',
        'complaint_count_30d': 'Complaints (30d)',
        'shared_phone_degree': 'Shared phone graph degree',
        'shared_address_degree': 'Shared address graph degree',
        'shared_device_degree': 'Shared device graph degree',
        'shared_ip_degree': 'Shared IP graph degree',
        'shared_payment_handle_degree': 'Shared payment-handle graph degree',
        'cancel_spike_factor': 'Cancellation spike factor',
        'failed_payment_spike_factor': 'Failed-payment spike factor',
        'event_booking_created': 'Booking-created event signal',
        'event_booking_cancelled': 'Booking-cancelled event signal',
        'event_payment_failed': 'Payment-failed event signal',
        'anomaly_score_hint': 'Rule anomaly signal',
        'risk_velocity_hint': 'Risk velocity signal',
    }
    return mapping.get(name, name.replace('_', ' ').title())


def _incident_label(status):
    if status in POSITIVE_LABEL_STATUSES:
        return 1
    if status in NEGATIVE_LABEL_STATUSES:
        return 0
    return None


def _latest_snapshot_for_seller(seller):
    return SellerRiskSnapshot.objects.filter(seller=seller).order_by('-created_at').first()


def _latest_active_incident(seller):
    return (
        SellerRiskIncident.objects.filter(seller=seller, is_active=True)
        .order_by('-created_at')
        .first()
    )


def _latest_manual_unfreeze_decision_time(seller):
    return (
        SellerRiskIncident.objects.filter(
            seller=seller,
            status=SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN,
            final_decision_at__isnull=False,
        )
        .order_by('-final_decision_at')
        .values_list('final_decision_at', flat=True)
        .first()
    )


def _latest_risk_signal_time(seller):
    timestamps = []

    booking_signal = (
        Booking.objects.filter(seller=seller)
        .order_by('-cancelled_at', '-booked_at')
        .values_list('cancelled_at', 'booked_at')
        .first()
    )
    if booking_signal:
        timestamps.extend([value for value in booking_signal if value is not None])

    tx_signal = (
        Transaction.objects.filter(booking__seller=seller)
        .order_by('-created_at', '-paid_at')
        .values_list('created_at', 'paid_at')
        .first()
    )
    if tx_signal:
        timestamps.extend([value for value in tx_signal if value is not None])

    complaint_signal = (
        Complaint.objects.filter(Q(booking__seller=seller) | Q(product__seller=seller))
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    if complaint_signal:
        timestamps.append(complaint_signal)

    low_rating_signal = (
        Feedback.objects.filter(Q(booking__seller=seller) | Q(product__seller=seller), rating__lte=2)
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    if low_rating_signal:
        timestamps.append(low_rating_signal)

    realtime_signal = (
        RiskRealtimeEvent.objects.filter(seller=seller)
        .exclude(event_type=RiskRealtimeEvent.EventType.MANUAL_REVIEW)
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    if realtime_signal:
        timestamps.append(realtime_signal)

    if not timestamps:
        return None
    return max(timestamps)


def _manual_unfreeze_guard_blocks_auto_freeze(seller):
    unfreeze_time = _latest_manual_unfreeze_decision_time(seller)
    if not unfreeze_time:
        return False
    latest_signal_time = _latest_risk_signal_time(seller)
    if latest_signal_time is None:
        return True
    return latest_signal_time <= unfreeze_time


def _risk_model_queryset():
    return RiskModelVersion.objects.filter(model_name=MODEL_NAME).order_by('-updated_at')


def _compute_confusion(labels, probabilities, threshold):
    tp = fp = tn = fn = 0
    for label, prob in zip(labels, probabilities):
        prediction = 1 if prob >= threshold else 0
        if prediction == 1 and label == 1:
            tp += 1
        elif prediction == 1 and label == 0:
            fp += 1
        elif prediction == 0 and label == 0:
            tn += 1
        else:
            fn += 1

    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    if precision + recall == 0:
        f1_score = 0.0
    else:
        f1_score = (2 * precision * recall) / (precision + recall)

    return {
        'tp': tp,
        'fp': fp,
        'tn': tn,
        'fn': fn,
        'precision': precision,
        'recall': recall,
        'f1': f1_score,
    }


def _compute_auc(labels, probabilities):
    positives = [(label, prob) for label, prob in zip(labels, probabilities) if label == 1]
    negatives = [(label, prob) for label, prob in zip(labels, probabilities) if label == 0]
    if not positives or not negatives:
        return 0.0

    wins = 0.0
    ties = 0.0
    for _label_pos, pos_prob in positives:
        for _label_neg, neg_prob in negatives:
            if pos_prob > neg_prob:
                wins += 1.0
            elif pos_prob == neg_prob:
                ties += 1.0
    total_pairs = len(positives) * len(negatives)
    return (wins + (0.5 * ties)) / total_pairs if total_pairs else 0.0


def _optimize_threshold(labels, probabilities):
    if not labels:
        return DEFAULT_THRESHOLD, {
            'tp': 0,
            'fp': 0,
            'tn': 0,
            'fn': 0,
            'precision': 0.0,
            'recall': 0.0,
            'f1': 0.0,
            'business_cost': 0.0,
            'threshold': DEFAULT_THRESHOLD,
            'auc': 0.0,
        }

    best_threshold = DEFAULT_THRESHOLD
    best_cost = None
    best_metrics = None
    for step in range(20, 96):
        threshold = step / 100.0
        metrics = _compute_confusion(labels, probabilities, threshold)
        business_cost = (FALSE_FREEZE_COST * metrics['fp']) + (MISSED_FRAUD_COST * metrics['fn'])
        if best_cost is None or business_cost < best_cost:
            best_cost = business_cost
            best_threshold = threshold
            best_metrics = metrics

    if best_metrics is None:
        best_metrics = _compute_confusion(labels, probabilities, best_threshold)
        best_cost = (FALSE_FREEZE_COST * best_metrics['fp']) + (MISSED_FRAUD_COST * best_metrics['fn'])

    best_metrics['business_cost'] = float(best_cost)
    best_metrics['threshold'] = best_threshold
    best_metrics['auc'] = _compute_auc(labels, probabilities)
    return best_threshold, best_metrics


def _build_calibration_bins(probabilities, labels, bin_count=8):
    if not probabilities or len(probabilities) != len(labels):
        return []
    pairs = sorted(zip(probabilities, labels), key=lambda pair: pair[0])
    chunk_size = max(1, len(pairs) // bin_count)
    bins = []
    for start in range(0, len(pairs), chunk_size):
        chunk = pairs[start : start + chunk_size]
        if not chunk:
            continue
        probs = [pair[0] for pair in chunk]
        outcomes = [pair[1] for pair in chunk]
        bins.append(
            {
                'min': float(min(probs)),
                'max': float(max(probs)),
                'calibrated': float(sum(outcomes) / len(outcomes)),
                'count': len(chunk),
            }
        )
    if bins:
        bins[-1]['max'] = 1.0
    return bins


def _apply_calibration(probability, bins):
    if not bins:
        return _clip(probability)
    value = _clip(probability)
    for item in bins:
        if value <= float(item.get('max', 1.0)):
            return _clip(float(item.get('calibrated', value)))
    return value


def _train_logistic(feature_rows, labels, feature_names, iterations=420, learning_rate=0.3, l2_penalty=0.01):
    if not feature_rows or not labels:
        return {'bias': 0.0, 'weights': {feature: 0.0 for feature in feature_names}}

    weights = {feature: 0.0 for feature in feature_names}
    bias = 0.0
    sample_size = float(len(labels))

    for _ in range(iterations):
        grad_bias = 0.0
        grad_weights = {feature: 0.0 for feature in feature_names}

        for row, label in zip(feature_rows, labels):
            linear = bias + sum(float(weights[name]) * float(row.get(name, 0.0)) for name in feature_names)
            prediction = _sigmoid(linear)
            error = prediction - label
            grad_bias += error
            for name in feature_names:
                grad_weights[name] += error * float(row.get(name, 0.0))

        bias -= learning_rate * (grad_bias / sample_size)
        for name in feature_names:
            regularized_gradient = (grad_weights[name] / sample_size) + (l2_penalty * weights[name])
            weights[name] -= learning_rate * regularized_gradient

    return {'bias': float(bias), 'weights': {name: float(value) for name, value in weights.items()}}


def _predict_probability(feature_vector, model):
    weights = model.feature_weights or {}
    bias = float(model.bias or 0.0)
    linear = bias
    for name, weight in weights.items():
        linear += float(weight) * float(feature_vector.get(name, 0.0))
    raw_probability = _sigmoid(linear)
    calibrated_probability = _apply_calibration(raw_probability, model.calibration_bins or [])
    return raw_probability, calibrated_probability


def _rule_based_probability(feature_vector):
    score = 0.0
    score += 0.26 * feature_vector.get('complaint_ratio', 0.0)
    score += 0.28 * feature_vector.get('failed_transaction_ratio', 0.0)
    score += 0.18 * feature_vector.get('low_rating_ratio', 0.0)
    score += 0.18 * feature_vector.get('cancellation_ratio', 0.0)
    score += 0.16 * feature_vector.get('not_shipped_overdue_ratio', 0.0)
    score += 0.10 * feature_vector.get('stale_pending_ratio', 0.0)
    score += 0.22 * feature_vector.get('network_risk_score', 0.0)
    score += 0.24 * feature_vector.get('sequence_risk_score', 0.0)
    score += 0.18 * feature_vector.get('event_booking_cancelled', 0.0)
    score += 0.22 * feature_vector.get('event_payment_failed', 0.0)
    return _clip(score)


def _collect_graph_features(seller):
    phone_degree = 0
    if seller.phone_number:
        phone_degree = (
            User.objects.filter(phone_number=seller.phone_number)
            .exclude(id=seller.id)
            .count()
        )

    seller_addresses = list(
        Booking.objects.filter(seller=seller)
        .exclude(shipping_address='')
        .values_list('shipping_address', flat=True)
        .distinct()[:35]
    )
    address_degree = 0
    if seller_addresses:
        address_degree = (
            Booking.objects.filter(shipping_address__in=seller_addresses)
            .exclude(seller=seller)
            .values('seller_id')
            .distinct()
            .count()
        )

    seller_events = RiskRealtimeEvent.objects.filter(seller=seller).order_by('-created_at')[:280]
    device_values = set()
    ip_values = set()
    payment_values = set()
    for event in seller_events:
        payload = event.payload or {}
        device_value = (payload.get('device_fingerprint') or '').strip().lower()
        ip_value = (payload.get('ip_address') or '').strip().lower()
        payment_value = (payload.get('payment_handle') or '').strip().lower()
        if device_value:
            device_values.add(device_value)
        if ip_value:
            ip_values.add(ip_value)
        if payment_value:
            payment_values.add(payment_value)

    shared_device_sellers = set()
    shared_ip_sellers = set()
    shared_payment_sellers = set()
    if device_values or ip_values or payment_values:
        for event in RiskRealtimeEvent.objects.exclude(seller=seller).only('seller_id', 'payload'):
            payload = event.payload or {}
            device_value = (payload.get('device_fingerprint') or '').strip().lower()
            ip_value = (payload.get('ip_address') or '').strip().lower()
            payment_value = (payload.get('payment_handle') or '').strip().lower()
            if device_value and device_value in device_values:
                shared_device_sellers.add(event.seller_id)
            if ip_value and ip_value in ip_values:
                shared_ip_sellers.add(event.seller_id)
            if payment_value and payment_value in payment_values:
                shared_payment_sellers.add(event.seller_id)

    device_degree = len(shared_device_sellers)
    ip_degree = len(shared_ip_sellers)
    payment_degree = len(shared_payment_sellers)
    network_score = (
        (min(phone_degree, 5) * 0.08)
        + (min(address_degree, 5) * 0.10)
        + (min(device_degree, 5) * 0.12)
        + (min(ip_degree, 5) * 0.11)
        + (min(payment_degree, 5) * 0.09)
    )
    network_score = _clip(network_score)
    return {
        'shared_phone_degree': _normalize_count(phone_degree, 5),
        'shared_address_degree': _normalize_count(address_degree, 5),
        'shared_device_degree': _normalize_count(device_degree, 5),
        'shared_ip_degree': _normalize_count(ip_degree, 5),
        'shared_payment_handle_degree': _normalize_count(payment_degree, 5),
        'network_risk_score': network_score,
    }


def _collect_sequence_features(seller, now):
    day_window = now - timedelta(hours=24)
    week_window = now - timedelta(days=7)
    month_window = now - timedelta(days=30)

    seller_cancellations_24h = Booking.objects.filter(
        seller=seller,
        status=Booking.BookingStatus.CANCELLED,
        cancelled_by_role=User.UserRole.SELLER,
        cancelled_at__gte=day_window,
    ).count()
    seller_cancellations_7d = Booking.objects.filter(
        seller=seller,
        status=Booking.BookingStatus.CANCELLED,
        cancelled_by_role=User.UserRole.SELLER,
        cancelled_at__gte=week_window,
    ).count()
    seller_cancellations_30d = Booking.objects.filter(
        seller=seller,
        status=Booking.BookingStatus.CANCELLED,
        cancelled_by_role=User.UserRole.SELLER,
        cancelled_at__gte=month_window,
    ).count()

    failed_payments_24h = Transaction.objects.filter(
        booking__seller=seller,
        status=Transaction.TransactionStatus.FAILED,
        created_at__gte=day_window,
    ).count()
    failed_payments_7d = Transaction.objects.filter(
        booking__seller=seller,
        status=Transaction.TransactionStatus.FAILED,
        created_at__gte=week_window,
    ).count()
    failed_payments_30d = Transaction.objects.filter(
        booking__seller=seller,
        status=Transaction.TransactionStatus.FAILED,
        created_at__gte=month_window,
    ).count()

    cancel_spike = _safe_ratio((seller_cancellations_24h * 7.0), (seller_cancellations_7d + 1.0))
    fail_spike = _safe_ratio((failed_payments_24h * 7.0), (failed_payments_7d + 1.0))
    spike_signal = max(0.0, cancel_spike - 1.0) + max(0.0, fail_spike - 1.0)
    burst_events = seller_cancellations_24h + failed_payments_24h

    sequence_risk_score = _clip((0.30 * max(0.0, cancel_spike - 1.0)) + (0.40 * max(0.0, fail_spike - 1.0)) + (0.04 * burst_events))

    return {
        'cancel_count_30d': _normalize_count(seller_cancellations_30d, 20),
        'failed_payment_count_30d': _normalize_count(failed_payments_30d, 20),
        'cancel_spike_factor': _clip(cancel_spike / 3.0),
        'failed_payment_spike_factor': _clip(fail_spike / 3.0),
        'sequence_risk_score': sequence_risk_score,
        'burst_signal': spike_signal,
    }


def _collect_primary_features(seller, event_type=None):
    now = timezone.now()
    month_window = now - timedelta(days=30)
    stale_window = now - timedelta(days=3)
    not_shipped_window = now - timedelta(days=2)

    booking_qs = Booking.objects.filter(seller=seller)
    total_bookings = booking_qs.count()
    booking_30d = booking_qs.filter(booked_at__gte=month_window).count()
    cancelled_by_seller = booking_qs.filter(
        status=Booking.BookingStatus.CANCELLED,
        cancelled_by_role=User.UserRole.SELLER,
    ).count()
    stale_pending = booking_qs.filter(
        status=Booking.BookingStatus.PENDING,
        booked_at__lt=stale_window,
    ).count()
    not_shipped_overdue = booking_qs.filter(
        status=Booking.BookingStatus.CONFIRMED,
        booked_at__lt=not_shipped_window,
    ).count()

    tx_qs = Transaction.objects.filter(booking__seller=seller)
    tx_total = tx_qs.count()
    failed_tx = tx_qs.filter(status=Transaction.TransactionStatus.FAILED).count()

    complaint_qs = Complaint.objects.filter(Q(booking__seller=seller) | Q(product__seller=seller)).distinct()
    complaint_total = complaint_qs.count()
    complaint_30d = complaint_qs.filter(created_at__gte=month_window).count()

    feedback_qs = Feedback.objects.filter(Q(booking__seller=seller) | Q(product__seller=seller)).distinct()
    feedback_total = feedback_qs.count()
    low_rating_total = feedback_qs.filter(rating__lte=2).count()

    primary = {
        'complaint_ratio': _clip(_safe_ratio(complaint_total, max(total_bookings, 1))),
        'failed_transaction_ratio': _clip(_safe_ratio(failed_tx, max(tx_total, 1))),
        'low_rating_ratio': _clip(_safe_ratio(low_rating_total, max(feedback_total, 1))),
        'cancellation_ratio': _clip(_safe_ratio(cancelled_by_seller, max(total_bookings, 1))),
        'not_shipped_overdue_ratio': _clip(_safe_ratio(not_shipped_overdue, max(total_bookings, 1))),
        'stale_pending_ratio': _clip(_safe_ratio(stale_pending + not_shipped_overdue, max(total_bookings, 1))),
        'booking_volume_30d': _normalize_count(booking_30d, 45),
        'complaint_count_30d': _normalize_count(complaint_30d, 20),
        'event_booking_created': 1.0 if event_type == RiskRealtimeEvent.EventType.BOOKING_CREATED else 0.0,
        'event_booking_cancelled': 1.0 if event_type == RiskRealtimeEvent.EventType.BOOKING_CANCELLED else 0.0,
        'event_payment_failed': 1.0 if event_type == RiskRealtimeEvent.EventType.PAYMENT_FAILED else 0.0,
    }

    primary.update(_collect_graph_features(seller))
    primary.update(_collect_sequence_features(seller=seller, now=now))

    anomaly_hint = (
        (0.24 * primary['complaint_ratio'])
        + (0.28 * primary['failed_transaction_ratio'])
        + (0.20 * primary['low_rating_ratio'])
        + (0.20 * primary['cancellation_ratio'])
        + (0.18 * primary['not_shipped_overdue_ratio'])
        + (0.14 * primary['stale_pending_ratio'])
        + (0.20 * primary['network_risk_score'])
        + (0.22 * primary['sequence_risk_score'])
        + (0.18 * primary['event_booking_cancelled'])
        + (0.24 * primary['event_payment_failed'])
    )
    primary['anomaly_score_hint'] = _clip(anomaly_hint)
    return primary


def _build_feature_vector(seller, event_type=None):
    feature_vector = _collect_primary_features(seller=seller, event_type=event_type)
    previous_snapshot = _latest_snapshot_for_seller(seller)
    previous_probability = previous_snapshot.calibrated_probability if previous_snapshot else 0.0
    current_hint = feature_vector.get('anomaly_score_hint', 0.0)
    feature_vector['risk_velocity_hint'] = _clip((current_hint - previous_probability) + 0.5) - 0.5
    return feature_vector


def _top_contributors(feature_vector, weights):
    contributors = []
    for feature_name, weight in (weights or {}).items():
        value = float(feature_vector.get(feature_name, 0.0))
        impact = float(weight) * value
        if abs(impact) < 0.002:
            continue
        contributors.append(
            {
                'feature': feature_name,
                'label': _feature_label(feature_name),
                'value': round(value, 4),
                'impact': round(impact * 100.0, 2),
                'direction': 'increase' if impact >= 0 else 'decrease',
            }
        )
    contributors.sort(key=lambda item: abs(item['impact']), reverse=True)
    return contributors[:6]


def _heuristic_contributors(feature_vector):
    weighted = {
        'complaint_ratio': feature_vector.get('complaint_ratio', 0.0) * 22,
        'failed_transaction_ratio': feature_vector.get('failed_transaction_ratio', 0.0) * 26,
        'low_rating_ratio': feature_vector.get('low_rating_ratio', 0.0) * 15,
        'cancellation_ratio': feature_vector.get('cancellation_ratio', 0.0) * 16,
        'not_shipped_overdue_ratio': feature_vector.get('not_shipped_overdue_ratio', 0.0) * 18,
        'network_risk_score': feature_vector.get('network_risk_score', 0.0) * 18,
        'sequence_risk_score': feature_vector.get('sequence_risk_score', 0.0) * 20,
    }
    rows = []
    for feature_name, impact in weighted.items():
        if abs(impact) < 0.6:
            continue
        rows.append(
            {
                'feature': feature_name,
                'label': _feature_label(feature_name),
                'value': round(float(feature_vector.get(feature_name, 0.0)), 4),
                'impact': round(float(impact), 2),
                'direction': 'increase' if impact >= 0 else 'decrease',
            }
        )
    rows.sort(key=lambda item: abs(item['impact']), reverse=True)
    return rows[:6]


def _build_risk_factors(feature_vector, contributors, extra_note=''):
    positive_contributors = [item for item in contributors if float(item.get('impact', 0.0)) > 0]
    reasons = []
    for item in positive_contributors[:4]:
        reasons.append(f"{item.get('label')} contributed +{abs(float(item.get('impact', 0.0))):.1f}")

    if feature_vector.get('event_payment_failed', 0.0) >= 1.0:
        reasons.append('Real-time failed payment event increased seller risk score.')
    if feature_vector.get('event_booking_cancelled', 0.0) >= 1.0:
        reasons.append('Real-time cancellation event increased seller risk score.')
    if feature_vector.get('not_shipped_overdue_ratio', 0.0) >= 0.15:
        reasons.append('Seller has confirmed bookings older than 2 days without shipment.')
    if feature_vector.get('network_risk_score', 0.0) >= 0.45:
        reasons.append('High graph overlap across phone/address/device/IP/payment handle.')
    if feature_vector.get('sequence_risk_score', 0.0) >= 0.40:
        reasons.append('Seller behavior spike detected for cancellation/payment-failure sequence.')
    if extra_note:
        reasons.append(extra_note)

    deduped = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped[:6]


def _classification_from_probability(probability, threshold):
    high_cutoff = threshold
    medium_cutoff = max(0.42, threshold - 0.22)
    if probability >= high_cutoff:
        return SellerRiskSnapshot.ClassificationLabel.HIGH
    if probability >= medium_cutoff:
        return SellerRiskSnapshot.ClassificationLabel.MEDIUM
    return SellerRiskSnapshot.ClassificationLabel.LOW


def _confidence_score(probability, threshold, signal_strength):
    distance = abs(probability - threshold)
    confidence = (distance * 130.0) + min(45.0, signal_strength * 60.0)
    return round(_clip(confidence / 100.0, 0.0, 1.0) * 100.0, 2)


def _calculate_incident_fine(risk_score):
    base = Decimal('120.00')
    variable = Decimal(str(max(0.0, risk_score - 55.0))) * Decimal('6.75')
    return _money(base + variable)


def _latest_drift_score(model):
    if not model:
        return 0.0
    snapshot = (
        RiskDriftSnapshot.objects.filter(model=model)
        .order_by('-created_at')
        .values_list('overall_drift_score', flat=True)
        .first()
    )
    return float(snapshot or 0.0)


def _as_trainable_row(feature_vector):
    return {feature_name: float(feature_vector.get(feature_name, 0.0)) for feature_name in TRAINABLE_FEATURES}


def _resolved_incident_samples(limit=2400):
    incidents = (
        SellerRiskIncident.objects.select_related('snapshot')
        .filter(status__in=TRAINABLE_LABEL_STATUSES, snapshot__isnull=False)
        .order_by('-final_decision_at', '-updated_at')[:limit]
    )
    rows = []
    labels = []
    times = []
    for incident in incidents:
        label = _incident_label(incident.status)
        if label is None:
            continue
        feature_vector = (incident.snapshot.feature_vector or {}) if incident.snapshot_id else {}
        if not feature_vector:
            continue
        rows.append(_as_trainable_row(feature_vector))
        labels.append(label)
        times.append(incident.final_decision_at or incident.updated_at or incident.created_at)
    return rows, labels, times


def _split_train_validation(rows, labels, times):
    if not rows or not labels or len(rows) != len(labels):
        return [], [], [], []
    indexed = sorted(
        range(len(rows)),
        key=lambda idx: times[idx] if idx < len(times) and times[idx] is not None else timezone.now(),
    )
    ordered_rows = [rows[idx] for idx in indexed]
    ordered_labels = [labels[idx] for idx in indexed]
    split_index = int(len(ordered_rows) * 0.8)
    split_index = max(1, min(split_index, len(ordered_rows) - 1)) if len(ordered_rows) > 1 else 1
    train_rows = ordered_rows[:split_index]
    train_labels = ordered_labels[:split_index]
    val_rows = ordered_rows[split_index:]
    val_labels = ordered_labels[split_index:]
    if not val_rows:
        val_rows = train_rows
        val_labels = train_labels
    return train_rows, train_labels, val_rows, val_labels


def _feature_mean_baseline(rows):
    if not rows:
        return {}
    baseline = {}
    for feature_name in TRAINABLE_FEATURES:
        baseline[feature_name] = float(mean(row.get(feature_name, 0.0) for row in rows))
    return baseline


def _bootstrap_model():
    existing = _risk_model_queryset().filter(is_active=True).first()
    if existing:
        return existing

    now = timezone.now()
    version = f'hybrid_v2_bootstrap_{now:%Y%m%d%H%M%S}'
    weights = {
        'complaint_ratio': 2.4,
        'failed_transaction_ratio': 2.7,
        'low_rating_ratio': 1.8,
        'cancellation_ratio': 2.2,
        'stale_pending_ratio': 1.1,
        'network_risk_score': 2.0,
        'sequence_risk_score': 2.1,
        'event_booking_cancelled': 1.6,
        'event_payment_failed': 1.9,
        'anomaly_score_hint': 1.5,
        'risk_velocity_hint': 0.9,
    }
    return RiskModelVersion.objects.create(
        model_name=MODEL_NAME,
        version=version,
        algorithm='hybrid_logistic_bootstrap',
        stage=RiskModelVersion.ModelStage.PRODUCTION,
        is_active=True,
        bias=-1.65,
        decision_threshold=DEFAULT_THRESHOLD,
        feature_weights=weights,
        calibration_bins=[],
        drift_baseline={feature: 0.0 for feature in TRAINABLE_FEATURES},
        quality_metrics={
            'bootstrap': True,
            'precision_score': 0.0,
            'recall_score': 0.0,
            'f1_score': 0.0,
            'auc_score': 0.0,
            'business_cost': 0.0,
            'false_freeze_cost': FALSE_FREEZE_COST,
            'missed_fraud_cost': MISSED_FRAUD_COST,
        },
        trained_at=now,
    )


def get_active_risk_model(create_if_missing=True):
    model = _risk_model_queryset().filter(is_active=True).first()
    if model:
        return model
    if not create_if_missing:
        return None
    return _bootstrap_model()


def _model_predict_rows(model, rows):
    raw_probs = []
    calibrated_probs = []
    for row in rows:
        raw_prob, calibrated_prob = _predict_probability(row, model)
        raw_probs.append(raw_prob)
        calibrated_probs.append(calibrated_prob)
    return raw_probs, calibrated_probs


def _promote_candidate_model(candidate, candidate_metrics):
    current_active = get_active_risk_model(create_if_missing=False)
    should_promote = current_active is None

    if current_active is not None:
        current_f1 = float((current_active.quality_metrics or {}).get('f1_score', 0.0))
        current_cost = float((current_active.quality_metrics or {}).get('business_cost', float('inf')))
        candidate_f1 = float(candidate_metrics.get('f1', 0.0))
        candidate_cost = float(candidate_metrics.get('business_cost', float('inf')))
        if candidate_f1 >= max(0.35, current_f1 - 0.05) and candidate_cost <= (current_cost * 1.05):
            should_promote = True

    if should_promote:
        RiskModelVersion.objects.filter(model_name=MODEL_NAME, is_active=True).exclude(id=candidate.id).update(
            is_active=False,
            stage=RiskModelVersion.ModelStage.ARCHIVED,
        )
        candidate.stage = RiskModelVersion.ModelStage.PRODUCTION
        candidate.is_active = True
    else:
        candidate.stage = RiskModelVersion.ModelStage.STAGING
        candidate.is_active = False
    candidate.save(update_fields=['stage', 'is_active', 'updated_at'])
    return should_promote


def train_supervised_risk_model(force=False, reason='manual'):
    active_model = get_active_risk_model(create_if_missing=True)
    now = timezone.now()
    if (
        not force
        and active_model
        and active_model.trained_at
        and (now - active_model.trained_at) < timedelta(hours=TRAINING_COOLDOWN_HOURS)
    ):
        return active_model

    rows, labels, times = _resolved_incident_samples()
    if len(rows) < MIN_LABELS_FOR_TRAINING and not force:
        return active_model
    if not rows:
        return active_model

    train_rows, train_labels, val_rows, val_labels = _split_train_validation(rows, labels, times)
    learned = _train_logistic(
        feature_rows=train_rows,
        labels=train_labels,
        feature_names=TRAINABLE_FEATURES,
    )
    provisional_model = RiskModelVersion(
        model_name=MODEL_NAME,
        version='temporary',
        algorithm='supervised_logistic_v1',
        stage=RiskModelVersion.ModelStage.DRAFT,
        is_active=False,
        bias=learned['bias'],
        decision_threshold=DEFAULT_THRESHOLD,
        feature_weights=learned['weights'],
        calibration_bins=[],
    )
    val_raw, _ = _model_predict_rows(provisional_model, val_rows)
    calibration_bins = _build_calibration_bins(val_raw, val_labels)
    provisional_model.calibration_bins = calibration_bins
    _raw_after_calibration, val_calibrated = _model_predict_rows(provisional_model, val_rows)
    threshold, metrics = _optimize_threshold(val_labels, val_calibrated)

    version = f'hybrid_v3_{now:%Y%m%d%H%M%S}'
    candidate = RiskModelVersion.objects.create(
        model_name=MODEL_NAME,
        version=version,
        algorithm='supervised_logistic_v1',
        stage=RiskModelVersion.ModelStage.DRAFT,
        is_active=False,
        bias=learned['bias'],
        decision_threshold=threshold,
        feature_weights=learned['weights'],
        calibration_bins=calibration_bins,
        drift_baseline=_feature_mean_baseline(train_rows),
        quality_metrics={
            'precision_score': metrics['precision'],
            'recall_score': metrics['recall'],
            'f1_score': metrics['f1'],
            'auc_score': metrics['auc'],
            'business_cost': metrics['business_cost'],
            'false_freeze_cost': FALSE_FREEZE_COST,
            'missed_fraud_cost': MISSED_FRAUD_COST,
            'train_reason': reason,
        },
        training_window_start=min(times) if times else None,
        training_window_end=max(times) if times else None,
        training_samples=len(train_rows),
        positive_samples=sum(train_labels),
        trained_at=now,
    )
    RiskModelBacktest.objects.create(
        model=candidate,
        sample_size=len(val_labels),
        precision_score=metrics['precision'],
        recall_score=metrics['recall'],
        f1_score=metrics['f1'],
        auc_score=metrics['auc'],
        confusion_matrix={
            'tp': metrics['tp'],
            'fp': metrics['fp'],
            'tn': metrics['tn'],
            'fn': metrics['fn'],
            'threshold': threshold,
            'business_cost': metrics['business_cost'],
            'false_freeze_cost': FALSE_FREEZE_COST,
            'missed_fraud_cost': MISSED_FRAUD_COST,
        },
        notes=f'Offline backtest completed before promote. Train reason: {reason}.',
    )
    _promote_candidate_model(candidate, metrics)
    return get_active_risk_model(create_if_missing=True)


def rollback_risk_model(version=None):
    if version:
        target = _risk_model_queryset().filter(version=version).first()
    else:
        target = (
            _risk_model_queryset()
            .filter(stage__in=[RiskModelVersion.ModelStage.STAGING, RiskModelVersion.ModelStage.ARCHIVED])
            .first()
        )
    if target is None:
        return None

    with transaction.atomic():
        RiskModelVersion.objects.filter(model_name=MODEL_NAME, is_active=True).exclude(id=target.id).update(
            is_active=False,
            stage=RiskModelVersion.ModelStage.ARCHIVED,
        )
        target.stage = RiskModelVersion.ModelStage.PRODUCTION
        target.is_active = True
        target.save(update_fields=['stage', 'is_active', 'updated_at'])
    return target


def detect_feature_drift(model=None, sample_size=220):
    model = model or get_active_risk_model(create_if_missing=True)
    baseline = model.drift_baseline or {}
    if not baseline:
        return None

    snapshots = list(
        SellerRiskSnapshot.objects.filter(model_version=model.version)
        .order_by('-created_at')[:sample_size]
    )
    if len(snapshots) < 12:
        return None

    feature_drift = {}
    for feature_name in TRAINABLE_FEATURES:
        values = [float((snapshot.feature_vector or {}).get(feature_name, 0.0)) for snapshot in snapshots]
        if not values:
            continue
        feature_drift[feature_name] = abs(float(sum(values) / len(values)) - float(baseline.get(feature_name, 0.0)))

    if not feature_drift:
        return None

    overall_drift_score = float(sum(feature_drift.values()) / len(feature_drift))
    return RiskDriftSnapshot.objects.create(
        model=model,
        overall_drift_score=overall_drift_score,
        feature_drift=feature_drift,
        is_alert=overall_drift_score >= FEATURE_DRIFT_ALERT_THRESHOLD,
        snapshot_count=len(snapshots),
    )


def detect_performance_drift(model=None, sample_size=180):
    model = model or get_active_risk_model(create_if_missing=True)
    incidents = list(
        SellerRiskIncident.objects.select_related('snapshot')
        .filter(status__in=TRAINABLE_LABEL_STATUSES, snapshot__model_version=model.version)
        .order_by('-final_decision_at', '-updated_at')[:sample_size]
    )
    if len(incidents) < 12:
        return None

    labels = []
    probabilities = []
    for incident in incidents:
        label = _incident_label(incident.status)
        if label is None or incident.snapshot is None:
            continue
        labels.append(label)
        probabilities.append(float(incident.snapshot.calibrated_probability or incident.snapshot.model_probability or 0.0))

    if len(labels) < 12:
        return None

    metrics = _compute_confusion(labels, probabilities, model.decision_threshold or DEFAULT_THRESHOLD)
    baseline_f1 = float((model.quality_metrics or {}).get('f1_score', 0.0))
    current_f1 = float(metrics['f1'])
    drift_score = max(0.0, baseline_f1 - current_f1)
    return RiskPerformanceDrift.objects.create(
        model=model,
        baseline_f1_score=baseline_f1,
        current_f1_score=current_f1,
        drift_score=drift_score,
        is_alert=drift_score >= PERFORMANCE_DRIFT_ALERT_THRESHOLD,
        window_start=incidents[-1].created_at,
        window_end=incidents[0].created_at,
        metadata={
            'sample_size': len(labels),
            'precision_score': metrics['precision'],
            'recall_score': metrics['recall'],
            'tp': metrics['tp'],
            'fp': metrics['fp'],
            'tn': metrics['tn'],
            'fn': metrics['fn'],
        },
    )


def _new_labels_since_model_train(model):
    queryset = SellerRiskIncident.objects.filter(status__in=TRAINABLE_LABEL_STATUSES)
    if model and model.trained_at:
        queryset = queryset.filter(final_decision_at__gt=model.trained_at)
    return queryset.count()


def maybe_trigger_retraining(reason='scheduled', force=False):
    model = get_active_risk_model(create_if_missing=True)
    if (
        not force
        and model
        and model.trained_at
        and (timezone.now() - model.trained_at) < timedelta(hours=TRAINING_COOLDOWN_HOURS)
    ):
        return model

    label_growth = _new_labels_since_model_train(model)
    if force or label_growth >= AUTO_RETRAIN_LABEL_GROWTH:
        return train_supervised_risk_model(force=True, reason=reason)
    return model


def _persist_snapshot(
    *,
    seller,
    risk_score,
    classification_label,
    complaint_ratio,
    failed_transaction_ratio,
    low_rating_ratio,
    cancellation_ratio,
    stale_pending_ratio,
    anomaly_score,
    confidence_score,
    risk_velocity,
    model_probability,
    calibrated_probability,
    decision_threshold,
    drift_score,
    network_risk_score,
    sequence_risk_score,
    model_version,
    feature_vector,
    top_contributors,
    risk_factors,
    is_flagged,
):
    return SellerRiskSnapshot.objects.create(
        seller=seller,
        risk_score=risk_score,
        complaint_ratio=complaint_ratio,
        failed_transaction_ratio=failed_transaction_ratio,
        low_rating_ratio=low_rating_ratio,
        cancellation_ratio=cancellation_ratio,
        stale_pending_ratio=stale_pending_ratio,
        anomaly_score=anomaly_score,
        confidence_score=confidence_score,
        risk_velocity=risk_velocity,
        model_probability=model_probability,
        calibrated_probability=calibrated_probability,
        decision_threshold=decision_threshold,
        drift_score=drift_score,
        network_risk_score=network_risk_score,
        sequence_risk_score=sequence_risk_score,
        classification_label=classification_label,
        model_version=model_version,
        feature_vector=feature_vector,
        top_contributors=top_contributors,
        risk_factors=risk_factors,
        is_flagged=is_flagged,
    )


def _set_seller_suspension_state(seller, *, suspended, verification_status, note='', risk_score=None):
    profile, _created = SellerProfile.objects.get_or_create(
        user=seller,
        defaults={'store_name': f'{seller.display_name} Store'},
    )
    profile.is_suspended = bool(suspended)
    profile.verification_status = verification_status
    if note:
        profile.suspension_note = note
    if risk_score is not None:
        profile.risk_score = float(risk_score)
    profile.save(update_fields=['is_suspended', 'verification_status', 'suspension_note', 'risk_score', 'updated_at'])
    return profile


def _refund_latest_success_transaction_for_booking(booking):
    refundable_tx = (
        booking.transactions.select_for_update()
        .filter(status=Transaction.TransactionStatus.SUCCESS)
        .order_by('-paid_at', '-created_at')
        .first()
    )
    if not refundable_tx:
        return None
    refundable_tx.status = Transaction.TransactionStatus.REFUNDED
    refundable_tx.save(update_fields=['status'])
    return refundable_tx


def _cancel_open_seller_bookings(seller, reason):
    now = timezone.now()
    open_bookings = list(
        Booking.objects.select_for_update()
        .filter(seller=seller)
        .exclude(status=Booking.BookingStatus.CANCELLED)
        .prefetch_related('items__product', 'transactions')
    )
    for booking in open_bookings:
        for item in booking.items.all():
            product = item.product
            product.stock_quantity += item.quantity
            product.save(update_fields=['stock_quantity'])
        booking.status = Booking.BookingStatus.CANCELLED
        booking.cancelled_at = now
        booking.cancelled_by_role = User.UserRole.ADMIN
        booking.cancellation_reason = reason
        booking.cancellation_impact = Booking.CancellationImpact.NEGATIVE_IMPACT
        booking.cancellation_impact_note = 'Auto-cancelled after fraud risk freeze/termination.'
        booking.cancellation_reviewed_at = now
        booking.cancellation_reviewed_by = None
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
            ]
        )
        _refund_latest_success_transaction_for_booking(booking)


def _upsert_active_incident(seller, snapshot, incident_note='', force_status=None):
    active_incident = _latest_active_incident(seller)
    reason_text = '; '.join(snapshot.risk_factors or [])
    if incident_note:
        reason_text = f'{reason_text}; {incident_note}' if reason_text else incident_note

    if active_incident:
        if force_status:
            active_incident.status = force_status
        active_incident.snapshot = snapshot
        active_incident.risk_score = snapshot.risk_score
        active_incident.classification_label = snapshot.classification_label
        active_incident.incident_reason = reason_text
        if active_incident.fine_amount <= 0:
            active_incident.fine_amount = _calculate_incident_fine(snapshot.risk_score)
        active_incident.seller_notified_at = active_incident.seller_notified_at or timezone.now()
        active_incident.save(
            update_fields=[
                'status',
                'snapshot',
                'risk_score',
                'classification_label',
                'incident_reason',
                'fine_amount',
                'seller_notified_at',
                'updated_at',
            ]
        )
        return active_incident

    return SellerRiskIncident.objects.create(
        seller=seller,
        snapshot=snapshot,
        status=force_status or SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
        risk_score=snapshot.risk_score,
        classification_label=snapshot.classification_label,
        incident_reason=reason_text,
        fine_amount=_calculate_incident_fine(snapshot.risk_score),
        seller_notified_at=timezone.now(),
        is_active=True,
    )


def freeze_seller_operations(seller, incident_note=''):
    reason = incident_note or 'Seller operations frozen after fraud risk review.'
    with transaction.atomic():
        _set_seller_suspension_state(
            seller,
            suspended=True,
            verification_status=SellerProfile.VerificationStatus.FLAGGED,
            note=reason,
        )
        _cancel_open_seller_bookings(seller, reason=reason)
    return True


def unfreeze_seller_operations(seller, decision_note=''):
    note = decision_note or 'Seller operations restored after review.'
    with transaction.atomic():
        _set_seller_suspension_state(
            seller,
            suspended=False,
            verification_status=SellerProfile.VerificationStatus.VERIFIED,
            note=note,
            risk_score=0.0,
        )
    return True


def terminate_seller_operations(seller, decision_note=''):
    note = decision_note or 'Seller operations terminated after fraud confirmation.'
    with transaction.atomic():
        _set_seller_suspension_state(
            seller,
            suspended=True,
            verification_status=SellerProfile.VerificationStatus.REJECTED,
            note=note,
        )
        Product.objects.filter(seller=seller, is_active=True).update(is_active=False)
        _cancel_open_seller_bookings(seller, reason=note)
    return True


def _score_seller_with_active_model(feature_vector, model):
    if model is None:
        rule_probability = _rule_based_probability(feature_vector)
        return {
            'model_probability': rule_probability,
            'calibrated_probability': rule_probability,
            'contributors': _heuristic_contributors(feature_vector),
            'decision_threshold': DEFAULT_THRESHOLD,
            'model_version': 'hybrid_v2_rules_only',
        }

    model_probability, calibrated_probability = _predict_probability(feature_vector, model)
    rule_probability = _rule_based_probability(feature_vector)
    blended_probability = _clip((0.72 * calibrated_probability) + (0.28 * rule_probability))
    contributors = _top_contributors(feature_vector, model.feature_weights or {})
    if not contributors:
        contributors = _heuristic_contributors(feature_vector)

    return {
        'model_probability': model_probability,
        'calibrated_probability': blended_probability,
        'contributors': contributors,
        'decision_threshold': float(model.decision_threshold or DEFAULT_THRESHOLD),
        'model_version': model.version,
    }


def calculate_seller_risk(
    seller,
    *,
    event_type=None,
    event_payload=None,
    force_freeze=False,
    incident_note='',
):
    if seller.role != User.UserRole.SELLER:
        return None

    active_model = get_active_risk_model(create_if_missing=True)
    feature_vector = _build_feature_vector(seller=seller, event_type=event_type)
    scoring = _score_seller_with_active_model(feature_vector, active_model)

    decision_threshold = float(scoring['decision_threshold'])
    calibrated_probability = float(scoring['calibrated_probability'])
    model_probability = float(scoring['model_probability'])
    risk_score = round(calibrated_probability * 100.0, 2)

    previous_snapshot = _latest_snapshot_for_seller(seller)
    previous_score = previous_snapshot.risk_score if previous_snapshot else 0.0
    risk_velocity = round(risk_score - float(previous_score), 2)

    classification_label = _classification_from_probability(calibrated_probability, decision_threshold)
    signal_strength = float(feature_vector.get('anomaly_score_hint', 0.0))
    confidence_score = _confidence_score(calibrated_probability, decision_threshold, signal_strength)
    anomaly_score = round(signal_strength * 100.0, 2)

    risk_factors = _build_risk_factors(feature_vector, scoring['contributors'], extra_note=incident_note)
    drift_score = _latest_drift_score(active_model)
    is_flagged = classification_label == SellerRiskSnapshot.ClassificationLabel.HIGH

    if force_freeze:
        is_flagged = True
        classification_label = SellerRiskSnapshot.ClassificationLabel.HIGH
        calibrated_probability = max(calibrated_probability, decision_threshold + 0.03)
        risk_score = round(calibrated_probability * 100.0, 2)

    if is_flagged and _manual_unfreeze_guard_blocks_auto_freeze(seller):
        is_flagged = False
        if classification_label == SellerRiskSnapshot.ClassificationLabel.HIGH:
            classification_label = SellerRiskSnapshot.ClassificationLabel.MEDIUM
            calibrated_probability = min(calibrated_probability, max(0.45, decision_threshold - 0.02))
            risk_score = round(calibrated_probability * 100.0, 2)
        risk_factors.append('Auto-freeze protection active after manual unfreeze; no new risk signals observed yet.')

    snapshot = _persist_snapshot(
        seller=seller,
        risk_score=risk_score,
        classification_label=classification_label,
        complaint_ratio=feature_vector.get('complaint_ratio', 0.0),
        failed_transaction_ratio=feature_vector.get('failed_transaction_ratio', 0.0),
        low_rating_ratio=feature_vector.get('low_rating_ratio', 0.0),
        cancellation_ratio=feature_vector.get('cancellation_ratio', 0.0),
        stale_pending_ratio=feature_vector.get('stale_pending_ratio', 0.0),
        anomaly_score=anomaly_score,
        confidence_score=confidence_score,
        risk_velocity=risk_velocity,
        model_probability=model_probability,
        calibrated_probability=calibrated_probability,
        decision_threshold=decision_threshold,
        drift_score=drift_score,
        network_risk_score=round(feature_vector.get('network_risk_score', 0.0) * 100.0, 2),
        sequence_risk_score=round(feature_vector.get('sequence_risk_score', 0.0) * 100.0, 2),
        model_version=scoring['model_version'],
        feature_vector=feature_vector,
        top_contributors=scoring['contributors'],
        risk_factors=risk_factors,
        is_flagged=is_flagged,
    )

    if is_flagged:
        freeze_note = '; '.join(risk_factors[:3]) or 'High-risk fraud score.'
        freeze_seller_operations(seller, incident_note=freeze_note)
        _upsert_active_incident(
            seller=seller,
            snapshot=snapshot,
            incident_note=incident_note,
            force_status=SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING,
        )
    return snapshot


def calculate_seller_risk_batch(sellers=None):
    maybe_trigger_retraining(reason='label_growth_check', force=False)

    if sellers is None:
        sellers = User.objects.filter(role=User.UserRole.SELLER)
    else:
        sellers = [seller for seller in sellers if seller.role == User.UserRole.SELLER]

    snapshots = []
    for seller in sellers:
        snapshot = calculate_seller_risk(seller)
        if snapshot is not None:
            snapshots.append(snapshot)

    active_model = get_active_risk_model(create_if_missing=True)
    feature_drift = detect_feature_drift(model=active_model)
    performance_drift = detect_performance_drift(model=active_model)

    if (feature_drift and feature_drift.is_alert) or (performance_drift and performance_drift.is_alert):
        maybe_trigger_retraining(reason='drift_alert', force=True)

    return snapshots


def _save_realtime_event(
    *,
    seller,
    event_type,
    payload,
    booking=None,
    transaction=None,
    snapshot=None,
):
    snapshot = snapshot or _latest_snapshot_for_seller(seller)
    return RiskRealtimeEvent.objects.create(
        seller=seller,
        event_type=event_type,
        booking=booking,
        transaction=transaction,
        payload=payload or {},
        snapshot=snapshot,
        risk_score=float(snapshot.risk_score if snapshot else 0.0),
        calibrated_probability=float(snapshot.calibrated_probability if snapshot else 0.0),
    )


def score_realtime_event(
    *,
    seller,
    event_type,
    payload=None,
    booking=None,
    transaction=None,
    force_freeze=False,
    incident_note='',
):
    payload = payload or {}
    snapshot = calculate_seller_risk(
        seller=seller,
        event_type=event_type,
        event_payload=payload,
        force_freeze=force_freeze,
        incident_note=incident_note,
    )
    event = _save_realtime_event(
        seller=seller,
        event_type=event_type,
        payload=payload,
        booking=booking,
        transaction=transaction,
        snapshot=snapshot,
    )
    incident = _latest_active_incident(seller)

    if RiskRealtimeEvent.objects.filter(seller=seller).count() % 24 == 0:
        maybe_trigger_retraining(reason='realtime_event_volume', force=False)

    return snapshot, incident, event


def report_booking_created_event(booking, payload=None):
    payload = payload or {}
    payload.setdefault('booking_id', booking.id)
    payload.setdefault('customer_id', booking.customer_id)
    payload.setdefault('shipping_address', booking.shipping_address)
    payload.setdefault('event_source', 'booking_created')
    snapshot, _incident, event = score_realtime_event(
        seller=booking.seller,
        event_type=RiskRealtimeEvent.EventType.BOOKING_CREATED,
        payload=payload,
        booking=booking,
        force_freeze=False,
    )
    return snapshot, event


def report_cancellation_anomaly_for_booking(booking, admin_note='', force_high_risk=None):
    payload = {
        'booking_id': booking.id,
        'customer_id': booking.customer_id,
        'cancelled_by_role': booking.cancelled_by_role,
        'cancellation_reason': booking.cancellation_reason,
        'cancellation_impact': booking.cancellation_impact,
        'event_source': 'booking_cancelled',
    }
    if admin_note:
        payload['admin_note'] = admin_note

    if force_high_risk is None:
        force_high_risk = bool(
            booking.cancelled_by_role == User.UserRole.SELLER
            or booking.cancellation_impact == Booking.CancellationImpact.NEGATIVE_IMPACT
            or admin_note
        )

    snapshot, incident, _event = score_realtime_event(
        seller=booking.seller,
        event_type=RiskRealtimeEvent.EventType.BOOKING_CANCELLED,
        payload=payload,
        booking=booking,
        force_freeze=force_high_risk,
        incident_note=admin_note,
    )
    return snapshot, incident


def report_failed_payment_event(transaction_obj, payload=None):
    booking = transaction_obj.booking
    payload = payload or {}
    payload.setdefault('booking_id', booking.id if booking else None)
    payload.setdefault('transaction_id', transaction_obj.id)
    payload.setdefault('payment_method', transaction_obj.payment_method)
    payload.setdefault('payment_handle', payload.get('payment_handle') or '')
    payload.setdefault('event_source', 'payment_failed')

    snapshot, incident, event = score_realtime_event(
        seller=booking.seller,
        event_type=RiskRealtimeEvent.EventType.PAYMENT_FAILED,
        payload=payload,
        booking=booking,
        transaction=transaction_obj,
        force_freeze=False,
    )
    return snapshot, incident, event
