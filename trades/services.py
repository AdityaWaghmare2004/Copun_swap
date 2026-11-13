from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from coupons.models import Coupon

from .models import Trade, TradeEvent


def _lock_coupons(coupon_ids):
    return {
        coupon.pk: coupon
        for coupon in Coupon.objects.select_for_update()
        .filter(pk__in=sorted(set(coupon_ids)))
        .only("pk", "owner_id", "status", "expiry_date", "title")
        .order_by("pk")
    }


def _record_event(trade, event_type, actor=None):
    return TradeEvent.objects.create(
        trade=trade,
        actor=actor,
        event_type=event_type,
    )


def _cancel_proposal(
    trade,
    offered_coupon,
    actor=None,
    event_type=TradeEvent.Type.CANCELLED,
):
    _record_event(trade, event_type, actor)
    trade.status = Trade.Status.CANCELLED
    trade.save(update_fields=("status", "updated_at"))
    today = timezone.localdate()
    if offered_coupon.expiry_date < today:
        released_status = Coupon.Status.EXPIRED
    else:
        released_status = Coupon.Status.AVAILABLE
    Coupon.objects.filter(
        pk=offered_coupon.pk,
        status=Coupon.Status.LOCKED_PENDING,
    ).update(status=released_status)
    requested_coupon = Coupon.objects.filter(
        pk=trade.requested_coupon_id,
        status=Coupon.Status.AVAILABLE,
        expiry_date__lt=today,
    )
    requested_coupon.update(status=Coupon.Status.EXPIRED)


def _cancel_incoming_proposals(coupon_id):
    proposals = Trade.objects.select_for_update().filter(
        requested_coupon_id=coupon_id,
        status=Trade.Status.PROPOSED,
    )
    for proposal in proposals:
        offered_coupon = Coupon.objects.select_for_update().get(
            pk=proposal.offered_coupon_id
        )
        _cancel_proposal(proposal, offered_coupon)


@transaction.atomic
def create_trade(*, sender, receiver, offered_coupon, requested_coupon):
    if sender.pk == receiver.pk:
        raise ValidationError("A trade must involve two different users.")
    if sender.requires_review:
        raise ValidationError("Your account must complete review before trading.")
    if offered_coupon.pk is None or requested_coupon.pk is None:
        raise ValidationError("Both coupons must be saved before creating a trade.")
    if offered_coupon.pk == requested_coupon.pk:
        raise ValidationError("A coupon cannot be traded for itself.")

    updated = Coupon.objects.filter(
        pk=offered_coupon.pk,
        status=Coupon.Status.AVAILABLE,
        owner_id=sender.pk,
        expiry_date__gte=timezone.localdate(),
    ).update(status=Coupon.Status.LOCKED_PENDING)
    if updated != 1:
        raise ValidationError(
            "The offered coupon is unavailable or has expired."
        )

    coupons = _lock_coupons((offered_coupon.pk, requested_coupon.pk))
    if len(coupons) != 2:
        raise ValidationError("Both coupons must exist to create a trade.")

    offered = coupons[offered_coupon.pk]
    requested = coupons[requested_coupon.pk]
    if offered.owner_id != sender.pk:
        raise ValidationError("The sender must own the offered coupon.")
    if requested.owner_id != receiver.pk:
        raise ValidationError("The receiver must own the requested coupon.")
    if (
        offered.status != Coupon.Status.LOCKED_PENDING
        or requested.status != Coupon.Status.AVAILABLE
    ):
        raise ValidationError("Both coupons must be available to propose a trade.")
    if (
        offered.expiry_date < timezone.localdate()
        or requested.expiry_date < timezone.localdate()
    ):
        raise ValidationError("Expired coupons cannot be traded.")

    trade = Trade.objects.create(
        sender=sender,
        receiver=receiver,
        offered_coupon=offered,
        requested_coupon=requested,
        status=Trade.Status.PROPOSED,
    )
    _record_event(trade, TradeEvent.Type.PROPOSED, sender)
    offered.status = Coupon.Status.LOCKED_PENDING
    _cancel_incoming_proposals(offered.pk)
    return trade


@transaction.atomic
def accept_trade(*, trade_id, receiver):
    claimed = Trade.objects.filter(
        pk=trade_id,
        receiver_id=receiver.pk,
        status=Trade.Status.PROPOSED,
    ).update(updated_at=F("updated_at"))
    trade = Trade.objects.select_for_update().get(pk=trade_id)
    if trade.receiver_id != receiver.pk:
        raise PermissionDenied("Only the receiver can accept this trade.")
    if claimed != 1 or trade.status != Trade.Status.PROPOSED:
        raise ValidationError("This trade is no longer awaiting a response.")

    coupons = _lock_coupons((trade.offered_coupon_id, trade.requested_coupon_id))
    offered = coupons.get(trade.offered_coupon_id)
    requested = coupons.get(trade.requested_coupon_id)
    if offered is None or requested is None:
        raise ValidationError("A coupon in this trade no longer exists.")

    if trade.sender.requires_review or receiver.requires_review:
        _cancel_proposal(trade, offered)
        return trade

    if (
        offered.status != Coupon.Status.LOCKED_PENDING
        or requested.status != Coupon.Status.AVAILABLE
        or offered.expiry_date < timezone.localdate()
        or requested.expiry_date < timezone.localdate()
    ):
        _cancel_proposal(trade, offered)
        return trade

    _record_event(trade, TradeEvent.Type.ACCEPTED, receiver)
    offered_updated = Coupon.objects.filter(
        pk=offered.pk,
        status=Coupon.Status.LOCKED_PENDING,
    ).update(status=Coupon.Status.LOCKED_ESCROW)
    requested_updated = Coupon.objects.filter(
        pk=requested.pk,
        status=Coupon.Status.AVAILABLE,
        owner_id=receiver.pk,
    ).update(status=Coupon.Status.LOCKED_ESCROW)
    if offered_updated != 1 or requested_updated != 1:
        raise ValidationError("The coupons changed while the trade was being accepted.")

    offered.status = Coupon.Status.LOCKED_ESCROW
    requested.status = Coupon.Status.LOCKED_ESCROW
    trade.start_verification_window()
    trade.save(
        update_fields=(
            "status",
            "accepted_at",
            "expires_at",
            "updated_at",
        )
    )
    _cancel_incoming_proposals(requested.pk)
    return trade


@transaction.atomic
def decline_trade(*, trade_id, receiver):
    trade = Trade.objects.select_for_update().get(pk=trade_id)
    if trade.receiver_id != receiver.pk:
        raise PermissionDenied("Only the receiver can decline this trade.")
    if trade.status != Trade.Status.PROPOSED:
        raise ValidationError("This trade is no longer awaiting a response.")

    offered = Coupon.objects.select_for_update().get(pk=trade.offered_coupon_id)
    _cancel_proposal(
        trade,
        offered,
        receiver,
        TradeEvent.Type.DECLINED,
    )
    return trade


def _lock_trade_and_coupons(trade_id):
    trade = Trade.objects.select_for_update().get(pk=trade_id)
    coupons = _lock_coupons((trade.offered_coupon_id, trade.requested_coupon_id))
    return trade, coupons[trade.offered_coupon_id], coupons[trade.requested_coupon_id]


def _complete_trade(
    trade,
    offered,
    requested,
    rewarded_users,
    actor=None,
    event_type=TradeEvent.Type.COMPLETED,
):
    offered.status = Coupon.Status.TRADED
    requested.status = Coupon.Status.TRADED
    offered.save(update_fields=("status",))
    requested.save(update_fields=("status",))
    trade.status = Trade.Status.COMPLETED
    trade.save(update_fields=("status", "updated_at"))
    _record_event(trade, event_type, actor)
    for user in rewarded_users:
        user.apply_success_reward()


def _dispute_trade(trade, offered, requested, reason, actor=None):
    offered.status = Coupon.Status.SUSPENDED_REVIEW
    requested.status = Coupon.Status.SUSPENDED_REVIEW
    offered.save(update_fields=("status",))
    requested.save(update_fields=("status",))
    trade.status = Trade.Status.DISPUTED
    trade.dispute_reason = reason
    trade.save(update_fields=("status", "dispute_reason", "updated_at"))
    event_type = (
        TradeEvent.Type.INVALID_CODE_REPORTED
        if reason == Trade.DisputeReason.INVALID_CODE
        else TradeEvent.Type.TIMEOUT_DISPUTED
    )
    _record_event(trade, event_type, actor)
    user_model = type(trade.sender)
    user_model.objects.filter(pk=trade.sender_id).update(
        disputed_trades_count=F("disputed_trades_count") + 1,
        requires_review=True,
    )
    user_model.objects.filter(pk=trade.receiver_id).update(
        disputed_trades_count=F("disputed_trades_count") + 1,
        requires_review=True,
    )
    trade.sender.refresh_from_db(
        fields=("disputed_trades_count", "requires_review")
    )
    trade.receiver.refresh_from_db(
        fields=("disputed_trades_count", "requires_review")
    )


@transaction.atomic
def record_trade_response(*, trade_id, user, response):
    trade, offered, requested = _lock_trade_and_coupons(trade_id)
    if user.pk not in (trade.sender_id, trade.receiver_id):
        raise PermissionDenied("Only a trade participant can respond.")
    if trade.status != Trade.Status.ACTIVE_EXCHANGE:
        raise ValidationError("This trade is not in its verification window.")

    if trade.expires_at <= timezone.now():
        _resolve_expired_trade(trade, offered, requested)
        return trade

    if response == "invalid":
        _dispute_trade(
            trade,
            offered,
            requested,
            Trade.DisputeReason.INVALID_CODE,
            actor=user,
        )
        return trade

    if response != "verified":
        raise ValidationError("Choose a valid trade response.")

    field = (
        "sender_verified_at"
        if user.pk == trade.sender_id
        else "receiver_verified_at"
    )
    if getattr(trade, field) is None:
        setattr(trade, field, timezone.now())
        trade.save(update_fields=(field, "updated_at"))
        _record_event(trade, TradeEvent.Type.VERIFIED, user)

    if trade.sender_verified_at and trade.receiver_verified_at:
        _complete_trade(
            trade,
            offered,
            requested,
            (trade.sender, trade.receiver),
            actor=user,
        )
    return trade


def _resolve_expired_trade(trade, offered, requested):
    if trade.status != Trade.Status.ACTIVE_EXCHANGE or trade.expires_at > timezone.now():
        return trade

    sender_verified = trade.sender_verified_at is not None
    receiver_verified = trade.receiver_verified_at is not None

    if sender_verified and receiver_verified:
        _complete_trade(
            trade,
            offered,
            requested,
            (trade.sender, trade.receiver),
            event_type=TradeEvent.Type.TIMEOUT_COMPLETED,
        )
        return trade

    if sender_verified or receiver_verified:
        if sender_verified:
            trade.sender.apply_success_reward()
            trade.receiver.apply_inactivity_penalty()
        else:
            trade.receiver.apply_success_reward()
            trade.sender.apply_inactivity_penalty()
        _complete_trade(
            trade,
            offered,
            requested,
            (),
            event_type=TradeEvent.Type.TIMEOUT_COMPLETED,
        )
        return trade

    _dispute_trade(
        trade,
        offered,
        requested,
        Trade.DisputeReason.TIMEOUT_ABANDONMENT,
    )
    trade.sender.apply_inactivity_penalty(heavy=True)
    trade.receiver.apply_inactivity_penalty(heavy=True)
    return trade


@transaction.atomic
def process_expired_trade(trade_id):
    trade, offered, requested = _lock_trade_and_coupons(trade_id)
    return _resolve_expired_trade(trade, offered, requested)


def process_expired_trades():
    now = timezone.now()
    expired_ids = list(
        Trade.objects.filter(
            status=Trade.Status.ACTIVE_EXCHANGE,
            expires_at__lte=now,
        ).values_list("pk", flat=True)
    )
    processed = 0
    for trade_id in expired_ids:
        trade = process_expired_trade(trade_id)
        if trade.status != Trade.Status.ACTIVE_EXCHANGE:
            processed += 1
    return processed
