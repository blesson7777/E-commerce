from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from math import exp
from math import ceil

from django.utils import timezone

from orders.models import Booking
from orders.models import BookingItem


DEFAULT_DAILY_DEMAND = 0.35
RECENCY_DECAY_DAYS = 60.0


@dataclass(frozen=True)
class RestockPrediction:
    expected_restock_date: date
    predicted_daily_demand: float
    predicted_stockout_date: date
    confidence_label: str
    sample_count: int


def _safe_positive(value):
    return value if value > 0 else 0.0


def _weighted_rate(rows, as_of_date):
    weighted_units = 0.0
    weighted_days = 0.0
    sample_count = 0
    for row in rows:
        booked_at = row['booking__booked_at']
        quantity = row['quantity']
        if not booked_at or not quantity:
            continue
        booking_date = booked_at.date()
        age_days = max((as_of_date - booking_date).days, 0)
        recency_weight = exp(-(age_days / RECENCY_DECAY_DAYS))
        weighted_units += float(quantity) * recency_weight
        weighted_days += recency_weight
        sample_count += 1
    if weighted_days <= 0:
        return 0.0, 0
    return weighted_units / weighted_days, sample_count


def attach_restock_predictions(products, *, reorder_level=5):
    product_list = [product for product in products if getattr(product, 'id', None)]
    if not product_list:
        return {}

    as_of_date = timezone.localdate()
    product_ids = [product.id for product in product_list]
    seller_ids = sorted({product.seller_id for product in product_list if product.seller_id})

    history_rows = list(
        BookingItem.objects.filter(product_id__in=product_ids)
        .exclude(booking__status=Booking.BookingStatus.CANCELLED)
        .values('product_id', 'quantity', 'booking__booked_at')
    )

    product_history = {}
    for row in history_rows:
        bucket = product_history.setdefault(row['product_id'], [])
        bucket.append(row)

    seller_history_rows = list(
        BookingItem.objects.filter(product__seller_id__in=seller_ids)
        .exclude(booking__status=Booking.BookingStatus.CANCELLED)
        .values('product__seller_id', 'quantity', 'booking__booked_at')
    )
    seller_history = {}
    for row in seller_history_rows:
        bucket = seller_history.setdefault(row['product__seller_id'], [])
        bucket.append(
            {
                'quantity': row['quantity'],
                'booking__booked_at': row['booking__booked_at'],
            }
        )

    prediction_map = {}
    for product in product_list:
        product_rate, product_samples = _weighted_rate(product_history.get(product.id, []), as_of_date)
        seller_rate, seller_samples = _weighted_rate(seller_history.get(product.seller_id, []), as_of_date)

        if product_samples >= 5:
            predicted_daily_demand = max(product_rate, 0.05)
            confidence = 'High'
            source_samples = product_samples
        elif product_samples >= 2:
            predicted_daily_demand = max((product_rate * 0.7) + (seller_rate * 0.3), 0.05)
            confidence = 'Medium'
            source_samples = product_samples
        elif seller_samples >= 4:
            predicted_daily_demand = max((seller_rate * 0.8) + (DEFAULT_DAILY_DEMAND * 0.2), 0.05)
            confidence = 'Low'
            source_samples = seller_samples
        else:
            predicted_daily_demand = DEFAULT_DAILY_DEMAND
            confidence = 'Default'
            source_samples = 0

        stock_qty = max(int(product.stock_quantity or 0), 0)
        effective_stock_for_restock = max(stock_qty - int(reorder_level), 0)
        days_to_restock = max(0, int(ceil(effective_stock_for_restock / _safe_positive(predicted_daily_demand))))
        days_to_stockout = max(0, int(ceil(stock_qty / _safe_positive(predicted_daily_demand))))

        expected_restock_date = as_of_date + timedelta(days=days_to_restock)
        predicted_stockout_date = as_of_date + timedelta(days=days_to_stockout)

        prediction = RestockPrediction(
            expected_restock_date=expected_restock_date,
            predicted_daily_demand=predicted_daily_demand,
            predicted_stockout_date=predicted_stockout_date,
            confidence_label=confidence,
            sample_count=source_samples,
        )
        product.predicted_restock_date = prediction.expected_restock_date
        product.predicted_stockout_date = prediction.predicted_stockout_date
        product.predicted_daily_demand = round(prediction.predicted_daily_demand, 2)
        product.predicted_restock_confidence = prediction.confidence_label
        product.predicted_restock_sample_count = prediction.sample_count
        prediction_map[product.id] = prediction

    return prediction_map
