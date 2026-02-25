import re

from django import forms

from config.form_mixins import AppStyledFormMixin
from catalog.models import Category
from catalog.models import Product
from locations.models import District
from locations.models import Location
from locations.models import State


class CategoryForm(AppStyledFormMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'description', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()
        if 'is_active' in self.fields:
            self.fields['is_active'].widget = forms.CheckboxInput(
                attrs={'class': 'nn-service-toggle-field'}
            )
            self.fields['is_active'].help_text = 'Toggle On to keep this category available to sellers.'

    def clean_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('name'), label='Category name', min_len=2)


class ProductForm(AppStyledFormMixin, forms.ModelForm):
    serviceable_pincodes = forms.CharField(
        required=False,
        label='Specific Serviceable Pincodes (Optional)',
        widget=forms.Textarea(attrs={'rows': 2}),
    )
    non_serviceable_pincodes = forms.CharField(
        required=False,
        label='Non-Serviceable Pincodes (Edit only)',
        widget=forms.Textarea(attrs={'rows': 2}),
    )

    class Meta:
        model = Product
        fields = [
            'category',
            'serviceable_states',
            'serviceable_districts',
            'serviceable_locations',
            'non_serviceable_locations',
            'name',
            'description',
            'photo',
            'price',
            'stock_quantity',
            'weight',
            'size',
            'is_active',
        ]
        widgets = {
            'photo': forms.ClearableFileInput(attrs={'accept': 'image/*'}),
            'serviceable_states': forms.SelectMultiple(attrs={'size': 8}),
            'serviceable_districts': forms.SelectMultiple(attrs={'size': 10}),
            'serviceable_locations': forms.MultipleHiddenInput(),
            'non_serviceable_locations': forms.MultipleHiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_non_serviceable_locations = bool(self.instance and self.instance.pk)
        active_states = State.objects.filter(is_active=True).order_by('name')
        active_districts = District.objects.select_related('state').filter(
            is_active=True,
            state__is_active=True,
        ).order_by('state__name', 'name')

        self.fields['category'].queryset = Category.objects.filter(is_active=True).order_by('name')
        self.fields['category'].help_text = 'Choose an active category created by admin.'
        self.fields['serviceable_states'].queryset = active_states
        self.fields['serviceable_districts'].queryset = active_districts
        self.fields['serviceable_states'].widget.attrs['class'] = (
            f"{self.fields['serviceable_states'].widget.attrs.get('class', '')} nn-service-select"
        ).strip()
        self.fields['serviceable_districts'].widget.attrs['class'] = (
            f"{self.fields['serviceable_districts'].widget.attrs.get('class', '')} nn-service-select"
        ).strip()
        # Keep model M2M field hidden to avoid rendering massive pincode lists.
        self.fields['serviceable_locations'].queryset = Location.objects.none()
        self.fields['serviceable_locations'].required = False

        if self.show_non_serviceable_locations:
            self.fields['non_serviceable_locations'].queryset = Location.objects.none()
            self.fields['non_serviceable_locations'].required = False
            self.fields['non_serviceable_pincodes'].help_text = (
                'Optional. Enter comma-separated pincodes to block for this product. '
                'Example: 680001, 682001'
            )
        else:
            self.fields.pop('non_serviceable_locations', None)
            self.fields.pop('non_serviceable_pincodes', None)

        self.fields['serviceable_states'].label = 'Serviceable States'
        self.fields['serviceable_districts'].label = 'Serviceable Districts'
        self.fields['serviceable_states'].help_text = (
            'Select states to make all active pincodes in those states serviceable.'
        )
        self.fields['serviceable_districts'].help_text = (
            'Select districts to make all active pincodes in those districts serviceable.'
        )
        self.fields['serviceable_pincodes'].help_text = (
            'Optional. Enter comma-separated pincodes for direct location-level service. '
            'Example: 680001, 682001'
        )
        self.fields['serviceable_districts'].label_from_instance = (
            lambda obj: f'{obj.name} ({obj.state.name})' if obj.state_id else obj.name
        )
        if self.instance.pk:
            pincode_values = self.instance.serviceable_locations.values_list('postal_code', flat=True).distinct()
            self.fields['serviceable_pincodes'].initial = ', '.join(pincode_values)
            if self.show_non_serviceable_locations:
                blocked_pincode_values = (
                    self.instance.non_serviceable_locations.values_list('postal_code', flat=True).distinct()
                )
                self.fields['non_serviceable_pincodes'].initial = ', '.join(blocked_pincode_values)
        self.fields['photo'].help_text = 'Upload JPG, JPEG, PNG, or WEBP.'
        self.fields['stock_quantity'].label = 'Quantity'
        self.fields['weight'].help_text = 'Optional: product weight in kilograms.'
        self.fields['size'].help_text = 'Optional: e.g. Small, Medium, 500 ml, 2 kg pack.'
        self.apply_widget_styles()
        if 'is_active' in self.fields:
            self.fields['is_active'].widget = forms.CheckboxInput(
                attrs={'class': 'nn-service-toggle-field'}
            )
            self.fields['is_active'].help_text = 'Toggle On to make this product visible and sellable.'

    def clean_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('name'), label='Product name', min_len=3)

    def clean_price(self):
        price = self.cleaned_data.get('price')
        if price is None or price <= 0:
            raise forms.ValidationError('Price must be greater than 0.')
        return price

    def clean_stock_quantity(self):
        stock = self.cleaned_data.get('stock_quantity')
        if stock is None or stock < 0:
            raise forms.ValidationError('Stock quantity cannot be negative.')
        return stock

    def clean_weight(self):
        weight = self.cleaned_data.get('weight')
        if weight is not None and weight <= 0:
            raise forms.ValidationError('Weight must be greater than 0.')
        return weight

    def clean_size(self):
        size = (self.cleaned_data.get('size') or '').strip()
        if size and len(size) < 2:
            raise forms.ValidationError('Size must be at least 2 characters.')
        return size

    def clean_description(self):
        description = (self.cleaned_data.get('description') or '').strip()
        if len(description) < 10:
            raise forms.ValidationError('Description must be at least 10 characters.')
        return description

    def clean(self):
        cleaned_data = super().clean()
        states = cleaned_data.get('serviceable_states')
        districts = cleaned_data.get('serviceable_districts')
        raw_pincodes = (cleaned_data.get('serviceable_pincodes') or '').strip()
        raw_non_serviceable_pincodes = (cleaned_data.get('non_serviceable_pincodes') or '').strip()
        resolved_districts = districts

        if states and states.exists():
            selected_state_ids = set(states.values_list('id', flat=True))
            if districts and districts.exists():
                invalid_districts = [
                    district.name
                    for district in districts
                    if district.state_id not in selected_state_ids
                ]
                if invalid_districts:
                    self.add_error(
                        'serviceable_districts',
                        (
                            'Selected districts must belong to the selected states. '
                            f'Invalid: {", ".join(invalid_districts[:8])}'
                        ),
                    )

        pincode_tokens = [
            token.strip()
            for token in re.split(r'[,\s]+', raw_pincodes)
            if token.strip()
        ]
        unique_pincodes = sorted(set(pincode_tokens))
        location_queryset = Location.objects.none()
        if unique_pincodes:
            location_queryset = Location.objects.select_related('district', 'district__state').filter(
                postal_code__in=unique_pincodes,
                is_active=True,
                district__is_active=True,
                district__state__is_active=True,
            ).distinct()
            found_codes = {code for code in location_queryset.values_list('postal_code', flat=True)}
            missing_codes = [code for code in unique_pincodes if code not in found_codes]
            if missing_codes:
                self.add_error(
                    'serviceable_pincodes',
                    (
                        'These pincodes were not found or are currently turned off: '
                        f'{", ".join(missing_codes[:10])}'
                    ),
                )

        cleaned_data['resolved_serviceable_locations'] = location_queryset
        cleaned_data['resolved_serviceable_districts'] = resolved_districts
        resolved_non_serviceable_locations = None

        if self.show_non_serviceable_locations:
            non_serviceable_tokens = [
                token.strip()
                for token in re.split(r'[,\s]+', raw_non_serviceable_pincodes)
                if token.strip()
            ]
            unique_non_serviceable_pincodes = sorted(set(non_serviceable_tokens))
            resolved_non_serviceable_locations = Location.objects.none()
            if unique_non_serviceable_pincodes:
                resolved_non_serviceable_locations = (
                    Location.objects.select_related('district', 'district__state')
                    .filter(
                        postal_code__in=unique_non_serviceable_pincodes,
                        is_active=True,
                        district__is_active=True,
                        district__state__is_active=True,
                    )
                    .distinct()
                )
                found_non_serviceable_codes = {
                    code for code in resolved_non_serviceable_locations.values_list('postal_code', flat=True)
                }
                missing_non_serviceable_codes = [
                    code
                    for code in unique_non_serviceable_pincodes
                    if code not in found_non_serviceable_codes
                ]
                if missing_non_serviceable_codes:
                    self.add_error(
                        'non_serviceable_pincodes',
                        (
                            'These non-serviceable pincodes were not found or are currently turned off: '
                            f'{", ".join(missing_non_serviceable_codes[:10])}'
                        ),
                    )

        cleaned_data['resolved_non_serviceable_locations'] = resolved_non_serviceable_locations

        has_states = bool(states and states.exists())
        has_districts = bool(resolved_districts and resolved_districts.exists())
        has_locations = bool(unique_pincodes and location_queryset.exists())

        if not (has_states or has_districts or has_locations):
            raise forms.ValidationError(
                'Select at least one serviceable state, district, or pincode for this product.'
            )

        return cleaned_data

    def _save_m2m(self):
        super()._save_m2m()
        if not self.instance.pk:
            return

        resolved_districts = self.cleaned_data.get('resolved_serviceable_districts')
        if resolved_districts is not None:
            self.instance.serviceable_districts.set(resolved_districts)

        locations = self.cleaned_data.get('resolved_serviceable_locations')
        if locations is not None:
            self.instance.serviceable_locations.set(locations)

        blocked_locations = self.cleaned_data.get('resolved_non_serviceable_locations')
        if blocked_locations is not None:
            self.instance.non_serviceable_locations.set(blocked_locations)
