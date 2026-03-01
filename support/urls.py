from django.urls import path

from support import views

app_name = 'support'

urlpatterns = [
    path('complaints/new/', views.complaint_create, name='complaint_create'),
    path('complaints/', views.complaint_list, name='complaint_list'),
    path('complaints/<int:complaint_id>/', views.complaint_detail, name='complaint_detail'),
    path('complaints/<int:complaint_id>/status/', views.complaint_update_status, name='complaint_update_status'),
    path('feedback/new/', views.feedback_create, name='feedback_create'),
    path('feedback/', views.feedback_list, name='feedback_list'),
]
