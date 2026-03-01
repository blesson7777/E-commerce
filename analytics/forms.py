from django import forms
from django.utils import timezone

from config.form_mixins import AppStyledFormMixin


class SellerRiskAppealForm(AppStyledFormMixin, forms.Form):
    appeal_text = forms.CharField(
        label='Appeal message',
        min_length=15,
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text='Explain why the risk action should be reconsidered.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()


class AdminRiskFinalDecisionForm(AppStyledFormMixin, forms.Form):
    DECISION_UNFREEZE = 'unfreeze'
    DECISION_KEEP_FROZEN = 'keep_frozen'
    DECISION_TERMINATE = 'terminate'
    DECISION_CHOICES = [
        (DECISION_UNFREEZE, 'Unfreeze seller after review'),
        (DECISION_KEEP_FROZEN, 'Keep seller frozen'),
        (DECISION_TERMINATE, 'Terminate seller account operations'),
    ]

    decision = forms.ChoiceField(choices=DECISION_CHOICES)
    decision_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Optional decision note for audit trail.',
    )
    waive_fine = forms.BooleanField(
        required=False,
        label='Remove penalty fine after validating seller appeal',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()


class SellerFinePaymentForm(AppStyledFormMixin, forms.Form):
    PAYMENT_CARD = 'card'
    PAYMENT_UPI = 'upi'
    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_CARD, 'Card'),
        (PAYMENT_UPI, 'UPI'),
    ]

    payment_method = forms.ChoiceField(
        choices=PAYMENT_METHOD_CHOICES,
        label='Payment method',
        widget=forms.RadioSelect,
    )
    card_holder_name = forms.CharField(required=False, max_length=120, label='Card holder name')
    card_number = forms.CharField(required=False, max_length=19, label='Card number')
    card_expiry = forms.CharField(required=False, max_length=7, label='Expiry (MM/YY)')
    card_cvv = forms.CharField(required=False, max_length=4, label='CVV')
    upi_id = forms.CharField(required=False, max_length=80, label='UPI ID')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault('payment_method', self.PAYMENT_CARD)
        self.apply_widget_styles()
        self.fields['payment_method'].help_text = 'Only Card and UPI are accepted for final fine payment.'
        self.fields['card_holder_name'].help_text = 'Name printed on your card.'
        self.fields['card_number'].help_text = 'Enter 15-19 digit card number.'
        self.fields['card_expiry'].help_text = 'Use MM/YY format.'
        self.fields['card_cvv'].help_text = '3 or 4 digit CVV.'
        self.fields['upi_id'].help_text = 'Example: seller@bank'

    def clean_card_number(self):
        value = (self.cleaned_data.get('card_number') or '').strip().replace(' ', '')
        if value and (not value.isdigit() or len(value) not in {15, 16, 19}):
            raise forms.ValidationError('Enter a valid card number.')
        return value

    def clean_card_expiry(self):
        value = (self.cleaned_data.get('card_expiry') or '').strip()
        if not value:
            return value
        parts = value.split('/')
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise forms.ValidationError('Use MM/YY format.')
        month = int(parts[0])
        year = int(parts[1])
        if month < 1 or month > 12:
            raise forms.ValidationError('Use a valid expiry month.')
        current_date = timezone.localdate()
        current_year = current_date.year % 100
        current_month = current_date.month
        if year < current_year or (year == current_year and month < current_month):
            raise forms.ValidationError('Card expiry cannot be in the past.')
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
        if method == self.PAYMENT_CARD:
            if not cleaned_data.get('card_holder_name'):
                self.add_error('card_holder_name', 'Enter card holder name.')
            if not cleaned_data.get('card_number'):
                self.add_error('card_number', 'Enter card number.')
            if not cleaned_data.get('card_expiry'):
                self.add_error('card_expiry', 'Enter card expiry.')
            if not cleaned_data.get('card_cvv'):
                self.add_error('card_cvv', 'Enter CVV.')
        elif method == self.PAYMENT_UPI:
            if not cleaned_data.get('upi_id'):
                self.add_error('upi_id', 'Enter UPI ID.')
        else:
            self.add_error('payment_method', 'Select a valid payment method.')
        return cleaned_data
