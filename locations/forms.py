from django import forms

from config.form_mixins import AppStyledFormMixin
from locations.models import District
from locations.models import Location
from locations.models import State


class StateForm(AppStyledFormMixin, forms.ModelForm):
    class Meta:
        model = State
        fields = ['name', 'code', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()
        if 'is_active' in self.fields:
            self.fields['is_active'].widget = forms.CheckboxInput(
                attrs={'class': 'nn-service-toggle-field'}
            )
            self.fields['is_active'].help_text = 'Turn on when this state is available for service.'

    def clean_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('name'), label='State name', min_len=2)

    def clean_code(self):
        code = (self.cleaned_data.get('code') or '').strip().upper()
        if code and len(code) < 2:
            raise forms.ValidationError('State code should be at least 2 characters.')
        return code


class DistrictForm(AppStyledFormMixin, forms.ModelForm):
    class Meta:
        model = District
        fields = ['state', 'name', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()
        if 'is_active' in self.fields:
            self.fields['is_active'].widget = forms.CheckboxInput(
                attrs={'class': 'nn-service-toggle-field'}
            )
            self.fields['is_active'].help_text = 'Turn on when this district is available for service.'

    def clean_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('name'), label='District name', min_len=2)

    def clean(self):
        cleaned_data = super().clean()
        state = cleaned_data.get('state')
        is_active = bool(cleaned_data.get('is_active'))
        if state and is_active and not state.is_active:
            self.add_error('is_active', 'Cannot mark this district available while the state is not available.')
        return cleaned_data


class LocationForm(AppStyledFormMixin, forms.ModelForm):
    class Meta:
        model = Location
        fields = ['district', 'name', 'postal_code', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_styles()
        if 'is_active' in self.fields:
            self.fields['is_active'].widget = forms.CheckboxInput(
                attrs={'class': 'nn-service-toggle-field'}
            )
            self.fields['is_active'].help_text = 'Turn on when this location is available for service.'

    def clean_name(self):
        return AppStyledFormMixin.clean_name_like(self.cleaned_data.get('name'), label='Location name', min_len=2)

    def clean_postal_code(self):
        return AppStyledFormMixin.clean_postal(self.cleaned_data.get('postal_code'))

    def clean(self):
        cleaned_data = super().clean()
        district = cleaned_data.get('district')
        is_active = bool(cleaned_data.get('is_active'))
        if district and is_active:
            state = district.state
            if not district.is_active:
                self.add_error('is_active', 'Cannot mark this location available while the district is not available.')
            elif state and not state.is_active:
                self.add_error('is_active', 'Cannot mark this location available while the state is not available.')
        return cleaned_data
