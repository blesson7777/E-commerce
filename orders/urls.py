from django.urls import path

from orders import views

app_name = 'orders'

urlpatterns = [
    path('cart/checkout/', views.cart_checkout, name='cart_checkout'),
    path('cart/payment/', views.cart_checkout_payment, name='cart_checkout_payment'),
    path('public-delivery-status/', views.public_delivery_status_update, name='public_delivery_status_update'),
    path('cancellations/review/', views.cancellation_monitor, name='cancellation_monitor'),
    path('create/<int:product_id>/', views.create_booking, name='create_booking'),
    path('list/', views.booking_list, name='booking_list'),
    path('<int:booking_id>/', views.booking_detail, name='booking_detail'),
    path('<int:booking_id>/cancel/', views.cancel_booking, name='cancel_booking'),
    path(
        '<int:booking_id>/cancellation-impact/',
        views.review_booking_cancellation,
        name='review_booking_cancellation',
    ),
    path('<int:booking_id>/receipt/', views.booking_receipt, name='booking_receipt'),
    path('<int:booking_id>/status/', views.update_booking_status, name='update_booking_status'),
    path(
        '<int:booking_id>/confirm-delivered/',
        views.confirm_booking_delivered,
        name='confirm_booking_delivered',
    ),
    path('<int:booking_id>/pay/', views.create_transaction, name='create_transaction'),
    path(
        '<int:booking_id>/payment-success/<int:transaction_id>/',
        views.transaction_success,
        name='transaction_success',
    ),
    path('transactions/', views.transaction_list, name='transaction_list'),
    path('transactions/<int:transaction_id>/', views.transaction_detail, name='transaction_detail'),
]
