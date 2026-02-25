import csv
from io import BytesIO

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.pagesizes import landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph
from reportlab.platypus import SimpleDocTemplate
from reportlab.platypus import Spacer
from reportlab.platypus import Table
from reportlab.platypus import TableStyle

from accounts.decorators import role_required
from accounts.models import SellerProfile
from accounts.models import User
from analytics.forms import AdminRiskFinalDecisionForm
from analytics.forms import SellerFinePaymentForm
from analytics.forms import SellerRiskAppealForm
from analytics.models import SellerRiskIncident
from analytics.models import SellerRiskSnapshot
from analytics.services import calculate_seller_risk
from analytics.services import calculate_seller_risk_batch
from analytics.services import freeze_seller_operations
from analytics.services import terminate_seller_operations
from analytics.services import unfreeze_seller_operations
from orders.models import Booking
from orders.models import Transaction
from support.models import Complaint
from support.models import Feedback


def _table_pdf_response(*, filename, title, headers, rows, subtitle='', use_landscape=False):
    buffer = BytesIO()
    page_size = landscape(A4) if use_landscape else A4
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=18,
        rightMargin=18,
        topMargin=24,
        bottomMargin=20,
    )
    styles = getSampleStyleSheet()
    elements = [Paragraph(title, styles['Title'])]
    if subtitle:
        elements.append(Paragraph(subtitle, styles['BodyText']))
    elements.append(Spacer(1, 8))

    safe_headers = [str(value) for value in headers]
    safe_rows = [[str(cell) for cell in row] for row in rows]
    if not safe_rows:
        safe_rows = [['No records found.'] + ['' for _ in safe_headers[1:]]]
    table_data = [safe_headers] + safe_rows
    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#0f172a')),
                ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#cbd5e1')),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]
        )
    )
    elements.append(table)
    doc.build(elements)

    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _latest_seller_snapshots(limit=200):
    snapshots = []
    seen_seller_ids = set()
    for snapshot in SellerRiskSnapshot.objects.select_related('seller').order_by('-created_at'):
        if snapshot.seller_id in seen_seller_ids:
            continue
        seen_seller_ids.add(snapshot.seller_id)
        snapshots.append(snapshot)
        if len(snapshots) >= limit:
            break
    return snapshots


@role_required(User.UserRole.ADMIN)
def run_seller_verification(request):
    if request.method == 'POST':
        sellers = User.objects.filter(role=User.UserRole.SELLER)
        calculate_seller_risk_batch(sellers)
        return redirect('analytics:verification_results')
    return render(request, 'analytics/run_verification.html')


@role_required(User.UserRole.ADMIN)
def fraud_detection_dashboard(request):
    latest_snapshots = _latest_seller_snapshots(limit=300)
    active_incidents = SellerRiskIncident.objects.filter(is_active=True).count()
    terminated_sellers = SellerProfile.objects.filter(
        verification_status=SellerProfile.VerificationStatus.REJECTED
    ).count()
    high_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.HIGH
    )
    medium_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.MEDIUM
    )
    low_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.LOW
    )
    one_star_feedback_count = Feedback.objects.filter(rating=1).count()
    high_confidence_count = sum(1 for snapshot in latest_snapshots if snapshot.confidence_score >= 70)
    sharp_risk_jump_count = sum(1 for snapshot in latest_snapshots if snapshot.risk_velocity >= 10)

    context = {
        'latest_snapshots': latest_snapshots[:25],
        'snapshot_count': len(latest_snapshots),
        'high_risk_count': high_risk_count,
        'medium_risk_count': medium_risk_count,
        'low_risk_count': low_risk_count,
        'active_incidents': active_incidents,
        'terminated_sellers': terminated_sellers,
        'one_star_feedback_count': one_star_feedback_count,
        'high_confidence_count': high_confidence_count,
        'sharp_risk_jump_count': sharp_risk_jump_count,
    }
    return render(request, 'analytics/fraud_detection_dashboard.html', context)


@role_required(User.UserRole.ADMIN)
def verification_results(request):
    snapshots = _latest_seller_snapshots(limit=200)
    active_incidents = {
        incident.seller_id: incident
        for incident in SellerRiskIncident.objects.select_related('seller')
        .filter(is_active=True)
        .order_by('-created_at')
    }
    snapshot_rows = [
        {
            'snapshot': snapshot,
            'incident': active_incidents.get(snapshot.seller_id),
        }
        for snapshot in snapshots
    ]
    return render(
        request,
        'analytics/verification_results.html',
        {
            'snapshot_rows': snapshot_rows,
        },
    )


@role_required(User.UserRole.ADMIN)
def reports_dashboard(request):
    latest_snapshots = _latest_seller_snapshots(limit=5000)
    high_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.HIGH
    )
    medium_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.MEDIUM
    )
    low_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.LOW
    )

    context = {
        'total_bookings': Booking.objects.count(),
        'completed_bookings': Booking.objects.filter(status=Booking.BookingStatus.DELIVERED).count(),
        'total_payments': Transaction.objects.filter(status=Transaction.TransactionStatus.SUCCESS).count(),
        'total_complaints': Complaint.objects.count(),
        'total_feedback': Feedback.objects.count(),
        'flagged_sellers': SellerProfile.objects.filter(
            verification_status=SellerProfile.VerificationStatus.FLAGGED
        ).count(),
        'high_risk_sellers': high_risk_count,
        'medium_risk_sellers': medium_risk_count,
        'low_risk_sellers': low_risk_count,
        'snapshot_count': len(latest_snapshots),
    }
    return render(request, 'analytics/reports_dashboard.html', context)


@role_required(User.UserRole.ADMIN)
def reports_export_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="naturenest_admin_reports.csv"'
    writer = csv.writer(response)
    writer.writerow(['Metric', 'Value'])
    writer.writerow(['Total Bookings', Booking.objects.count()])
    writer.writerow(['Completed Bookings', Booking.objects.filter(status=Booking.BookingStatus.DELIVERED).count()])
    writer.writerow(['Total Payments', Transaction.objects.filter(status=Transaction.TransactionStatus.SUCCESS).count()])
    writer.writerow(['Total Complaints', Complaint.objects.count()])
    writer.writerow(['Total Feedback', Feedback.objects.count()])
    writer.writerow(
        [
            'Flagged Sellers',
            SellerProfile.objects.filter(verification_status=SellerProfile.VerificationStatus.FLAGGED).count(),
        ]
    )
    return response


@role_required(User.UserRole.ADMIN)
def reports_export_pdf(request):
    latest_snapshots = _latest_seller_snapshots(limit=5000)
    high_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.HIGH
    )
    medium_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.MEDIUM
    )
    low_risk_count = sum(
        1
        for snapshot in latest_snapshots
        if snapshot.classification_label == SellerRiskSnapshot.ClassificationLabel.LOW
    )

    rows = [
        ['Total Bookings', Booking.objects.count()],
        ['Completed Bookings', Booking.objects.filter(status=Booking.BookingStatus.DELIVERED).count()],
        ['Total Payments', Transaction.objects.filter(status=Transaction.TransactionStatus.SUCCESS).count()],
        ['Total Complaints', Complaint.objects.count()],
        ['Total Feedback', Feedback.objects.count()],
        [
            'Flagged Sellers',
            SellerProfile.objects.filter(verification_status=SellerProfile.VerificationStatus.FLAGGED).count(),
        ],
        ['High Risk Sellers', high_risk_count],
        ['Medium Risk Sellers', medium_risk_count],
        ['Low Risk Sellers', low_risk_count],
        ['Latest Verification Snapshots', len(latest_snapshots)],
    ]
    return _table_pdf_response(
        filename='naturenest_admin_reports.pdf',
        title='Nature Nest Admin Reports',
        subtitle=f'Generated at {timezone.now():%Y-%m-%d %H:%M}',
        headers=['Metric', 'Value'],
        rows=rows,
        use_landscape=False,
    )


@role_required(User.UserRole.ADMIN)
def verification_results_export_csv(request):
    snapshots = _latest_seller_snapshots(limit=5000)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="naturenest_seller_verification.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            'Seller',
            'Risk Score',
            'Classification',
            'Anomaly Score',
            'Complaint Ratio',
            'Failed Transaction Ratio',
            'Low Rating Ratio',
            'Cancellation Ratio',
            'Stale Pending Ratio',
            'Flagged',
            'Model Version',
            'Generated At',
            'Risk Factors',
            'Confidence Score',
            'Risk Velocity',
        ]
    )
    for snapshot in snapshots:
        writer.writerow(
            [
                snapshot.seller.display_name,
                f'{snapshot.risk_score:.2f}',
                snapshot.get_classification_label_display(),
                f'{snapshot.anomaly_score:.2f}',
                f'{snapshot.complaint_ratio:.4f}',
                f'{snapshot.failed_transaction_ratio:.4f}',
                f'{snapshot.low_rating_ratio:.4f}',
                f'{snapshot.cancellation_ratio:.4f}',
                f'{snapshot.stale_pending_ratio:.4f}',
                'Yes' if snapshot.is_flagged else 'No',
                snapshot.model_version,
                snapshot.created_at.isoformat(),
                '; '.join(snapshot.risk_factors or []),
                f'{snapshot.confidence_score:.2f}',
                f'{snapshot.risk_velocity:.2f}',
            ]
        )
    return response


@role_required(User.UserRole.ADMIN)
def verification_results_export_pdf(request):
    snapshots = _latest_seller_snapshots(limit=5000)
    rows = []
    for snapshot in snapshots:
        rows.append(
            [
                snapshot.seller.display_name,
                f'{snapshot.risk_score:.2f}',
                snapshot.get_classification_label_display(),
                f'{snapshot.anomaly_score:.2f}',
                f'{snapshot.complaint_ratio:.4f}',
                f'{snapshot.failed_transaction_ratio:.4f}',
                f'{snapshot.low_rating_ratio:.4f}',
                f'{snapshot.cancellation_ratio:.4f}',
                f'{snapshot.stale_pending_ratio:.4f}',
                'Yes' if snapshot.is_flagged else 'No',
                snapshot.model_version,
                snapshot.created_at.strftime('%Y-%m-%d %H:%M'),
            ]
        )
    return _table_pdf_response(
        filename='naturenest_seller_verification.pdf',
        title='Nature Nest Seller Verification',
        subtitle=f'Generated at {timezone.now():%Y-%m-%d %H:%M}',
        headers=[
            'Seller',
            'Risk',
            'Label',
            'Anomaly',
            'Complaint',
            'Failed TX',
            'Low Rating',
            'Cancel',
            'Stale',
            'Flagged',
            'Model',
            'Generated',
        ],
        rows=rows,
        use_landscape=True,
    )


@role_required(User.UserRole.ADMIN)
def fraud_detection_export_pdf(request):
    latest_snapshots = _latest_seller_snapshots(limit=300)
    rows = []
    for snapshot in latest_snapshots[:200]:
        rows.append(
            [
                snapshot.seller.display_name,
                f'{snapshot.risk_score:.2f}',
                snapshot.get_classification_label_display(),
                f'{snapshot.anomaly_score:.2f}',
                f'{snapshot.confidence_score:.2f}',
                f'{snapshot.risk_velocity:.2f}',
                snapshot.model_version,
                snapshot.created_at.strftime('%Y-%m-%d %H:%M'),
            ]
        )
    return _table_pdf_response(
        filename='naturenest_fraud_detection.pdf',
        title='Nature Nest Fraud Detection',
        subtitle=f'Generated at {timezone.now():%Y-%m-%d %H:%M}',
        headers=['Seller', 'Risk', 'Label', 'Anomaly', 'Confidence', 'Velocity', 'Model', 'Generated'],
        rows=rows,
        use_landscape=True,
    )


@role_required(User.UserRole.ADMIN)
def risk_incident_queue(request):
    incidents = SellerRiskIncident.objects.select_related('seller', 'snapshot').order_by('-created_at')[:200]
    return render(
        request,
        'analytics/risk_incident_queue.html',
        {
            'incidents': incidents,
            'decision_form': AdminRiskFinalDecisionForm(),
        },
    )


@role_required(User.UserRole.ADMIN)
def risk_incident_export_pdf(request):
    incidents = SellerRiskIncident.objects.select_related('seller').order_by('-created_at')[:1000]
    rows = []
    for incident in incidents:
        rows.append(
            [
                f'#{incident.id}',
                incident.seller.display_name,
                incident.get_status_display(),
                f'{incident.risk_score:.2f}',
                f'INR {incident.fine_amount}',
                'Yes' if incident.is_active else 'No',
                incident.created_at.strftime('%Y-%m-%d %H:%M'),
            ]
        )
    return _table_pdf_response(
        filename='naturenest_risk_incidents.pdf',
        title='Nature Nest Risk Incident Queue',
        subtitle=f'Generated at {timezone.now():%Y-%m-%d %H:%M}',
        headers=['Incident', 'Seller', 'Status', 'Risk', 'Fine', 'Active', 'Created'],
        rows=rows,
        use_landscape=True,
    )


@role_required(User.UserRole.ADMIN)
def risk_incident_reverify(request, incident_id):
    if request.method != 'POST':
        return redirect('analytics:risk_incident_queue')

    incident = get_object_or_404(SellerRiskIncident.objects.select_related('seller'), id=incident_id)
    if not incident.is_active:
        messages.info(request, 'This risk incident is already closed.')
        return redirect('analytics:risk_incident_queue')
    snapshot = calculate_seller_risk(incident.seller)
    if snapshot is None:
        messages.error(request, 'Unable to generate fresh verification snapshot for this seller.')
        return redirect('analytics:risk_incident_queue')

    incident.snapshot = snapshot
    incident.risk_score = snapshot.risk_score
    incident.classification_label = snapshot.classification_label
    incident.incident_reason = '; '.join(snapshot.risk_factors or [])
    incident.status = SellerRiskIncident.IncidentStatus.UNDER_REVIEW
    incident.save(
        update_fields=[
            'snapshot',
            'risk_score',
            'classification_label',
            'incident_reason',
            'status',
            'updated_at',
        ]
    )
    messages.success(
        request,
        (
            f'Seller {incident.seller.display_name} re-verified. '
            f'Latest risk: {snapshot.get_classification_label_display()} ({snapshot.risk_score:.2f}).'
        ),
    )
    return redirect('analytics:risk_incident_queue')


@role_required(User.UserRole.ADMIN)
def risk_incident_finalize(request, incident_id):
    if request.method != 'POST':
        return redirect('analytics:risk_incident_queue')

    incident = get_object_or_404(SellerRiskIncident.objects.select_related('seller'), id=incident_id)
    if not incident.is_active:
        messages.info(request, 'This risk incident is already closed.')
        return redirect('analytics:risk_incident_queue')
    form = AdminRiskFinalDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Enter a valid final decision for this incident.')
        return redirect('analytics:risk_incident_queue')

    decision = form.cleaned_data['decision']
    decision_note = (form.cleaned_data['decision_note'] or '').strip()
    waive_fine = bool(form.cleaned_data.get('waive_fine'))

    if waive_fine and decision != AdminRiskFinalDecisionForm.DECISION_UNFREEZE:
        messages.error(request, 'Penalty removal is available only when unfreezing after appeal validation.')
        return redirect('analytics:risk_incident_queue')
    if waive_fine and not (incident.appeal_text or '').strip():
        messages.error(request, 'Penalty can be removed only after seller appeal is submitted.')
        return redirect('analytics:risk_incident_queue')
    if waive_fine:
        decision_note = (
            f'{decision_note}\nPenalty waived after appeal validation.'
            if decision_note
            else 'Penalty waived after appeal validation.'
        )

    if decision == AdminRiskFinalDecisionForm.DECISION_UNFREEZE:
        unfreeze_seller_operations(incident.seller, decision_note=decision_note)
        incident.status = SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN
        if waive_fine:
            incident.fine_amount = 0
            incident.fine_paid_at = None
            incident.risk_score = 0.0
            incident.classification_label = SellerRiskSnapshot.ClassificationLabel.LOW
            messages.success(
                request,
                (
                    f'Seller {incident.seller.display_name} has been unfrozen. '
                    'Penalty fine and incident high-risk score were removed after appeal validation.'
                ),
            )
        else:
            messages.success(request, f'Seller {incident.seller.display_name} has been unfrozen.')
    elif decision == AdminRiskFinalDecisionForm.DECISION_TERMINATE:
        terminate_seller_operations(incident.seller, decision_note=decision_note)
        incident.status = SellerRiskIncident.IncidentStatus.RESOLVED_TERMINATED
        messages.success(request, f'Seller {incident.seller.display_name} has been terminated.')
    else:
        freeze_seller_operations(incident.seller, incident_note=decision_note)
        if incident.fine_amount > 0 and incident.fine_paid_at is None:
            incident.status = SellerRiskIncident.IncidentStatus.FROZEN_FINE_PENDING
            incident.admin_decision_note = decision_note
            incident.final_decision_at = None
            incident.is_active = True
            incident.save(
                update_fields=[
                    'status',
                    'admin_decision_note',
                    'final_decision_at',
                    'is_active',
                    'updated_at',
                ]
            )
            messages.warning(
                request,
                (
                    f'Seller {incident.seller.display_name} has not paid the required fine. '
                    'Account remains frozen.'
                ),
            )
            return redirect('analytics:risk_incident_queue')
        else:
            incident.status = SellerRiskIncident.IncidentStatus.RESOLVED_FROZEN
            messages.success(request, f'Seller {incident.seller.display_name} remains frozen.')

    incident.admin_decision_note = decision_note
    incident.final_decision_at = timezone.now()
    incident.is_active = False
    incident.save(
        update_fields=[
            'status',
            'admin_decision_note',
            'final_decision_at',
            'is_active',
            'fine_amount',
            'fine_paid_at',
            'risk_score',
            'classification_label',
            'updated_at',
        ]
    )
    return redirect('analytics:risk_incident_queue')


@role_required(User.UserRole.SELLER)
def seller_risk_incident(request):
    active_incident = (
        SellerRiskIncident.objects.select_related('snapshot')
        .filter(seller=request.user, is_active=True)
        .order_by('-created_at')
        .first()
    )
    incident = active_incident
    if incident is None:
        incident = (
            SellerRiskIncident.objects.select_related('snapshot')
            .filter(seller=request.user)
            .order_by('-created_at')
            .first()
        )
    appeal_form = SellerRiskAppealForm()
    payment_form = SellerFinePaymentForm()
    allow_seller_actions = bool(
        incident
        and incident.is_active
        and incident.status != SellerRiskIncident.IncidentStatus.RESOLVED_TERMINATED
    )
    return render(
        request,
        'analytics/seller_risk_incident.html',
        {
            'incident': incident,
            'appeal_form': appeal_form,
            'payment_form': payment_form,
            'allow_seller_actions': allow_seller_actions,
        },
    )


@role_required(User.UserRole.SELLER)
def seller_risk_pay_fine(request, incident_id):
    if request.method != 'POST':
        return redirect('analytics:seller_risk_incident')

    incident = get_object_or_404(
        SellerRiskIncident.objects.select_related('seller'),
        id=incident_id,
        seller=request.user,
        is_active=True,
    )
    if incident.fine_amount <= 0:
        messages.error(request, 'No fine is pending for this incident.')
        return redirect('analytics:seller_risk_incident')
    if incident.status == SellerRiskIncident.IncidentStatus.FINE_PAID:
        messages.info(request, 'Fine is already marked as paid for this incident.')
        return redirect('analytics:seller_risk_incident')

    payment_form = SellerFinePaymentForm(request.POST)
    if not payment_form.is_valid():
        messages.error(request, 'Enter valid fine payment details.')
        return render(
            request,
            'analytics/seller_risk_incident.html',
            {
                'incident': incident,
                'appeal_form': SellerRiskAppealForm(),
                'payment_form': payment_form,
                'allow_seller_actions': bool(
                    incident
                    and incident.is_active
                    and incident.status != SellerRiskIncident.IncidentStatus.RESOLVED_TERMINATED
                ),
            },
            status=400,
        )

    payment_method = payment_form.cleaned_data['payment_method']
    payment_method_label = dict(SellerFinePaymentForm.PAYMENT_METHOD_CHOICES).get(
        payment_method,
        'selected method',
    )
    now = timezone.now()
    incident.fine_paid_at = now
    incident.status = SellerRiskIncident.IncidentStatus.RESOLVED_UNFROZEN
    incident.is_active = False
    incident.final_decision_at = now
    incident.admin_decision_note = (
        f'{incident.admin_decision_note}\nAuto-unfrozen after seller paid fine via {payment_method_label}.'
        if incident.admin_decision_note
        else f'Auto-unfrozen after seller paid fine via {payment_method_label}.'
    )
    incident.save(
        update_fields=[
            'fine_paid_at',
            'status',
            'is_active',
            'final_decision_at',
            'admin_decision_note',
            'updated_at',
        ]
    )
    unfreeze_seller_operations(
        incident.seller,
        decision_note=f'Auto-unfrozen after fine payment via {payment_method_label}.',
    )
    messages.success(
        request,
        f'Fine paid via {payment_method_label}. Seller account has been automatically unfrozen.',
    )
    return redirect('analytics:seller_risk_incident')


@role_required(User.UserRole.SELLER)
def seller_risk_submit_appeal(request, incident_id):
    if request.method != 'POST':
        return redirect('analytics:seller_risk_incident')

    incident = get_object_or_404(
        SellerRiskIncident.objects.select_related('seller'),
        id=incident_id,
        seller=request.user,
        is_active=True,
    )
    form = SellerRiskAppealForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Enter a valid appeal message.')
        return redirect('analytics:seller_risk_incident')

    incident.appeal_text = form.cleaned_data['appeal_text']
    incident.appealed_at = timezone.now()
    incident.seller_acknowledged_at = timezone.now()
    incident.status = SellerRiskIncident.IncidentStatus.APPEALED
    incident.save(
        update_fields=[
            'appeal_text',
            'appealed_at',
            'seller_acknowledged_at',
            'status',
            'updated_at',
        ]
    )
    messages.success(request, 'Appeal submitted. Admin re-verification is pending.')
    messages.warning(
        request,
        'Seller account remains frozen. Pay the fine to auto-unfreeze, or wait for admin penalty waiver.',
    )
    return redirect('analytics:seller_risk_incident')

# Create your views here.
