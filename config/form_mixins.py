import re

from django import forms


PHONE_RE = re.compile(r'^\+?[0-9]{10,15}$')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 .,&()/-]*$')
POSTAL_RE = re.compile(r'^[A-Za-z0-9 -]{4,12}$')
TRACKING_RE = re.compile(r'^[A-Za-z0-9-]{4,40}$')


class AppStyledFormMixin:
    input_class = 'form-control w-full rounded-lg border border-gray-300 px-3 py-2'
    select_class = 'form-select w-full rounded-lg border border-gray-300 px-3 py-2'
    textarea_class = 'form-control w-full rounded-lg border border-gray-300 px-3 py-2'
    checkbox_class = 'form-check-input'

    def apply_widget_styles(self):
        for name, field in self.fields.items():
            widget = field.widget
            current = widget.attrs.get('class', '')

            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = f'{self.checkbox_class} {current}'.strip()
                continue

            if isinstance(widget, forms.Select):
                widget.attrs['class'] = f'{self.select_class} {current}'.strip()
            elif isinstance(widget, forms.Textarea):
                widget.attrs['class'] = f'{self.textarea_class} {current}'.strip()
                widget.attrs.setdefault('rows', 3)
            else:
                widget.attrs['class'] = f'{self.input_class} {current}'.strip()

            widget.attrs.setdefault('placeholder', field.label or name.replace('_', ' ').title())

    @staticmethod
    def clean_name_like(value, label='This field', min_len=2):
        value = (value or '').strip()
        if len(value) < min_len:
            raise forms.ValidationError(f'{label} must be at least {min_len} characters.')
        if not NAME_RE.match(value):
            raise forms.ValidationError(f'{label} contains invalid characters.')
        return value

    @staticmethod
    def clean_phone(value):
        value = (value or '').strip()
        if not value:
            return value
        if not PHONE_RE.match(value):
            raise forms.ValidationError('Enter a valid phone number (10-15 digits, optional +).')
        return value

    @staticmethod
    def clean_postal(value):
        value = (value or '').strip()
        if not value:
            return value
        if not POSTAL_RE.match(value):
            raise forms.ValidationError('Postal code must be 4-12 characters (letters, numbers, space, dash).')
        return value

    @staticmethod
    def clean_tracking(value):
        value = (value or '').strip()
        if not value:
            return value
        if not TRACKING_RE.match(value):
            raise forms.ValidationError('Tracking number must be 4-40 characters (letters, numbers, dash).')
        return value
