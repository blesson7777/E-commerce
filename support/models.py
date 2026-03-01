from django.core.validators import MaxValueValidator
from django.core.validators import MinValueValidator
from django.db import models


class Complaint(models.Model):
    class ComplaintStatus(models.TextChoices):
        OPEN = 'open', 'Open'
        IN_PROGRESS = 'in_progress', 'In Progress'
        RESOLVED = 'resolved', 'Resolved'
        CLOSED = 'closed', 'Closed'

    customer = models.ForeignKey(
        'accounts.User',
        on_delete=models.CASCADE,
        related_name='complaints',
        limit_choices_to={'role': 'customer'},
    )
    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='complaints',
    )
    booking = models.ForeignKey(
        'orders.Booking',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='complaints',
    )
    subject = models.CharField(max_length=150)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=ComplaintStatus.choices, default=ComplaintStatus.OPEN)
    is_anomaly = models.BooleanField(default=False)
    anomaly_note = models.TextField(blank=True)
    anomaly_marked_at = models.DateTimeField(null=True, blank=True)
    anomaly_marked_by = models.ForeignKey(
        'accounts.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='marked_complaint_anomalies',
        limit_choices_to={'role': 'admin'},
    )
    ml_scored_at = models.DateTimeField(null=True, blank=True)
    risk_snapshot = models.ForeignKey(
        'analytics.SellerRiskSnapshot',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='complaint_anomalies',
    )
    risk_incident = models.ForeignKey(
        'analytics.SellerRiskIncident',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='complaint_anomalies',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.subject


class Feedback(models.Model):
    customer = models.ForeignKey(
        'accounts.User',
        on_delete=models.CASCADE,
        related_name='feedbacks',
        limit_choices_to={'role': 'customer'},
    )
    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feedbacks',
    )
    booking = models.ForeignKey(
        'orders.Booking',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feedbacks',
    )
    rating = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Feedback {self.rating}/5'

# Create your models here.
