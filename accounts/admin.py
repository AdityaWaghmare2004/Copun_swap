from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username",
        "email",
        "trust_score",
        "successful_trades_count",
        "disputed_trades_count",
        "dispute_rate",
        "requires_review",
        "is_staff",
    )
    list_filter = UserAdmin.list_filter + ("requires_review",)

    @admin.display(description="Dispute rate")
    def dispute_rate(self, user):
        rate = user.dispute_rate_percent
        return "No trades" if rate is None else f"{rate:.1f}%"

    fieldsets = UserAdmin.fieldsets + (
        (
            "Reputation",
            {
                "fields": (
                    "trust_score",
                    "successful_trades_count",
                    "disputed_trades_count",
                    "inactivity_timeout_count",
                    "requires_review",
                )
            },
        ),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        (
            "Reputation",
            {
                "fields": (
                    "trust_score",
                    "successful_trades_count",
                    "disputed_trades_count",
                    "inactivity_timeout_count",
                    "requires_review",
                )
            },
        ),
    )
