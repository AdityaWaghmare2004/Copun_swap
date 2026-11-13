from django.contrib import admin

from .models import Category, Coupon, Platform


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "category", "platform", "expiry_date", "status")
    list_filter = ("status", "category", "platform", "expiry_date")
    search_fields = ("title", "code", "owner__username")
    autocomplete_fields = ("owner", "category", "platform")
