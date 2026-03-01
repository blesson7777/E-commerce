from django.contrib.auth import views as auth_views
from django.urls import path

from accounts import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.UserLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('signup/customer/', views.customer_signup, name='customer_signup'),
    path('signup/seller/', views.seller_signup, name='seller_signup'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('notifications/mark-all/', views.mark_all_notifications_read, name='mark_all_notifications_read'),
    path(
        'seller-risk/acknowledge/',
        views.acknowledge_seller_risk_action,
        name='acknowledge_seller_risk_action',
    ),
    path('search/', views.search_results, name='search_results'),
    path('dashboard/product-preview/<int:product_id>/', views.dashboard_product_preview, name='dashboard_product_preview'),
    path('search/suggestions/', views.search_suggestions, name='search_suggestions'),
    path('profile/', views.profile_view, name='profile'),
    path('profile/update/', views.profile_update, name='profile_update'),
    path('profile/delete/', views.delete_account, name='delete_account'),
    path('profile/addresses/', views.manage_addresses, name='manage_addresses'),
    path('profile/addresses/<int:address_id>/edit/', views.edit_address, name='edit_address'),
    path('profile/addresses/<int:address_id>/toggle/', views.toggle_address_status, name='toggle_address_status'),
    path('profile/addresses/<int:address_id>/default/', views.set_default_address, name='set_default_address'),
    path('promote/seller/', views.promote_to_seller, name='promote_to_seller'),
    path('sellers/new/', views.admin_add_seller, name='admin_add_seller'),
    path(
        'password/change/',
        auth_views.PasswordChangeView.as_view(template_name='accounts/password_change_form.html'),
        name='password_change',
    ),
    path(
        'password/change/done/',
        auth_views.PasswordChangeDoneView.as_view(template_name='accounts/password_change_done.html'),
        name='password_change_done',
    ),
    path(
        'password/forgot/',
        views.password_reset_otp_request,
        name='password_reset',
    ),
    path(
        'password/forgot/done/',
        views.password_reset_otp_done,
        name='password_reset_done',
    ),
    path(
        'password/forgot/verify/',
        views.password_reset_otp_confirm,
        name='password_reset_confirm',
    ),
    path(
        'password/reset/complete/',
        views.password_reset_otp_complete,
        name='password_reset_complete',
    ),
]
