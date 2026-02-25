from django.db import models


class Booking(models.Model):
    class BookingStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CONFIRMED = 'confirmed', 'Confirmed'
        SHIPPED = 'shipped', 'Shipped'
        OUT_FOR_DELIVERY = 'out_for_delivery', 'Out for Delivery'
        DELIVERED = 'delivered', 'Delivered'
        CANCELLED = 'cancelled', 'Cancelled'

    class CancellationImpact(models.TextChoices):
        NOT_REVIEWED = 'not_reviewed', 'Not Reviewed'
        NO_IMPACT = 'no_impact', 'No Impact'
        NEGATIVE_IMPACT = 'negative_impact', 'Negative Impact'

    customer = models.ForeignKey(
        'accounts.User',
        on_delete=models.CASCADE,
        related_name='customer_bookings',
        limit_choices_to={'role': 'customer'},
    )
    seller = models.ForeignKey(
        'accounts.User',
        on_delete=models.CASCADE,
        related_name='seller_bookings',
        limit_choices_to={'role': 'seller'},
    )
    booked_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=BookingStatus.choices, default=BookingStatus.PENDING)
    delivery_location = models.ForeignKey(
        'locations.Location',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='bookings',
    )
    shipping_address = models.TextField()
    expected_delivery_date = models.DateField(null=True, blank=True)
    tracking_number = models.CharField(max_length=40, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cancellation_reason = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by_role = models.CharField(max_length=20, blank=True)
    cancellation_impact = models.CharField(
        max_length=20,
        choices=CancellationImpact.choices,
        default=CancellationImpact.NOT_REVIEWED,
    )
    cancellation_impact_note = models.TextField(blank=True)
    cancellation_reviewed_at = models.DateTimeField(null=True, blank=True)
    cancellation_reviewed_by = models.ForeignKey(
        'accounts.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_booking_cancellations',
        limit_choices_to={'role': 'admin'},
    )
    anomaly_reported_at = models.DateTimeField(null=True, blank=True)
    anomaly_incident = models.ForeignKey(
        'analytics.SellerRiskIncident',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='anomalous_cancellation_bookings',
    )

    class Meta:
        ordering = ['-booked_at']

    def __str__(self):
        return f'Booking #{self.id}'


class BookingItem(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('catalog.Product', on_delete=models.PROTECT, related_name='booking_items')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ('booking', 'product')

    def __str__(self):
        return f'{self.product.name} x{self.quantity}'

    @property
    def subtotal(self):
        return self.quantity * self.unit_price


class Transaction(models.Model):
    class TransactionStatus(models.TextChoices):
        INITIATED = 'initiated', 'Initiated'
        SUCCESS = 'success', 'Success'
        FAILED = 'failed', 'Failed'
        REFUNDED = 'refunded', 'Refunded'

    class PaymentMethod(models.TextChoices):
        CARD = 'card', 'Card'
        UPI = 'upi', 'UPI'
        COD = 'cod', 'Cash on Delivery'
        WALLET = 'wallet', 'Wallet'
        NET_BANKING = 'net_banking', 'Net Banking'

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    status = models.CharField(max_length=20, choices=TransactionStatus.choices, default=TransactionStatus.INITIATED)
    transaction_reference = models.CharField(max_length=80, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'TXN-{self.transaction_reference}'

# Create your models here.
