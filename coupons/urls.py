from django.urls import path

from .views import marketplace, upload_coupon

app_name = "coupons"
urlpatterns = [
    path("", marketplace, name="marketplace"),
    path("upload/", upload_coupon, name="upload"),
]
