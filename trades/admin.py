from django.contrib import admin

from .models import Trade, TradeEvent


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sender",
        "receiver",
        "offered_coupon",
        "requested_coupon",
        "status",
        "created_at",
        "accepted_at",
        "expires_at",
    )
    list_filter = ("status", "created_at")
    search_fields = (
        "sender__username",
        "receiver__username",
        "offered_coupon__title",
        "requested_coupon__title",
    )
    autocomplete_fields = (
        "sender",
        "receiver",
        "offered_coupon",
        "requested_coupon",
    )


@admin.register(TradeEvent)
class TradeEventAdmin(admin.ModelAdmin):
    list_display = ("trade", "event_type", "actor", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("trade__id", "actor__username")
    readonly_fields = ("trade", "actor", "event_type", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
