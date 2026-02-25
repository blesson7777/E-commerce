from django import forms
from django.utils import timezone

from accounts.models import CustomerAddress
from accounts.models import User
from config.form_mixins import AppStyledFormMixin
from locations.models import Location
from orders.models import Booking
from orders.models import Transaction


class PreviousBookingAddressChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        location = obj.location
        if location and location.district_id and location.district.state_id:
            location_label = (
                f'{location.postal_code} - {location.name}, {location.district.name}, {location.district.state.name}'
            )
        elif location:
            location_label = f'{location.postal_code} - {location.name}'
        else:
            location_label = 'Unknown pincode'

        first_line = (obj.address or '').strip().splitlines()[0] if obj.address else ''
        if len(first_line) > 70:
            first_line = f'{first_line[:67]}...'

        return f'{obj.label} - {location_label} - {first_line}'


def _is_active_delivery_location(location):
    if not location or not location.district_id:
        return False
    district = location.district
    state = district.state if district and district.state_id else None
    return bool(location.is_active and district.is_active and (state.is_active if state else True))


def _seller_allowed_statuses(current_status):
    mapping = {
        Booking.BookingStatus.PENDING: {
            Booking.BookingStatus.PENDING,
            Booking.BookingStatus.CANCELLED,
        },
        Booking.BookingStatus.CONFIRMED: {
            Booking.BookingStatus.CONFIRMED,
            Booking.BookingStatus.SHIPPED,
            Booking.BookingStatus.OUT_FOR_DELIVERY,
            Booking.BookingStatus.CANCELLED,
        },
        Booking.BookingStatus.SHIPPED: {
            Booking.BookingStatus.SHIPPED,
            Booking.BookingStatus.OUT_FOR_DELIVERY,
            Booking.BookingStatus.DELIVERED,
            Booking.BookingStatus.CANCELLED,
        },
        Booking.BookingStatus.OUT_FOR_DELIVERY: {
            Booking.BookingStatus.OUT_FOR_DELIVERY,
            Booking.BookingStatus.DELIVERED,
            Booking.BookingStatus.CANCELLED,
        },
        Booking.BookingStatus.DELIVERED: {Booking.BookingStatus.DELIVERED},
        Booking.BookingStatus.CANCELLED: {Booking.BookingStatus.CANCELLED},
    }
    return mapping.get(current_status, {current_status})


def _admin_allowed_statuses(current_status):
    mapping = {
        Booking.BookingStatus.PENDING: {
            Booking.BookingStatus.PENDING,
            Booking.BookingStatus.CANCELLED,
        },
        Booking.BookingStatus.CONFIRMED: {
            Booking.BookingStatus.PENDING,
            Booking.BookingStatus.CONFIRMED,
            Booking.BookingStatus.SHIPPED,
            Booking.BookingStatus.OUT_FOR_DELIVERY,
            Booking.BookingStatus.CANCELLED,
            Booking.BookingStatus.DELIVERED,
        },
        Booking.BookingStatus.SHIPPED: {
            Booking.BookingStatus.CONFIRMED,
            Booking.BookingStatus.SHIPPED,
            Booking.BookingStatus.OUT_FOR_DELIVERY,
            Booking.BookingStatus.CANCELLED,
            Booking.BookingStatus.DELIVERED,
        },
        Booking.BookingStatus.OUT_FOR_DELIVERY: {
            Booking.BookingStatus.CONFIRMED,
            Booking.BookingStatus.SHIPPED,
            Booking.BookingStatus.OUT_FOR_DELIVERY,
            Booking.BookingStatus.CANCELLED,
            Booking.BookingStatus.DELIVERED,
        },
        Booking.BookingStatus.DELIVERED: {Booking.BookingStatus.DELIVERED},
        Booking.BookingStatus.CANCELLED: {
            Booking.BookingStatus.CANCELLED,
            Booking.BookingStatus.PENDING,
            Booking.BookingStatus.CONFIRMED,
        },
    }
    return mapping.get(current_status, {current_status})


class BookingCreateForm(AppStyledFormMixin, forms.Form):
    ADDRESS_MODE_PREVIOUS = 'previous'
    ADDRESS_MODE_NEW = 'new'
    ADDRESS_MODE_CHOICES = [
        (ADDRESS_MODE_PREVIOUS, 'Use a saved address'),
        (ADDRESS_MODE_NEW, 'Enter a new address'),
    ]

    quantity = forms.IntegerField(min_value=1, initial=1)
    address_mode = forms.ChoiceField(
        choices=ADDRESS_MODE_CHOICES,
        widget=forms.RadioSelect,
        initial=ADDRESS_MODE_PREVIOUS,
    )
    previous_address = PreviousBookingAddressChoiceField(
        queryset=CustomerAddress.objects.none(),
        required=False,
        empty_label='Select saved address',
    )
    delivery_pincode = forms.CharField(
        required=False,
        max_length=20,
        label='Delivery pincode',
    )
    shipping_address = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        saved_address_qs = CustomerAddress.objects.none()
        if user is not None and getattr(user, 'is_authenticated', False):
            saved_address_qs = (
                CustomerAddress.objects.select_related('location', 'location__district', 'location__district__state')
                .filter(customer=user, is_active=True, location__isnull=False)
                .order_by('-is_default', 'label', '-updated_at')
            )

        self.fields['previous_address'].queryset = saved_address_qs
        if not saved_address_qs.exists():
            self.fields['address_mode'].choices = [(self.ADDRESS_MODE_NEW, 'Enter a new address')]
            self.fields['address_mode'].initial = self.ADDRESS_MODE_NEW
            self.fields['previous_address'].widget = forms.HiddenInput()

        self.fields['quantity'].help_text = 'Choose quantity up to 100 units for this booking.'
        self.fields['address_mode'].help_text = 'Choose whether to reuse a saved address or enter a new one.'
        self.fields['previous_address'].help_text = 'Saved addresses are pre-verified with your profile.'
        self.fields['delivery_pincode'].help_text = 'Type pincode to check and confirm delivery availability.'
        self.fields['shipping_address'].help_text = 'Enter house/building, street, landmark, and area details.'
        self.apply_widget_styles()
        self.fields['address_mode'].widget.attrs['class'] = 'nn-address-mode'

    def clean_quantity(self):
        quantity = self.cleaned_data.get('quantity')
        if quantity and quantity > 100:
            raise forms.ValidationError('Maximum allowed quantity per order is 100.')
        return quantity

    def clean(self):
        cleaned_data = super().clean()
        mode = cleaned_data.get('address_mode') or self.ADDRESS_MODE_NEW
        if mode == self.ADDRESS_MODE_PREVIOUS:
            saved_address = cleaned_data.get('previous_address')
            if not saved_address:
                self.add_error('previous_address', 'Select a saved address to continue.')
                return cleaned_data

            delivery_location = saved_address.location
            if not _is_active_delivery_location(delivery_location):
                self.add_error(
                    'previous_address',
                    'The selected saved address pincode is currently unavailable for delivery.',
                )
                return cleaned_data

            shipping_address = (saved_address.address or '').strip()
            if not shipping_address:
                self.add_error(
                    'previous_address',
                    'The selected saved address is incomplete. Enter a new address instead.',
                )
                return cleaned_data

            cleaned_data['resolved_delivery_location'] = delivery_location
            cleaned_data['resolved_shipping_address'] = shipping_address
            cleaned_data['resolved_is_previous_address'] = True
            return cleaned_data

        delivery_pincode = (cleaned_data.get('delivery_pincode') or '').strip()
        shipping_address = (cleaned_data.get('shipping_address') or '').strip()
        delivery_location = None

        if not delivery_pincode:
            self.add_error('delivery_pincode', 'Enter a delivery pincode.')
        else:
            delivery_location = (
                Location.objects.select_related('district', 'district__state')
                .filter(
                    postal_code__iexact=delivery_pincode,
                    is_active=True,
                    district__is_active=True,
                    district__state__is_active=True,
                )
                .order_by('district__state__name', 'district__name', 'name')
                .first()
            )
            if not delivery_location:
                self.add_error('delivery_pincode', 'This pincode is currently unavailable for delivery.')

        if not shipping_address:
            self.add_error('shipping_address', 'Enter the shipping address.')
        elif len(shipping_address) < 10:
            self.add_error('shipping_address', 'Shipping address must be at least 10 characters.')

        if self.errors:
            return cleaned_data

        cleaned_data['resolved_delivery_location'] = delivery_location
        cleaned_data['resolved_shipping_address'] = shipping_address
        cleaned_data['resolved_is_previous_address'] = False
        return cleaned_data


class BookingCancelForm(AppStyledFormMixin, forms.Form):
    cancellation_reason = forms.CharField(
        min_length=8,
        max_length=600,
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Explain why this order is being cancelled.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()

    def clean_cancellation_reason(self):
        reason = (self.cleaned_data.get('cancellation_reason') or '').strip()
        if len(reason) < 8:
            raise forms.ValidationError('Cancellation reason must be at least 8 characters.')
        return reason


class BookingCancellationImpactForm(AppStyledFormMixin, forms.Form):
    cancellation_impact = forms.ChoiceField(
        choices=[
            (Booking.CancellationImpact.NO_IMPACT, 'No Impact'),
            (Booking.CancellationImpact.NEGATIVE_IMPACT, 'Negative Impact'),
        ],
        label='Cancellation impact',
    )
    cancellation_impact_note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={'rows': 3}),
        label='Admin note',
        help_text='Optional note for audit and review records.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()

    def clean(self):
        cleaned_data = super().clean()
        impact = cleaned_data.get('cancellation_impact')
        note = (cleaned_data.get('cancellation_impact_note') or '').strip()
        cleaned_data['cancellation_impact_note'] = note
        if impact == Booking.CancellationImpact.NEGATIVE_IMPACT and len(note) < 10:
            self.add_error(
                'cancellation_impact_note',
                'Add at least 10 characters to explain the negative impact.',
            )
        return cleaned_data


class CartCheckoutForm(AppStyledFormMixin, forms.Form):
    ADDRESS_MODE_PREVIOUS = 'previous'
    ADDRESS_MODE_NEW = 'new'
    ADDRESS_MODE_CHOICES = [
        (ADDRESS_MODE_PREVIOUS, 'Use a saved address'),
        (ADDRESS_MODE_NEW, 'Enter a new address'),
    ]

    address_mode = forms.ChoiceField(
        choices=ADDRESS_MODE_CHOICES,
        widget=forms.RadioSelect,
        initial=ADDRESS_MODE_PREVIOUS,
    )
    previous_booking = PreviousBookingAddressChoiceField(
        queryset=CustomerAddress.objects.none(),
        required=False,
        empty_label='Select saved address',
    )
    delivery_pincode = forms.CharField(
        required=False,
        max_length=20,
        label='Delivery pincode',
    )
    shipping_address = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        previous_booking_qs = CustomerAddress.objects.none()
        if user is not None and getattr(user, 'is_authenticated', False):
            previous_booking_qs = (
                CustomerAddress.objects.select_related('location', 'location__district', 'location__district__state')
                .filter(customer=user, is_active=True, location__isnull=False)
                .order_by('-is_default', 'label', '-updated_at')
            )
        self.fields['previous_booking'].queryset = previous_booking_qs
        has_previous_bookings = previous_booking_qs.exists()
        if not has_previous_bookings:
            self.fields['address_mode'].choices = [(self.ADDRESS_MODE_NEW, 'Enter a new address')]
            self.fields['address_mode'].initial = self.ADDRESS_MODE_NEW
            self.fields['previous_booking'].widget = forms.HiddenInput()
        self.fields['address_mode'].help_text = 'Choose whether to reuse an old address or type a new one.'
        self.fields['previous_booking'].help_text = 'Select any saved address.'
        self.fields['delivery_pincode'].help_text = 'Enter the pincode where all items in this checkout should be delivered.'
        self.fields['shipping_address'].help_text = 'Enter house/building, street, landmark, and area details.'
        self.apply_widget_styles()
        self.fields['address_mode'].widget.attrs['class'] = 'nn-address-mode'

    def clean(self):
        cleaned_data = super().clean()
        mode = cleaned_data.get('address_mode') or self.ADDRESS_MODE_NEW
        if mode == self.ADDRESS_MODE_PREVIOUS:
            saved_address = cleaned_data.get('previous_booking')
            if not saved_address:
                self.add_error('previous_booking', 'Select a saved address to continue.')
                return cleaned_data

            delivery_location = saved_address.location
            if not _is_active_delivery_location(delivery_location):
                self.add_error(
                    'previous_booking',
                    'The selected saved address pincode is currently unavailable for delivery.',
                )
                return cleaned_data

            shipping_address = (saved_address.address or '').strip()
            if not shipping_address:
                self.add_error(
                    'previous_booking',
                    'The selected saved address is incomplete. Enter a new address instead.',
                )
                return cleaned_data

            cleaned_data['resolved_delivery_location'] = delivery_location
            cleaned_data['resolved_shipping_address'] = shipping_address
            cleaned_data['resolved_is_previous_address'] = True
            return cleaned_data

        delivery_pincode = (cleaned_data.get('delivery_pincode') or '').strip()
        shipping_address = (cleaned_data.get('shipping_address') or '').strip()
        delivery_location = None

        if not delivery_pincode:
            self.add_error('delivery_pincode', 'Enter a delivery pincode.')
        else:
            delivery_location = (
                Location.objects.select_related('district', 'district__state')
                .filter(
                    postal_code__iexact=delivery_pincode,
                    is_active=True,
                    district__is_active=True,
                    district__state__is_active=True,
                )
                .order_by('district__state__name', 'district__name', 'name')
                .first()
            )
            if not delivery_location:
                self.add_error('delivery_pincode', 'This pincode is currently unavailable for delivery.')

        if not shipping_address:
            self.add_error('shipping_address', 'Enter the shipping address.')
        elif len(shipping_address) < 10:
            self.add_error('shipping_address', 'Shipping address must be at least 10 characters.')

        if self.errors:
            return cleaned_data

        cleaned_data['resolved_delivery_location'] = delivery_location
        cleaned_data['resolved_shipping_address'] = shipping_address
        cleaned_data['resolved_is_previous_address'] = False
        return cleaned_data


class BookingStatusForm(AppStyledFormMixin, forms.ModelForm):
    SELLER_CANCEL_REASON_STOCK = 'out_of_stock'
    SELLER_CANCEL_REASON_QUALITY = 'quality_issue'
    SELLER_CANCEL_REASON_LOGISTICS = 'delivery_constraint'
    SELLER_CANCEL_REASON_SUPPLIER = 'supplier_delay'
    SELLER_CANCEL_REASON_COMPLIANCE = 'compliance_hold'
    SELLER_CANCEL_REASON_OTHER = 'other'
    SELLER_CANCEL_REASON_CHOICES = [
        (SELLER_CANCEL_REASON_STOCK, 'Item became out of stock'),
        (SELLER_CANCEL_REASON_QUALITY, 'Quality check failed before dispatch'),
        (SELLER_CANCEL_REASON_LOGISTICS, 'Delivery area/logistics constraint'),
        (SELLER_CANCEL_REASON_SUPPLIER, 'Supplier or replenishment delay'),
        (SELLER_CANCEL_REASON_COMPLIANCE, 'Compliance/policy hold'),
        (SELLER_CANCEL_REASON_OTHER, 'Other reason'),
    ]

    seller_cancellation_reason_code = forms.ChoiceField(
        required=False,
        choices=[('', 'Select cancellation reason')] + SELLER_CANCEL_REASON_CHOICES,
        label='Seller cancellation reason',
    )
    seller_cancellation_other_reason = forms.CharField(
        required=False,
        max_length=300,
        widget=forms.Textarea(attrs={'rows': 2}),
        label='Other cancellation reason',
        help_text='Required only when "Other reason" is selected.',
    )
    seller_cancellation_ack_note = forms.CharField(
        required=False,
        max_length=400,
        widget=forms.Textarea(attrs={'rows': 2}),
        label='Seller acknowledgement note (admin only)',
        help_text='Optional internal note for admin review. Customers will not see this note.',
    )
    seller_cancellation_acknowledged = forms.BooleanField(
        required=False,
        label='I understand seller cancellation will trigger risk scoring and may freeze seller operations.',
    )

    class Meta:
        model = Booking
        fields = ['status', 'tracking_number', 'expected_delivery_date']
        widgets = {
            'expected_delivery_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()
        current_status = self.instance.status if self.instance and self.instance.pk else Booking.BookingStatus.PENDING

        if self.user and self.user.role == User.UserRole.SELLER:
            allowed_statuses = _seller_allowed_statuses(current_status)
        elif self.user and self.user.role == User.UserRole.ADMIN:
            allowed_statuses = _admin_allowed_statuses(current_status)
        else:
            allowed_statuses = {value for value, _label in self.fields['status'].choices}

        self.fields['status'].choices = [
            (value, label)
            for value, label in self.fields['status'].choices
            if value in allowed_statuses
        ]
        self.fields['status'].help_text = (
            'Status options depend on current booking state and your role.'
        )
        self.fields['tracking_number'].help_text = 'Add shipment tracking number.'
        self.fields['expected_delivery_date'].help_text = 'Share expected delivery date with customer.'

        show_seller_cancel_fields = (
            bool(self.user and self.user.role == User.UserRole.SELLER)
            and current_status != Booking.BookingStatus.CANCELLED
            and Booking.BookingStatus.CANCELLED in allowed_statuses
        )
        if not show_seller_cancel_fields:
            for name in (
                'seller_cancellation_reason_code',
                'seller_cancellation_other_reason',
                'seller_cancellation_ack_note',
                'seller_cancellation_acknowledged',
            ):
                self.fields[name].required = False
                self.fields[name].widget = forms.HiddenInput()

        self.fields['seller_cancellation_reason_code'].help_text = (
            'Choose a standard reason before cancelling an order.'
        )

    def clean_tracking_number(self):
        return AppStyledFormMixin.clean_tracking(self.cleaned_data.get('tracking_number'))

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get('status')
        tracking_number = (cleaned_data.get('tracking_number') or '').strip()
        expected_delivery_date = cleaned_data.get('expected_delivery_date')

        if status == Booking.BookingStatus.SHIPPED:
            if not tracking_number:
                self.add_error('tracking_number', 'Tracking number is required before marking shipped.')
            if not expected_delivery_date:
                self.add_error('expected_delivery_date', 'Expected delivery date is required before marking shipped.')
            elif expected_delivery_date < timezone.localdate():
                self.add_error('expected_delivery_date', 'Expected delivery date cannot be in the past.')

        if (
            status == Booking.BookingStatus.CANCELLED
            and self.user
            and self.user.role == User.UserRole.SELLER
        ):
            reason_code = (cleaned_data.get('seller_cancellation_reason_code') or '').strip()
            other_reason = (cleaned_data.get('seller_cancellation_other_reason') or '').strip()
            ack_note = (cleaned_data.get('seller_cancellation_ack_note') or '').strip()
            acknowledged = bool(cleaned_data.get('seller_cancellation_acknowledged'))

            choice_map = {value: label for value, label in self.SELLER_CANCEL_REASON_CHOICES}
            if reason_code not in choice_map:
                self.add_error('seller_cancellation_reason_code', 'Select a valid seller cancellation reason.')
            if reason_code == self.SELLER_CANCEL_REASON_OTHER and len(other_reason) < 8:
                self.add_error(
                    'seller_cancellation_other_reason',
                    'Enter at least 8 characters for the other cancellation reason.',
                )
            if not acknowledged:
                self.add_error(
                    'seller_cancellation_acknowledged',
                    'Acknowledge the seller cancellation warning before continuing.',
                )

            resolved_reason = choice_map.get(reason_code, '')
            if reason_code == self.SELLER_CANCEL_REASON_OTHER and other_reason:
                resolved_reason = other_reason
            cleaned_data['seller_cancellation_reason_text'] = resolved_reason
            cleaned_data['seller_cancellation_reason_code'] = reason_code
            cleaned_data['seller_cancellation_other_reason'] = other_reason
            cleaned_data['seller_cancellation_ack_note'] = ack_note

        cleaned_data['tracking_number'] = tracking_number
        return cleaned_data


class PublicDeliveryStatusForm(forms.Form):
    booking_id = forms.IntegerField(min_value=1, label='Booking ID')
    status = forms.ChoiceField(
        choices=[
            (Booking.BookingStatus.OUT_FOR_DELIVERY, 'Out for Delivery'),
            (Booking.BookingStatus.DELIVERED, 'Delivered'),
        ],
        label='Update Status To',
    )


class TransactionForm(AppStyledFormMixin, forms.ModelForm):
    card_holder_name = forms.CharField(required=False, max_length=120, label='Card holder name')
    card_number = forms.CharField(required=False, max_length=19, label='Card number')
    card_expiry = forms.CharField(required=False, max_length=7, label='Expiry (MM/YY)')
    card_cvv = forms.CharField(required=False, max_length=4, label='CVV')
    upi_id = forms.CharField(required=False, max_length=80, label='UPI ID')
    cod_consent = forms.BooleanField(required=False, label='I confirm cash on delivery payment.')

    class Meta:
        model = Transaction
        fields = ['payment_method']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['payment_method'].choices = [
            (Transaction.PaymentMethod.CARD, 'Card'),
            (Transaction.PaymentMethod.UPI, 'UPI'),
            (Transaction.PaymentMethod.COD, 'Cash on Delivery'),
        ]
        if not self.is_bound:
            self.initial.setdefault('payment_method', Transaction.PaymentMethod.CARD)
        self.apply_widget_styles()
        self.fields['payment_method'].help_text = 'Choose Card, UPI, or Cash on Delivery for this academic demo.'
        self.fields['card_holder_name'].help_text = 'Name as shown on your card.'
        self.fields['card_number'].help_text = 'Enter 16-digit card number.'
        self.fields['card_expiry'].help_text = 'Format: MM/YY'
        self.fields['card_cvv'].help_text = '3 or 4 digit security code.'
        self.fields['upi_id'].help_text = 'Example: name@bank'

    def clean_card_number(self):
        value = (self.cleaned_data.get('card_number') or '').strip().replace(' ', '')
        if value and (not value.isdigit() or len(value) not in {15, 16, 19}):
            raise forms.ValidationError('Enter a valid card number.')
        return value

    def clean_card_expiry(self):
        value = (self.cleaned_data.get('card_expiry') or '').strip()
        if value:
            parts = value.split('/')
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise forms.ValidationError('Use MM/YY format.')
            month = int(parts[0])
            year = int(parts[1])
            if month < 1 or month > 12:
                raise forms.ValidationError('Use a valid expiry month.')
            if year < 0 or year > 99:
                raise forms.ValidationError('Use a valid expiry year.')
        return value

    def clean_card_cvv(self):
        value = (self.cleaned_data.get('card_cvv') or '').strip()
        if value and (not value.isdigit() or len(value) not in {3, 4}):
            raise forms.ValidationError('Enter a valid CVV.')
        return value

    def clean_upi_id(self):
        value = (self.cleaned_data.get('upi_id') or '').strip()
        if value and '@' not in value:
            raise forms.ValidationError('Enter a valid UPI ID.')
        return value

    def clean(self):
        cleaned_data = super().clean()
        method = cleaned_data.get('payment_method')

        if method == Transaction.PaymentMethod.CARD:
            if not cleaned_data.get('card_holder_name'):
                self.add_error('card_holder_name', 'Enter card holder name.')
            if not cleaned_data.get('card_number'):
                self.add_error('card_number', 'Enter card number.')
            if not cleaned_data.get('card_expiry'):
                self.add_error('card_expiry', 'Enter card expiry.')
            if not cleaned_data.get('card_cvv'):
                self.add_error('card_cvv', 'Enter CVV.')
        elif method == Transaction.PaymentMethod.UPI:
            if not cleaned_data.get('upi_id'):
                self.add_error('upi_id', 'Enter UPI ID.')
        elif method == Transaction.PaymentMethod.COD:
            if not cleaned_data.get('cod_consent'):
                self.add_error('cod_consent', 'Confirm cash on delivery to continue.')

        return cleaned_data
