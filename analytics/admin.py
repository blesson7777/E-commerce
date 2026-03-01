from django.contrib import admin

from analytics.models import RiskDriftSnapshot
from analytics.models import RiskModelBacktest
from analytics.models import RiskModelVersion
from analytics.models import RiskPerformanceDrift
from analytics.models import RiskRealtimeEvent
from analytics.models import SellerRiskIncident
from analytics.models import SellerRiskSnapshot


@admin.register(SellerRiskSnapshot)
class SellerRiskSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'seller',
        'risk_score',
        'classification_label',
        'anomaly_score',
        'confidence_score',
        'model_version',
        'top_contributors_preview',
        'created_at',
    )
    list_filter = ('classification_label', 'is_flagged', 'model_version', 'created_at')
    search_fields = ('seller__email', 'seller__first_name', 'seller__last_name')

    def top_contributors_preview(self, obj):
        contributors = obj.top_contributors or []
        labels = []
        for item in contributors[:3]:
            name = item.get('label') or item.get('feature') or 'feature'
            impact = item.get('impact')
            if impact is None:
                labels.append(str(name))
            else:
                labels.append(f'{name} ({impact})')
        return ', '.join(labels) if labels else '-'

    top_contributors_preview.short_description = 'Top Contributors'


@admin.register(SellerRiskIncident)
class SellerRiskIncidentAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'seller',
        'status',
        'risk_score',
        'classification_label',
        'fine_amount',
        'snapshot_contributors_preview',
        'is_active',
        'created_at',
    )
    list_filter = ('status', 'classification_label', 'is_active', 'created_at')
    search_fields = ('seller__email', 'seller__first_name', 'seller__last_name')

    def snapshot_contributors_preview(self, obj):
        if not obj.snapshot_id:
            return '-'
        contributors = obj.snapshot.top_contributors or []
        labels = []
        for item in contributors[:3]:
            name = item.get('label') or item.get('feature') or 'feature'
            impact = item.get('impact')
            if impact is None:
                labels.append(str(name))
            else:
                labels.append(f'{name} ({impact})')
        return ', '.join(labels) if labels else '-'

    snapshot_contributors_preview.short_description = 'Incident Explainability'


@admin.register(RiskModelVersion)
class RiskModelVersionAdmin(admin.ModelAdmin):
    list_display = (
        'version',
        'model_name',
        'algorithm',
        'stage',
        'is_active',
        'decision_threshold',
        'training_samples',
        'trained_at',
    )
    list_filter = ('model_name', 'stage', 'is_active', 'algorithm')
    search_fields = ('version', 'model_name')


@admin.register(RiskModelBacktest)
class RiskModelBacktestAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'model',
        'sample_size',
        'precision_score',
        'recall_score',
        'f1_score',
        'auc_score',
        'created_at',
    )
    list_filter = ('model', 'created_at')


@admin.register(RiskDriftSnapshot)
class RiskDriftSnapshotAdmin(admin.ModelAdmin):
    list_display = ('id', 'model', 'overall_drift_score', 'is_alert', 'snapshot_count', 'created_at')
    list_filter = ('is_alert', 'model', 'created_at')


@admin.register(RiskPerformanceDrift)
class RiskPerformanceDriftAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'model',
        'baseline_f1_score',
        'current_f1_score',
        'drift_score',
        'is_alert',
        'created_at',
    )
    list_filter = ('is_alert', 'model', 'created_at')


@admin.register(RiskRealtimeEvent)
class RiskRealtimeEventAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'seller',
        'event_type',
        'booking',
        'transaction',
        'risk_score',
        'calibrated_probability',
        'created_at',
    )
    list_filter = ('event_type', 'created_at')
    search_fields = ('seller__email', 'seller__first_name', 'seller__last_name')
