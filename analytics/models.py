from django.db import models


class SellerRiskSnapshot(models.Model):
    class ClassificationLabel(models.TextChoices):
        LOW = 'low_risk', 'Low Risk'
        MEDIUM = 'medium_risk', 'Medium Risk'
        HIGH = 'high_risk', 'High Risk'

    seller = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='risk_snapshots')
    risk_score = models.FloatField()
    complaint_ratio = models.FloatField(default=0)
    failed_transaction_ratio = models.FloatField(default=0)
    low_rating_ratio = models.FloatField(default=0)
    cancellation_ratio = models.FloatField(default=0)
    stale_pending_ratio = models.FloatField(default=0)
    anomaly_score = models.FloatField(default=0)
    confidence_score = models.FloatField(default=0)
    risk_velocity = models.FloatField(default=0)
    model_probability = models.FloatField(default=0)
    calibrated_probability = models.FloatField(default=0)
    decision_threshold = models.FloatField(default=0.70)
    drift_score = models.FloatField(default=0)
    network_risk_score = models.FloatField(default=0)
    sequence_risk_score = models.FloatField(default=0)
    classification_label = models.CharField(
        max_length=20,
        choices=ClassificationLabel.choices,
        default=ClassificationLabel.LOW,
    )
    model_version = models.CharField(max_length=40, default='hybrid_v2')
    feature_vector = models.JSONField(default=dict, blank=True)
    top_contributors = models.JSONField(default=list, blank=True)
    risk_factors = models.JSONField(default=list, blank=True)
    is_flagged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.seller.username}: {self.classification_label} ({self.risk_score:.2f})'


class SellerRiskIncident(models.Model):
    class IncidentStatus(models.TextChoices):
        FROZEN_FINE_PENDING = 'frozen_fine_pending', 'Frozen - Fine Pending'
        FINE_PAID = 'fine_paid', 'Fine Paid'
        APPEALED = 'appealed', 'Appealed'
        UNDER_REVIEW = 'under_review', 'Under Review'
        RESOLVED_UNFROZEN = 'resolved_unfrozen', 'Resolved - Unfrozen'
        RESOLVED_FROZEN = 'resolved_frozen', 'Resolved - Frozen'
        RESOLVED_TERMINATED = 'resolved_terminated', 'Resolved - Terminated'

    seller = models.ForeignKey(
        'accounts.User',
        on_delete=models.CASCADE,
        related_name='risk_incidents',
    )
    snapshot = models.ForeignKey(
        SellerRiskSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='incidents',
    )
    status = models.CharField(
        max_length=32,
        choices=IncidentStatus.choices,
        default=IncidentStatus.FROZEN_FINE_PENDING,
    )
    risk_score = models.FloatField(default=0.0)
    classification_label = models.CharField(
        max_length=20,
        choices=SellerRiskSnapshot.ClassificationLabel.choices,
        default=SellerRiskSnapshot.ClassificationLabel.HIGH,
    )
    incident_reason = models.TextField(blank=True)
    fine_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fine_paid_at = models.DateTimeField(null=True, blank=True)
    appeal_text = models.TextField(blank=True)
    appealed_at = models.DateTimeField(null=True, blank=True)
    seller_notified_at = models.DateTimeField(null=True, blank=True)
    seller_acknowledged_at = models.DateTimeField(null=True, blank=True)
    admin_decision_note = models.TextField(blank=True)
    final_decision_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Incident<{self.seller.display_name} - {self.get_status_display()}>'


class RiskModelVersion(models.Model):
    class ModelStage(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        STAGING = 'staging', 'Staging'
        PRODUCTION = 'production', 'Production'
        ARCHIVED = 'archived', 'Archived'

    model_name = models.CharField(max_length=60, default='seller_fraud')
    version = models.CharField(max_length=60, unique=True)
    algorithm = models.CharField(max_length=80, default='hybrid_logistic')
    stage = models.CharField(
        max_length=20,
        choices=ModelStage.choices,
        default=ModelStage.DRAFT,
    )
    is_active = models.BooleanField(default=False)
    bias = models.FloatField(default=0.0)
    decision_threshold = models.FloatField(default=0.70)
    feature_weights = models.JSONField(default=dict, blank=True)
    calibration_bins = models.JSONField(default=list, blank=True)
    drift_baseline = models.JSONField(default=dict, blank=True)
    quality_metrics = models.JSONField(default=dict, blank=True)
    training_window_start = models.DateTimeField(null=True, blank=True)
    training_window_end = models.DateTimeField(null=True, blank=True)
    training_samples = models.PositiveIntegerField(default=0)
    positive_samples = models.PositiveIntegerField(default=0)
    trained_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.model_name}:{self.version} ({self.get_stage_display()})'


class RiskModelBacktest(models.Model):
    model = models.ForeignKey(
        RiskModelVersion,
        on_delete=models.CASCADE,
        related_name='backtests',
    )
    sample_size = models.PositiveIntegerField(default=0)
    precision_score = models.FloatField(default=0)
    recall_score = models.FloatField(default=0)
    f1_score = models.FloatField(default=0)
    auc_score = models.FloatField(default=0)
    confusion_matrix = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Backtest<{self.model.version} @ {self.created_at:%Y-%m-%d %H:%M}>'


class RiskDriftSnapshot(models.Model):
    model = models.ForeignKey(
        RiskModelVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='drift_snapshots',
    )
    overall_drift_score = models.FloatField(default=0)
    feature_drift = models.JSONField(default=dict, blank=True)
    is_alert = models.BooleanField(default=False)
    snapshot_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        model_version = self.model.version if self.model_id else 'no-model'
        return f'Drift<{model_version} score={self.overall_drift_score:.2f}>'


class RiskPerformanceDrift(models.Model):
    model = models.ForeignKey(
        RiskModelVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='performance_drifts',
    )
    baseline_f1_score = models.FloatField(default=0)
    current_f1_score = models.FloatField(default=0)
    drift_score = models.FloatField(default=0)
    is_alert = models.BooleanField(default=False)
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        model_version = self.model.version if self.model_id else 'no-model'
        return f'PerformanceDrift<{model_version} score={self.drift_score:.2f}>'


class RiskRealtimeEvent(models.Model):
    class EventType(models.TextChoices):
        BOOKING_CREATED = 'booking_created', 'Booking Created'
        BOOKING_CANCELLED = 'booking_cancelled', 'Booking Cancelled'
        PAYMENT_FAILED = 'payment_failed', 'Payment Failed'
        PAYMENT_SUCCESS = 'payment_success', 'Payment Success'
        MANUAL_REVIEW = 'manual_review', 'Manual Review'

    seller = models.ForeignKey(
        'accounts.User',
        on_delete=models.CASCADE,
        related_name='risk_realtime_events',
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    booking = models.ForeignKey(
        'orders.Booking',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='risk_events',
    )
    transaction = models.ForeignKey(
        'orders.Transaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='risk_events',
    )
    payload = models.JSONField(default=dict, blank=True)
    snapshot = models.ForeignKey(
        SellerRiskSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='realtime_events',
    )
    risk_score = models.FloatField(default=0)
    calibrated_probability = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'RealtimeEvent<{self.seller.display_name} {self.event_type}>'
