from django import forms

from catalog.models import Product
from config.form_mixins import AppStyledFormMixin
from orders.models import Booking
from support.models import Complaint
from support.models import Feedback


class ComplaintForm(AppStyledFormMixin, forms.ModelForm):
    class Meta:
        model = Complaint
        fields = ['product', 'booking', 'subject', 'message']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()

    def clean_subject(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('subject'), label='Subject', min_len=4)

    def clean_message(self):
        message = (self.cleaned_data.get('message') or '').strip()
        if len(message) < 15:
            raise forms.ValidationError('Please enter at least 15 characters for your complaint.')
        return message


class ComplaintStatusForm(AppStyledFormMixin, forms.ModelForm):
    class Meta:
        model = Complaint
        fields = ['status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()


class ComplaintActionForm(AppStyledFormMixin, forms.ModelForm):
    mark_anomaly = forms.BooleanField(required=False, label='Mark as anomaly')
    run_ml_check = forms.BooleanField(required=False, initial=True, label='Run ML risk scoring now')
    anomaly_note = forms.CharField(
        required=False,
        label='Anomaly/Action Note',
        widget=forms.Textarea(attrs={'rows': 4}),
    )

    class Meta:
        model = Complaint
        fields = ['status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['mark_anomaly'].initial = bool(self.instance.is_anomaly)
            self.fields['anomaly_note'].initial = self.instance.anomaly_note
        self.apply_widget_styles()

    def clean(self):
        cleaned_data = super().clean()
        mark_anomaly = bool(cleaned_data.get('mark_anomaly'))
        anomaly_note = (cleaned_data.get('anomaly_note') or '').strip()
        if mark_anomaly and len(anomaly_note) < 10:
            self.add_error('anomaly_note', 'Please add at least 10 characters when marking anomaly.')
        cleaned_data['anomaly_note'] = anomaly_note
        return cleaned_data


class FeedbackForm(AppStyledFormMixin, forms.ModelForm):
    class Meta:
        model = Feedback
        fields = ['product', 'booking', 'rating', 'comment']

    @staticmethod
    def _to_positive_int(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def __init__(
        self,
        *args,
        user=None,
        initial_booking_id=None,
        locked_booking_id=None,
        locked_product_id=None,
        **kwargs,
    ):
        self.user = user
        self.initial_booking_id = initial_booking_id
        self.locked_booking_id = self._to_positive_int(locked_booking_id)
        self.locked_product_id = self._to_positive_int(locked_product_id)
        self.is_feedback_target_locked = bool(self.locked_booking_id and self.locked_product_id)
        super().__init__(*args, **kwargs)

        self.fields['booking'].required = True
        self.fields['product'].required = True

        delivered_bookings = Booking.objects.none()
        delivered_products = Product.objects.none()
        if user is not None and getattr(user, 'is_authenticated', False):
            delivered_bookings = Booking.objects.filter(
                customer=user,
                status=Booking.BookingStatus.DELIVERED,
            ).order_by('-booked_at')
            delivered_seller_ids = delivered_bookings.values_list('seller_id', flat=True).distinct()
            delivered_products = Product.objects.filter(
                seller_id__in=delivered_seller_ids,
                is_active=True,
            ).distinct()

            booking_id = None
            if self.is_bound:
                raw_booking = self.data.get(self.add_prefix('booking'))
                if raw_booking and str(raw_booking).isdigit():
                    booking_id = int(raw_booking)
            elif initial_booking_id:
                booking_id = initial_booking_id
                self.initial.setdefault('booking', booking_id)

            if booking_id and delivered_bookings.filter(id=booking_id).exists():
                self.initial.setdefault('booking', booking_id)

        if self.is_feedback_target_locked:
            self.initial['booking'] = self.locked_booking_id
            self.initial['product'] = self.locked_product_id
            self.fields['booking'].queryset = delivered_bookings.filter(id=self.locked_booking_id)
            self.fields['product'].queryset = Product.objects.filter(id=self.locked_product_id)
            self.fields['booking'].widget = forms.HiddenInput()
            self.fields['product'].widget = forms.HiddenInput()
        else:
            self.fields['booking'].queryset = delivered_bookings
            self.fields['product'].queryset = delivered_products

        self.apply_widget_styles()

    def clean_comment(self):
        comment = (self.cleaned_data.get('comment') or '').strip()
        rating = self.cleaned_data.get('rating')
        if rating is not None and rating <= 2 and len(comment) < 10:
            raise forms.ValidationError('Please add at least 10 characters for low ratings.')
        return comment

    def clean(self):
        cleaned_data = super().clean()
        booking = cleaned_data.get('booking')
        product = cleaned_data.get('product')

        if self.user is None:
            return cleaned_data

        if not booking:
            self.add_error('booking', 'Select a delivered booking.')
            return cleaned_data
        if booking.customer_id != self.user.id:
            self.add_error('booking', 'Selected booking does not belong to your account.')
            return cleaned_data
        if booking.status != Booking.BookingStatus.DELIVERED:
            self.add_error('booking', 'Feedback can be submitted only after delivery.')
            return cleaned_data

        if not product:
            self.add_error('product', 'Select a product from the delivered booking.')
            return cleaned_data
        if not booking.items.filter(product=product).exists():
            self.add_error('product', 'This product is not part of the selected booking.')
            return cleaned_data

        if self.locked_booking_id and booking.id != self.locked_booking_id:
            self.add_error('booking', 'Booking is locked for this review flow and cannot be changed.')
        if self.locked_product_id and product.id != self.locked_product_id:
            self.add_error('product', 'Product is locked for this review flow and cannot be changed.')

        if Feedback.objects.filter(
            customer=self.user,
            booking=booking,
            product=product,
        ).exists():
            self.add_error('product', 'Feedback already submitted for this product in this booking.')
        return cleaned_data
