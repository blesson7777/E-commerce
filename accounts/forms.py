import re

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.utils.text import get_valid_filename

from config.form_mixins import AppStyledFormMixin
from accounts.models import CustomerAddress
from accounts.models import User
from locations.models import Location


AUTH_INPUT_CLASS = (
    'w-full rounded-lg border border-gray-300 text-gray-900 px-3 py-2 '
    'focus:shadow-[0_0_0_.25rem_rgba(10,173,10,.25)] focus:ring-green-600 focus:ring-0 focus:border-green-600'
)
ALLOWED_PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
MAX_PROFILE_PHOTO_SIZE = 5 * 1024 * 1024
PERSON_NAME_RE = re.compile(r"^[A-Za-z]+(?:[ '-][A-Za-z]+)*$")


def _clean_person_name(value, label):
    value = ' '.join((value or '').split())
    if len(value) < 2:
        raise forms.ValidationError(f'{label} must be at least 2 characters.')
    if not PERSON_NAME_RE.fullmatch(value):
        raise forms.ValidationError(
            f"{label} may contain only letters, spaces, apostrophes, and hyphens."
        )
    return value


def _save_profile_photo(upload):
    base_name = get_valid_filename(upload.name or 'profile-photo')
    if '.' in base_name:
        stem, ext = base_name.rsplit('.', 1)
        ext = f'.{ext.lower()}'
    else:
        stem, ext = base_name, ''
    if ext not in ALLOWED_PHOTO_EXTENSIONS:
        raise ValidationError('Upload a JPG, JPEG, PNG, WEBP, or GIF image.')

    safe_stem = (stem or 'profile-photo')[:50]
    relative_path = default_storage.save(f'profile_photos/{safe_stem}{ext}', upload)
    return default_storage.url(relative_path)


class UserTemplateStyleMixin:
    def _apply_user_template_styles(self):
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                continue
            field.widget.attrs['class'] = AUTH_INPUT_CLASS
            field.widget.attrs.setdefault('placeholder', field.label or name.replace('_', ' ').title())


class ProfilePhotoUploadMixin:
    def clean_profile_photo(self):
        upload = self.cleaned_data.get('profile_photo')
        if not upload:
            return upload

        if upload.size > MAX_PROFILE_PHOTO_SIZE:
            raise ValidationError('Profile picture must be 5 MB or smaller.')

        content_type = (getattr(upload, 'content_type', '') or '').lower()
        if content_type and not content_type.startswith('image/'):
            raise ValidationError('Please upload a valid image file.')

        return upload

    def resolve_profile_photo_url(self):
        upload = self.cleaned_data.get('profile_photo')
        if upload:
            return _save_profile_photo(upload)
        return (self.cleaned_data.get('profile_photo_url') or '').strip()


class StyledAuthenticationForm(UserTemplateStyleMixin, AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_user_template_styles()
        username_field = self.fields.get('username')
        if username_field:
            username_field.label = 'Email'
            username_field.widget.attrs['placeholder'] = 'Email'
            username_field.widget.attrs['autocomplete'] = 'email'


class ForgotPasswordOTPRequestForm(AppStyledFormMixin, forms.Form):
    email = forms.EmailField(label='Registered email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.apply_widget_styles()
        self.fields['email'].widget.attrs['autocomplete'] = 'email'

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        self.user = User.objects.filter(email__iexact=email, is_active=True).first()
        if not self.user:
            raise forms.ValidationError('No active account found with this email address.')
        return email


class ForgotPasswordOTPVerifyForm(AppStyledFormMixin, forms.Form):
    otp = forms.CharField(
        label='6-digit OTP',
        min_length=6,
        max_length=6,
        help_text='Enter the OTP sent to your email address.',
    )
    new_password1 = forms.CharField(
        label='New password',
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    new_password2 = forms.CharField(
        label='Confirm new password',
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()
        self.fields['otp'].widget.attrs['autocomplete'] = 'one-time-code'
        self.fields['otp'].widget.attrs['inputmode'] = 'numeric'

    def clean_otp(self):
        otp = (self.cleaned_data.get('otp') or '').strip()
        if not otp.isdigit():
            raise forms.ValidationError('OTP must contain only digits.')
        return otp

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('new_password1') or ''
        password2 = cleaned_data.get('new_password2') or ''
        if not password1 or not password2:
            return cleaned_data
        if password1 != password2:
            self.add_error('new_password2', 'Passwords do not match.')
            return cleaned_data
        try:
            validate_password(password1, user=self.user)
        except ValidationError as exc:
            self.add_error('new_password1', exc)
        return cleaned_data


class CustomerSignUpForm(ProfilePhotoUploadMixin, UserTemplateStyleMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=True)
    email = forms.EmailField(required=True)
    profile_photo = forms.FileField(
        required=False,
        label='Profile Picture',
        widget=forms.ClearableFileInput(attrs={'accept': 'image/*'}),
        help_text='Optional. Upload JPG, JPEG, PNG, WEBP, or GIF (max 5 MB).',
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('first_name', 'last_name', 'email', 'phone_number')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields([
            'first_name',
            'last_name',
            'email',
            'phone_number',
            'profile_photo',
            'password1',
            'password2',
        ])
        self._apply_user_template_styles()
        self.fields['phone_number'].required = True
        self.fields['first_name'].widget.attrs.update(
            {
                'autocomplete': 'given-name',
                'pattern': r"[A-Za-z]+(?:[ '-][A-Za-z]+)*",
                'title': 'Use letters only (spaces, apostrophes, and hyphens are allowed).',
            }
        )
        self.fields['last_name'].widget.attrs.update(
            {
                'autocomplete': 'family-name',
                'pattern': r"[A-Za-z]+(?:[ '-][A-Za-z]+)*",
                'title': 'Use letters only (spaces, apostrophes, and hyphens are allowed).',
            }
        )
        self.fields['email'].widget.attrs['autocomplete'] = 'email'
        self.fields['phone_number'].widget.attrs.update(
            {
                'autocomplete': 'tel',
                'inputmode': 'tel',
                'pattern': r"\+?[0-9]{10,12}",
                'title': 'Enter 10-12 digits with an optional + prefix.',
                'placeholder': '+919876543210',
            }
        )

    def clean_first_name(self):
        return _clean_person_name(self.cleaned_data.get('first_name'), 'First name')

    def clean_last_name(self):
        return _clean_person_name(self.cleaned_data.get('last_name'), 'Last name')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean_phone_number(self):
        phone_number = AppStyledFormMixin.clean_phone(self.cleaned_data.get('phone_number'))
        if not phone_number:
            raise forms.ValidationError('Phone number is required.')
        return phone_number

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data['first_name'].strip()
        user.last_name = self.cleaned_data['last_name'].strip()
        user.email = self.cleaned_data['email']
        user.profile_photo_url = self.resolve_profile_photo_url()
        user.role = User.UserRole.CUSTOMER
        if commit:
            user.save()
        return user


class SellerSignUpForm(ProfilePhotoUploadMixin, UserTemplateStyleMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=True)
    email = forms.EmailField(required=True)
    profile_photo = forms.FileField(
        required=False,
        label='Profile Picture',
        widget=forms.ClearableFileInput(attrs={'accept': 'image/*'}),
        help_text='Optional. Upload JPG, JPEG, PNG, WEBP, or GIF (max 5 MB).',
    )
    store_name = forms.CharField(max_length=120)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('first_name', 'last_name', 'email', 'phone_number')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields([
            'first_name',
            'last_name',
            'email',
            'phone_number',
            'store_name',
            'profile_photo',
            'password1',
            'password2',
        ])
        self._apply_user_template_styles()
        self.fields['phone_number'].required = True
        self.fields['first_name'].widget.attrs.update(
            {
                'autocomplete': 'given-name',
                'pattern': r"[A-Za-z]+(?:[ '-][A-Za-z]+)*",
                'title': 'Use letters only (spaces, apostrophes, and hyphens are allowed).',
            }
        )
        self.fields['last_name'].widget.attrs.update(
            {
                'autocomplete': 'family-name',
                'pattern': r"[A-Za-z]+(?:[ '-][A-Za-z]+)*",
                'title': 'Use letters only (spaces, apostrophes, and hyphens are allowed).',
            }
        )
        self.fields['email'].widget.attrs['autocomplete'] = 'email'
        self.fields['phone_number'].widget.attrs.update(
            {
                'autocomplete': 'tel',
                'inputmode': 'tel',
                'pattern': r"\+?[0-9]{10,15}",
                'title': 'Enter 10-15 digits with an optional + prefix.',
                'placeholder': '+919876543210',
            }
        )

    def clean_first_name(self):
        return _clean_person_name(self.cleaned_data.get('first_name'), 'First name')

    def clean_last_name(self):
        return _clean_person_name(self.cleaned_data.get('last_name'), 'Last name')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean_phone_number(self):
        phone_number = AppStyledFormMixin.clean_phone(self.cleaned_data.get('phone_number'))
        if not phone_number:
            raise forms.ValidationError('Phone number is required.')
        return phone_number

    def clean_store_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('store_name'), label='Store name', min_len=3)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data['first_name'].strip()
        user.last_name = self.cleaned_data['last_name'].strip()
        user.email = self.cleaned_data['email']
        user.profile_photo_url = self.resolve_profile_photo_url()
        user.role = User.UserRole.SELLER
        if commit:
            user.save()
            user.seller_profile.store_name = self.cleaned_data['store_name']
            user.seller_profile.save(update_fields=['store_name'])
        return user


class ProfileUpdateForm(ProfilePhotoUploadMixin, AppStyledFormMixin, forms.ModelForm):
    profile_photo = forms.FileField(
        required=False,
        label='Profile Picture',
        widget=forms.ClearableFileInput(attrs={'accept': 'image/*'}),
        help_text='Upload a new image (max 5 MB). Leave blank to keep current photo.',
    )

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'username', 'email', 'phone_number')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['username'].required = True
        self.apply_widget_styles()
        self.fields['username'].help_text = 'Unique login username.'

    def clean_first_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('first_name'), label='First name', min_len=2)

    def clean_last_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('last_name'), label='Last name', min_len=2)

    def clean_username(self):
        username = (self.cleaned_data.get('username') or '').strip()
        if not username:
            raise forms.ValidationError('Username is required.')
        existing = User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError('This username is already in use.')
        return username

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        existing = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError('This email is already in use.')
        return email

    def clean_phone_number(self):
        return AppStyledFormMixin.clean_phone(self.cleaned_data.get('phone_number'))

    def save(self, commit=True):
        user = super().save(commit=False)
        upload = self.cleaned_data.get('profile_photo')
        if upload:
            user.profile_photo_url = _save_profile_photo(upload)
        if commit:
            user.save()
        return user


class CustomerAddressForm(AppStyledFormMixin, forms.ModelForm):
    pincode = forms.CharField(max_length=20, required=True, label='Pincode')

    class Meta:
        model = CustomerAddress
        fields = ('label', 'address', 'is_default')
        widgets = {
            'address': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.location_id:
            self.fields['pincode'].initial = self.instance.location.postal_code
        self.apply_widget_styles()
        self.fields['label'].help_text = 'Example: Home, Work, Office'
        self.fields['pincode'].help_text = 'Enter a serviceable pincode.'
        self.fields['address'].help_text = 'Enter complete address details with landmark.'
        self.fields['is_default'].help_text = 'Use as your default saved address.'

    def clean_label(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('label'), label='Address label', min_len=2)

    def clean_address(self):
        address = (self.cleaned_data.get('address') or '').strip()
        if len(address) < 10:
            raise forms.ValidationError('Address must be at least 10 characters.')
        return address

    def clean_pincode(self):
        pincode = (self.cleaned_data.get('pincode') or '').strip()
        if not pincode:
            raise forms.ValidationError('Enter a pincode.')
        location = (
            Location.objects.select_related('district', 'district__state')
            .filter(
                postal_code__iexact=pincode,
                is_active=True,
                district__is_active=True,
                district__state__is_active=True,
            )
            .order_by('district__state__name', 'district__name', 'name')
            .first()
        )
        if not location:
            raise forms.ValidationError('This pincode is unavailable for delivery.')
        self.cleaned_data['resolved_location'] = location
        return pincode

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.location = self.cleaned_data.get('resolved_location')
        if commit:
            instance.save()
        return instance


class AccountDeletionForm(AppStyledFormMixin, forms.Form):
    password = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()
        self.fields['password'].help_text = 'Enter your current password to permanently delete your account.'

    def clean_password(self):
        password = self.cleaned_data.get('password') or ''
        if not self.user or not self.user.is_authenticated or not self.user.check_password(password):
            raise forms.ValidationError('Incorrect password.')
        return password
