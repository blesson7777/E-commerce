from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from accounts.models import CustomerProfile
from accounts.models import SellerProfile
from accounts.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('username', 'email', 'role', 'is_staff', 'is_active', 'last_login')
    list_filter = ('role', 'is_staff', 'is_superuser', 'is_active')
    fieldsets = BaseUserAdmin.fieldsets + (
        (
            'Nature Nest',
            {
                'fields': (
                    'role',
                    'phone_number',
                    'is_email_verified',
                    'created_at',
                    'updated_at',
                )
            },
        ),
    )
    readonly_fields = ('created_at', 'updated_at')


@admin.register(SellerProfile)
class SellerProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'store_name', 'verification_status', 'risk_score', 'total_sales')
    list_filter = ('verification_status',)
    search_fields = ('user__username', 'store_name', 'business_license_number')


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'district', 'location', 'created_at')
    search_fields = ('user__username', 'user__email')

# Register your models here.
