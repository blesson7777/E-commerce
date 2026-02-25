from django.urls import path

from analytics import views

app_name = 'analytics'

urlpatterns = [
    path('fraud-detection/', views.fraud_detection_dashboard, name='fraud_detection_dashboard'),
    path('fraud-detection/export/pdf/', views.fraud_detection_export_pdf, name='fraud_detection_export_pdf'),
    path('seller-verification/', views.run_seller_verification, name='run_seller_verification'),
    path('seller-verification/results/', views.verification_results, name='verification_results'),
    path(
        'seller-verification/results/export/pdf/',
        views.verification_results_export_pdf,
        name='verification_results_export_pdf',
    ),
    path('risk-incidents/', views.risk_incident_queue, name='risk_incident_queue'),
    path('risk-incidents/export/pdf/', views.risk_incident_export_pdf, name='risk_incident_export_pdf'),
    path('risk-incidents/<int:incident_id>/reverify/', views.risk_incident_reverify, name='risk_incident_reverify'),
    path('risk-incidents/<int:incident_id>/finalize/', views.risk_incident_finalize, name='risk_incident_finalize'),
    path('seller-risk/incident/', views.seller_risk_incident, name='seller_risk_incident'),
    path('seller-risk/incident/<int:incident_id>/pay-fine/', views.seller_risk_pay_fine, name='seller_risk_pay_fine'),
    path(
        'seller-risk/incident/<int:incident_id>/appeal/',
        views.seller_risk_submit_appeal,
        name='seller_risk_submit_appeal',
    ),
    path(
        'seller-verification/results/export/csv/',
        views.verification_results_export_csv,
        name='verification_results_export_csv',
    ),
    path('reports/', views.reports_dashboard, name='reports_dashboard'),
    path('reports/export/csv/', views.reports_export_csv, name='reports_export_csv'),
    path('reports/export/pdf/', views.reports_export_pdf, name='reports_export_pdf'),
]
