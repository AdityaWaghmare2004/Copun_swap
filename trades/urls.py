from django.urls import path

from . import views

app_name = "trades"
urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("", views.incoming_requests, name="incoming"),
    path(
        "propose/<int:requested_coupon_id>/",
        views.propose_trade,
        name="propose",
    ),
    path("<int:trade_id>/", views.trade_room, name="room"),
    path("<int:trade_id>/accept/", views.accept_trade_view, name="accept"),
    path("<int:trade_id>/decline/", views.decline_trade_view, name="decline"),
    path("<int:trade_id>/respond/", views.respond_to_trade, name="respond"),
]
