from django.urls import path

from locations import views

app_name = 'locations'

urlpatterns = [
    path('states/', views.state_list_create, name='state_list'),
    path('states/<int:state_id>/edit/', views.state_edit, name='state_edit'),
    path(
        'states/<int:state_id>/toggle-availability/',
        views.state_toggle_availability,
        name='state_toggle_availability',
    ),
    path('districts/', views.district_list_create, name='district_list'),
    path('districts/<int:district_id>/edit/', views.district_edit, name='district_edit'),
    path(
        'districts/<int:district_id>/toggle-availability/',
        views.district_toggle_availability,
        name='district_toggle_availability',
    ),
    path('places/', views.location_list_create, name='location_list'),
    path('places/non-servicing/', views.non_servicing_pincode_list, name='non_servicing_pincode_list'),
    path('places/<int:location_id>/edit/', views.location_edit, name='location_edit'),
    path(
        'places/<int:location_id>/toggle-availability/',
        views.location_toggle_availability,
        name='location_toggle_availability',
    ),
]
