import warnings

from django import forms
from django.core.exceptions import ValidationError
from django.db import models
from PIL import Image, UnidentifiedImageError

from .models import Category, Coupon, Platform, sanitize_coupon_code


MAX_COUPON_IMAGE_SIZE = 10 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class CouponUploadForm(forms.Form):
    title = forms.CharField(max_length=200)
    category = forms.ModelChoiceField(queryset=Category.objects.all())
    platform = forms.ModelChoiceField(queryset=Platform.objects.all())
    terms = forms.CharField(required=False, widget=forms.Textarea)
    proof_image = forms.ImageField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 "
                "text-slate-900 shadow-sm focus:border-indigo-600 focus:outline-none "
                "focus:ring-2 focus:ring-indigo-100"
            )
        self.fields["terms"].widget.attrs["rows"] = 3
        self.fields["proof_image"].widget.attrs.update(
            {
                "accept": "image/jpeg,image/png,image/webp",
                "class": (
                    "block w-full cursor-pointer text-sm text-slate-600 "
                    "file:mr-4 file:rounded-lg file:border-0 file:bg-indigo-50 "
                    "file:px-4 file:py-2.5 file:font-semibold file:text-indigo-700 "
                    "hover:file:bg-indigo-100"
                ),
            }
        )

    def clean_proof_image(self):
        image = self.cleaned_data["proof_image"]
        if image.size > MAX_COUPON_IMAGE_SIZE:
            raise ValidationError("Image must be no larger than 10 MB.")

        image.seek(0)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(image) as decoded_image:
                    detected_format = decoded_image.format
                    decoded_image.verify()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
        ) as error:
            raise ValidationError("The uploaded image is invalid or corrupted.") from error
        finally:
            image.seek(0)

        expected_mime_type = ALLOWED_IMAGE_FORMATS.get(detected_format, (None,))[0]
        if expected_mime_type is None or image.content_type != expected_mime_type:
            raise ValidationError("Upload a valid JPEG, PNG, or WebP image.")
        image.image_extension = ALLOWED_IMAGE_FORMATS[detected_format][1]
        return image


class CouponConfirmationForm(forms.Form):
    code = forms.CharField(max_length=255)
    expiry_date = forms.DateField(
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    upload_token = forms.CharField(widget=forms.HiddenInput)

    def clean_code(self):
        code = sanitize_coupon_code(self.cleaned_data["code"])
        if not code:
            raise ValidationError("Enter a code containing at least one letter or digit.")
        if len(code) > 255:
            raise ValidationError("The sanitized code must be 255 characters or fewer.")
        return code


class MarketplaceFilterForm(forms.Form):
    class ExpiryTimeline(models.TextChoices):
        ANY = "any", "Any expiry"
        UPCOMING = "upcoming", "Not expired"
        EXPIRED = "expired", "Expired"
        NEXT_7_DAYS = "next_7_days", "Expires in 7 days"
        NEXT_30_DAYS = "next_30_days", "Expires in 30 days"
        NEXT_90_DAYS = "next_90_days", "Expires in 90 days"

    keyword = forms.CharField(required=False, max_length=100)
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(),
        required=False,
        empty_label="All categories",
    )
    platform = forms.ModelChoiceField(
        queryset=Platform.objects.all(),
        required=False,
        empty_label="All platforms",
    )
    expiry_timeline = forms.ChoiceField(
        choices=ExpiryTimeline.choices,
        required=False,
        initial=ExpiryTimeline.ANY,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 "
                "text-slate-900 shadow-sm focus:border-indigo-600 focus:outline-none "
                "focus:ring-2 focus:ring-indigo-100"
            )
