import hashlib
import re
import unicodedata

from django.core.exceptions import ValidationError
from django.db import models


def sanitize_coupon_code(code):
    normalized = unicodedata.normalize("NFKC", code).upper()
    return re.sub(r"[^A-Z0-9]", "", normalized)


def hash_coupon_code(code):
    sanitized_code = sanitize_coupon_code(code)
    if not sanitized_code:
        raise ValidationError("Coupon code must contain at least one letter or digit.")
    return hashlib.sha256(sanitized_code.encode("ascii")).hexdigest()


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Platform(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Coupon(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        LOCKED_PENDING = "LOCKED_PENDING", "Locked pending acceptance"
        LOCKED_ESCROW = "LOCKED_ESCROW", "Locked in escrow"
        IN_ESCROW = "IN_ESCROW", "In escrow"
        REDEEMED = "REDEEMED", "Redeemed"
        TRADED = "TRADED", "Traded"
        SUSPENDED_REVIEW = "SUSPENDED_REVIEW", "Suspended for review"
        EXPIRED = "EXPIRED", "Expired"

    owner = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="coupons",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="coupons",
    )
    platform = models.ForeignKey(
        Platform,
        on_delete=models.PROTECT,
        related_name="coupons",
    )
    title = models.CharField(max_length=200)
    code = models.CharField(max_length=255)
    code_hash = models.CharField(max_length=64, editable=False, blank=True, default="")
    terms = models.TextField(blank=True)
    expiry_date = models.DateField()
    proof_image = models.ImageField(upload_to="coupons/proofs/")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.AVAILABLE,
    )

    class Meta:
        ordering = ["expiry_date", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "code_hash"),
                name="unique_coupon_code_per_owner",
            )
        ]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        self.code = sanitize_coupon_code(self.code)
        if len(self.code) > 255:
            raise ValidationError(
                {"code": "Coupon codes must be 255 characters or fewer."}
            )
        self.code_hash = hash_coupon_code(self.code)
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                "code",
                "code_hash",
            }
        super().save(*args, **kwargs)
