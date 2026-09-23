from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q


class SignupForm(forms.Form):
    restaurant_name = forms.CharField(max_length=255)
    owner_name = forms.CharField(max_length=255)
    email = forms.EmailField()
    phone = forms.CharField(
        max_length=20,
        error_messages={"required": "Phone is required."},
    )
    password = forms.CharField(widget=forms.PasswordInput)
    confirm_password = forms.CharField(widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(
            Q(email__iexact=email) | Q(username__iexact=email)
        ).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        if not phone:
            raise forms.ValidationError("Phone is required.")
        return phone

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "Passwords do not match.")
        if password:
            validate_password(password)
        return cleaned_data


class RestaurantEmailConnectionForm(forms.Form):
    email_address = forms.EmailField(label="Gmail address")
    app_password = forms.CharField(
        label="Google App Password",
        widget=forms.PasswordInput(render_value=False),
        help_text="Use a Google App Password, not your regular Gmail password.",
    )
