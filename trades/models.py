from datetime import timedelta

from django.db import models
from django.utils import timezone


class Trade(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        ACTIVE_EXCHANGE = "ACTIVE_EXCHANGE", "Active exchange"
        COMPLETED = "COMPLETED", "Completed"
        DISPUTED = "DISPUTED", "Disputed"
        CANCELLED = "CANCELLED", "Cancelled"

    class DisputeReason(models.TextChoices):
        INVALID_CODE = "INVALID_CODE", "Invalid code reported"
        TIMEOUT_ABANDONMENT = "TIMEOUT_ABANDONMENT", "Mutual timeout"

    sender = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="sent_trades",
    )
    receiver = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="received_trades",
    )
    offered_coupon = models.ForeignKey(
        "coupons.Coupon",
        on_delete=models.PROTECT,
        related_name="offered_trades",
    )
    requested_coupon = models.ForeignKey(
        "coupons.Coupon",
        on_delete=models.PROTECT,
        related_name="requested_trades",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PROPOSED,
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    sender_verified_at = models.DateTimeField(null=True, blank=True)
    receiver_verified_at = models.DateTimeField(null=True, blank=True)
    dispute_reason = models.CharField(
        max_length=24,
        choices=DisputeReason.choices,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=("status", "expires_at"))]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(sender=models.F("receiver")),
                name="trade_participants_must_differ",
            ),
            models.CheckConstraint(
                condition=~models.Q(offered_coupon=models.F("requested_coupon")),
                name="trade_coupons_must_differ",
            ),
        ]

    def __str__(self):
        return f"Trade {self.pk}: {self.offered_coupon} for {self.requested_coupon}"

    def start_verification_window(self):
        self.accepted_at = timezone.now()
        self.expires_at = self.accepted_at + timedelta(minutes=15)
        self.status = self.Status.ACTIVE_EXCHANGE


class TradeEvent(models.Model):
    class Type(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"
        VERIFIED = "VERIFIED", "Confirmed"
        INVALID_CODE_REPORTED = "INVALID_CODE_REPORTED", "Invalid code reported"
        COMPLETED = "COMPLETED", "Completed"
        DISPUTED = "DISPUTED", "Disputed"
        TIMEOUT_COMPLETED = "TIMEOUT_COMPLETED", "Completed after timeout"
        TIMEOUT_DISPUTED = "TIMEOUT_DISPUTED", "Quarantined after timeout"

    trade = models.ForeignKey(
        Trade,
        on_delete=models.CASCADE,
        related_name="events",
    )
    actor = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trade_events",
    )
    event_type = models.CharField(max_length=24, choices=Type.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.get_event_type_display()} on trade #{self.trade_id}"
