from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.utils.text import slugify


class UserManager(DjangoUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set.')
        email = self.normalize_email(email).strip().lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role', self.model.UserRole.ADMIN)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class UserRole(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        SELLER = 'seller', 'Seller'
        CUSTOMER = 'customer', 'Customer'

    email = models.EmailField('email address', unique=True)
    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.CUSTOMER)
    phone_number = models.CharField(max_length=20, blank=True)
    profile_photo_url = models.URLField(blank=True)
    is_email_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    @property
    def display_name(self):
        full_name = self.get_full_name().strip()
        return full_name or self.email or self.username

    def _ensure_generated_username(self):
        if self.username:
            return
        local_part = (self.email or '').split('@', 1)[0]
        base = slugify(local_part) or 'user'
        base = base[:150]
        candidate = base
        counter = 1
        while type(self).objects.exclude(pk=self.pk).filter(username__iexact=candidate).exists():
            suffix = f'-{counter}'
            candidate = f'{base[: 150 - len(suffix)]}{suffix}'
            counter += 1
        self.username = candidate

    def save(self, *args, **kwargs):
        self.email = (self.email or '').strip().lower()
        self.first_name = (self.first_name or '').strip()
        self.last_name = (self.last_name or '').strip()
        self._ensure_generated_username()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.display_name} ({self.get_role_display()})'


class SellerProfile(models.Model):
    class VerificationStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        VERIFIED = 'verified', 'Verified'
        REJECTED = 'rejected', 'Rejected'
        FLAGGED = 'flagged', 'Flagged'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='seller_profile',
        limit_choices_to={'role': User.UserRole.SELLER},
    )
    store_name = models.CharField(max_length=120)
    business_license_number = models.CharField(max_length=80, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
    )
    is_suspended = models.BooleanField(default=False)
    suspension_note = models.TextField(blank=True)
    risk_score = models.FloatField(default=0.0)
    total_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'SellerProfile<{self.user.display_name}>'


class CustomerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='customer_profile',
        limit_choices_to={'role': User.UserRole.CUSTOMER},
    )
    district = models.ForeignKey(
        'locations.District',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_profiles',
    )
    location = models.ForeignKey(
        'locations.Location',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_profiles',
    )
    address = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'CustomerProfile<{self.user.display_name}>'


class CustomerAddress(models.Model):
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='saved_addresses',
        limit_choices_to={'role': User.UserRole.CUSTOMER},
    )
    label = models.CharField(max_length=40, default='Home')
    address = models.TextField()
    location = models.ForeignKey(
        'locations.Location',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='saved_customer_addresses',
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', 'label', '-updated_at']

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            type(self).objects.filter(customer=self.customer).exclude(pk=self.pk).update(is_default=False)

    def __str__(self):
        return f'{self.customer.display_name}: {self.label}'

# Create your models here.
