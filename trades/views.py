from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from coupons.models import Coupon

from .forms import TradeProposalForm
from .models import Trade, TradeEvent
from .services import (
    accept_trade,
    create_trade,
    decline_trade,
    process_expired_trade,
    record_trade_response,
)


@login_required
def incoming_requests(request):
    trades = Trade.objects.filter(
        receiver=request.user,
        status=Trade.Status.PROPOSED,
    ).select_related("sender", "offered_coupon", "requested_coupon").only(
        "pk",
        "status",
        "created_at",
        "sender_id",
        "sender__username",
        "receiver_id",
        "offered_coupon_id",
        "offered_coupon__title",
        "requested_coupon_id",
        "requested_coupon__title",
    )
    return render(request, "trades/incoming_requests.html", {"trades": trades})


@login_required
def dashboard(request):
    today = timezone.localdate()
    listings = Coupon.objects.filter(
        owner=request.user,
        status=Coupon.Status.AVAILABLE,
        expiry_date__gte=today,
    ).values("pk", "title", "expiry_date", "category__name", "platform__name")

    participant_trades = Trade.objects.filter(
        models.Q(sender=request.user) | models.Q(receiver=request.user)
    )
    pending_fields = (
        "pk",
        "status",
        "created_at",
        "sender_id",
        "sender__username",
        "receiver_id",
        "receiver__username",
        "offered_coupon_id",
        "offered_coupon__title",
        "requested_coupon_id",
        "requested_coupon__title",
    )
    incoming = participant_trades.filter(
        receiver=request.user,
        status=Trade.Status.PROPOSED,
    ).select_related("sender", "offered_coupon", "requested_coupon").only(
        *pending_fields
    )
    outgoing = participant_trades.filter(
        sender=request.user,
        status=Trade.Status.PROPOSED,
    ).select_related("sender", "receiver", "offered_coupon", "requested_coupon").only(
        *pending_fields
    )
    active = participant_trades.filter(
        status=Trade.Status.ACTIVE_EXCHANGE,
    ).select_related("sender", "receiver", "offered_coupon", "requested_coupon").only(
        *pending_fields,
        "accepted_at",
        "expires_at",
        "sender_verified_at",
        "receiver_verified_at",
    )
    history = participant_trades.filter(
        status__in=(
            Trade.Status.COMPLETED,
            Trade.Status.DISPUTED,
            Trade.Status.CANCELLED,
        )
    ).select_related("sender", "receiver", "offered_coupon", "requested_coupon").only(
        *pending_fields,
        "dispute_reason",
        "updated_at",
    )
    events = TradeEvent.objects.filter(
        trade__in=participant_trades,
    ).select_related("actor").only(
        "pk",
        "trade_id",
        "event_type",
        "created_at",
        "actor_id",
        "actor__username",
    )

    return render(
        request,
        "trades/dashboard.html",
        {
            "listings": listings,
            "incoming_requests": incoming,
            "outgoing_requests": outgoing,
            "active_trades": active,
            "trade_history": history,
            "audit_events": events,
        },
    )


@login_required
def propose_trade(request, requested_coupon_id):
    requested_coupon = get_object_or_404(
        Coupon.objects.only("pk", "owner_id", "title", "status", "expiry_date"),
        pk=requested_coupon_id,
        status=Coupon.Status.AVAILABLE,
    )
    if requested_coupon.owner_id == request.user.pk:
        raise PermissionDenied("You cannot propose a trade for your own coupon.")
    requested_owner = get_user_model().objects.get(pk=requested_coupon.owner_id)

    form = TradeProposalForm(
        request.POST or None,
        sender=request.user,
    )
    if request.method == "POST" and form.is_valid():
        try:
            trade = create_trade(
                sender=request.user,
                receiver=requested_owner,
                offered_coupon=form.cleaned_data["offered_coupon"],
                requested_coupon=requested_coupon,
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "Trade request sent.")
            return redirect("trades:room", trade_id=trade.pk)

    return render(
        request,
        "trades/propose.html",
        {"form": form, "requested_coupon": requested_coupon},
    )


@login_required
def trade_room(request, trade_id):
    trade = get_object_or_404(
        Trade.objects.only(
            "pk",
            "sender_id",
            "receiver_id",
            "status",
            "expires_at",
            "offered_coupon_id",
            "requested_coupon_id",
        ),
        pk=trade_id,
    )
    if request.user.pk not in (trade.sender_id, trade.receiver_id):
        raise PermissionDenied("Only trade participants can view this room.")

    if (
        trade.status == Trade.Status.ACTIVE_EXCHANGE
        and trade.expires_at <= timezone.now()
    ):
        process_expired_trade(trade.pk)
        trade.refresh_from_db()

    trade = Trade.objects.select_related(
        "sender",
        "receiver",
        "offered_coupon",
        "requested_coupon",
    ).only(
        "pk",
        "sender_id",
        "sender__username",
        "receiver_id",
        "receiver__username",
        "status",
        "expires_at",
        "offered_coupon_id",
        "offered_coupon__title",
        "requested_coupon_id",
        "requested_coupon__title",
    ).get(pk=trade.pk)
    offered_code = None
    requested_code = None
    if trade.status == Trade.Status.ACTIVE_EXCHANGE:
        if request.user.pk == trade.sender_id:
            requested_code = Coupon.objects.filter(
                pk=trade.requested_coupon_id
            ).values_list("code", flat=True).get()
        else:
            offered_code = Coupon.objects.filter(
                pk=trade.offered_coupon_id
            ).values_list("code", flat=True).get()

    return render(
        request,
        "trades/room.html",
        {
            "trade": trade,
            "offered_code": offered_code,
            "requested_code": requested_code,
        },
    )


@login_required
def accept_trade_view(request, trade_id):
    if request.method != "POST":
        raise Http404
    try:
        trade = accept_trade(trade_id=trade_id, receiver=request.user)
    except Trade.DoesNotExist as error:
        raise Http404("Trade not found.") from error
    except ValidationError as error:
        messages.error(request, error.messages[0])
        return redirect("trades:incoming")

    if trade.status == Trade.Status.CANCELLED:
        messages.warning(
            request,
            "The trade request is no longer eligible for acceptance and was cancelled.",
        )
    else:
        messages.success(
            request,
            "Trade accepted. The 15-minute verification window is active.",
        )
    return redirect("trades:room", trade_id=trade.pk)


@login_required
def decline_trade_view(request, trade_id):
    if request.method != "POST":
        raise Http404
    try:
        trade = decline_trade(trade_id=trade_id, receiver=request.user)
    except Trade.DoesNotExist as error:
        raise Http404("Trade not found.") from error
    except ValidationError as error:
        messages.error(request, error.messages[0])
        return redirect("trades:incoming")
    messages.info(request, "Trade request declined.")
    return redirect("trades:room", trade_id=trade.pk)


@login_required
def respond_to_trade(request, trade_id):
    if request.method != "POST":
        raise Http404
    response = request.POST.get("response")
    try:
        trade = record_trade_response(
            trade_id=trade_id,
            user=request.user,
            response=response,
        )
    except Trade.DoesNotExist as error:
        raise Http404("Trade not found.") from error
    except ValidationError as error:
        messages.error(request, error.messages[0])
        return redirect("trades:room", trade_id=trade_id)

    if trade.status == Trade.Status.DISPUTED:
        messages.warning(request, "The trade is disputed and both coupons are quarantined.")
    elif trade.status == Trade.Status.COMPLETED:
        messages.success(request, "The trade is complete.")
    return redirect("trades:room", trade_id=trade.pk)
