from django.urls import path

from catalog import views

app_name = 'catalog'

urlpatterns = [
    path('categories/', views.category_list_create, name='category_list'),
    path('categories/<int:category_id>/edit/', views.category_edit, name='category_edit'),
    path('categories/<int:category_id>/delete/', views.category_delete, name='category_delete'),
    path(
        'categories/<int:category_id>/toggle-availability/',
        views.category_toggle_availability,
        name='category_toggle_availability',
    ),
    path('products/', views.product_list, name='product_list'),
    path('products/<int:product_id>/', views.product_detail, name='product_detail'),
    path('cart/add/<int:product_id>/', views.cart_add, name='cart_add'),
    path('cart/update/<int:product_id>/', views.cart_update, name='cart_update'),
    path('cart/remove/<int:product_id>/', views.cart_remove, name='cart_remove'),
    path('seller/inventory/', views.seller_inventory, name='seller_inventory'),
    path('seller/restocking/', views.seller_restock_dashboard, name='seller_restock_dashboard'),
    path('seller/inventory/<int:product_id>/edit/', views.seller_product_edit, name='seller_product_edit'),
    path('seller/inventory/<int:product_id>/delete/', views.seller_product_delete, name='seller_product_delete'),
    path(
        'seller/inventory/<int:product_id>/update-stock/',
        views.seller_product_update_stock,
        name='seller_product_update_stock',
    ),
    path(
        'seller/inventory/<int:product_id>/toggle-availability/',
        views.seller_product_toggle_availability,
        name='seller_product_toggle_availability',
    ),
    path(
        'admin/products/<int:product_id>/toggle-availability/',
        views.admin_product_toggle_availability,
        name='admin_product_toggle_availability',
    ),
    path(
        'admin/products/<int:product_id>/delete/',
        views.admin_product_delete,
        name='admin_product_delete',
    ),
]
