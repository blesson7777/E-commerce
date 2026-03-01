from django.conf import settings
from django.conf.urls.static import static
from django.urls import include
from django.urls import path

from accounts import views as account_views

urlpatterns = [
    path('', account_views.home, name='home'),
    path('accounts/', include('accounts.urls')),
    path('locations/', include('locations.urls')),
    path('catalog/', include('catalog.urls')),
    path('orders/', include('orders.urls')),
    path('support/', include('support.urls')),
    path('analytics/', include('analytics.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
