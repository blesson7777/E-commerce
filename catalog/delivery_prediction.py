from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from math import exp

from django.utils import timezone

from orders.models import Booking
from orders.models import BookingItem


DEFAULT_DELIVERY_DAYS = 10
MIN_DELIVERY_DAYS = 1
MAX_DELIVERY_DAYS = 30
RECENCY_DECAY_DAYS = 120.0


@dataclass(frozen=True)
class DeliveryPrediction:
    days: int
    expected_date: date
    sample_count: int
    is_fallback: bool
    source: str


def _clamp_days(value):
    return max(MIN_DELIVERY_DAYS, min(MAX_DELIVERY_DAYS, int(round(value))))


def _sample_days_and_weight(booked_at, expected_delivery_date, as_of_date):
    if not booked_at or not expected_delivery_date:
        return None
    booked_date = booked_at.date()
    lead_days = (expected_delivery_date - booked_date).days
    if lead_days <= 0:
        return None
    age_days = max((as_of_date - booked_date).days, 0)
    recency_weight = exp(-(age_days / RECENCY_DECAY_DAYS))
    return _clamp_days(lead_days), recency_weight


def _add_stat(bucket, key, days, weight):
    stats = bucket.setdefault(
        key,
        {
            'weighted_sum': 0.0,
            'weight_total': 0.0,
            'count': 0,
        },
    )
    stats['weighted_sum'] += float(days) * float(weight)
    stats['weight_total'] += float(weight)
    stats['count'] += 1


def _get_mean_and_count(bucket, key):
    stats = bucket.get(key)
    if not stats:
        return 0.0, 0
    if stats['weight_total'] <= 0:
        return 0.0, 0
    return stats['weighted_sum'] / stats['weight_total'], stats['count']


def _build_prediction(days, sample_count, as_of_date, is_fallback, source):
    final_days = _clamp_days(days)
    return DeliveryPrediction(
        days=final_days,
        expected_date=as_of_date + timedelta(days=final_days),
        sample_count=sample_count,
        is_fallback=is_fallback,
        source=source,
    )


def predict_delivery_for_products(products, booking_date=None):
    product_list = [product for product in products if getattr(product, 'id', None)]
    if not product_list:
        return {}

    as_of_date = booking_date or timezone.localdate()
    product_ids = {product.id for product in product_list}
    seller_ids = {product.seller_id for product in product_list if product.seller_id}
    category_ids = {product.category_id for product in product_list if product.category_id}

    sample_base = (
        BookingItem.objects.filter(booking__expected_delivery_date__isnull=False)
        .exclude(booking__status=Booking.BookingStatus.CANCELLED)
        .select_related(None)
    )

    product_stats = {}
    for row in sample_base.filter(product_id__in=product_ids).values(
        'product_id',
        'booking__booked_at',
        'booking__expected_delivery_date',
    ):
        sample = _sample_days_and_weight(
            row['booking__booked_at'],
            row['booking__expected_delivery_date'],
            as_of_date,
        )
        if sample is None:
            continue
        lead_days, recency_weight = sample
        _add_stat(product_stats, row['product_id'], lead_days, recency_weight)

    seller_stats = {}
    for row in sample_base.filter(booking__seller_id__in=seller_ids).values(
        'booking__seller_id',
        'booking__booked_at',
        'booking__expected_delivery_date',
    ):
        sample = _sample_days_and_weight(
            row['booking__booked_at'],
            row['booking__expected_delivery_date'],
            as_of_date,
        )
        if sample is None:
            continue
        lead_days, recency_weight = sample
        _add_stat(seller_stats, row['booking__seller_id'], lead_days, recency_weight)

    category_stats = {}
    for row in sample_base.filter(product__category_id__in=category_ids).values(
        'product__category_id',
        'booking__booked_at',
        'booking__expected_delivery_date',
    ):
        sample = _sample_days_and_weight(
            row['booking__booked_at'],
            row['booking__expected_delivery_date'],
            as_of_date,
        )
        if sample is None:
            continue
        lead_days, recency_weight = sample
        _add_stat(category_stats, row['product__category_id'], lead_days, recency_weight)

    predictions = {}
    for product in product_list:
        product_mean, product_count = _get_mean_and_count(product_stats, product.id)
        seller_mean, seller_count = _get_mean_and_count(seller_stats, product.seller_id)
        category_mean, category_count = _get_mean_and_count(category_stats, product.category_id)

        if product_count == 0 and seller_count == 0 and category_count == 0:
            predictions[product.id] = _build_prediction(
                days=DEFAULT_DELIVERY_DAYS,
                sample_count=0,
                as_of_date=as_of_date,
                is_fallback=True,
                source='default',
            )
            continue

        weighted_sum = DEFAULT_DELIVERY_DAYS * 0.12
        total_weight = 0.12
        source = 'default'

        if product_count:
            product_weight = min(1.10, 0.40 + (0.10 * product_count))
            weighted_sum += product_mean * product_weight
            total_weight += product_weight
            source = 'product'

        if seller_count:
            seller_weight = min(0.45, 0.18 + (0.04 * seller_count))
            weighted_sum += seller_mean * seller_weight
            total_weight += seller_weight
            if source == 'default':
                source = 'seller'

        if category_count:
            category_weight = min(0.35, 0.12 + (0.03 * category_count))
            weighted_sum += category_mean * category_weight
            total_weight += category_weight
            if source == 'default':
                source = 'category'

        predicted_days = weighted_sum / total_weight if total_weight else DEFAULT_DELIVERY_DAYS
        predictions[product.id] = _build_prediction(
            days=predicted_days,
            sample_count=product_count,
            as_of_date=as_of_date,
            is_fallback=False,
            source=source,
        )

    return predictions


def predict_delivery_for_product(product, booking_date=None):
    if not product or not getattr(product, 'id', None):
        as_of_date = booking_date or timezone.localdate()
        return _build_prediction(
            days=DEFAULT_DELIVERY_DAYS,
            sample_count=0,
            as_of_date=as_of_date,
            is_fallback=True,
            source='default',
        )
    return predict_delivery_for_products([product], booking_date=booking_date)[product.id]


def attach_delivery_predictions(products, booking_date=None):
    predictions = predict_delivery_for_products(products, booking_date=booking_date)
    for product in products:
        prediction = predictions.get(product.id)
        if prediction is None:
            prediction = predict_delivery_for_product(product, booking_date=booking_date)
        product.predicted_delivery_days = prediction.days
        product.predicted_delivery_date = prediction.expected_date
        product.predicted_delivery_is_fallback = prediction.is_fallback
        product.predicted_delivery_source = prediction.source
    return predictions
