from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections
from django.test import TransactionTestCase

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from coupons.models import Category, Coupon, Platform
from .models import Trade, TradeEvent
from .services import (
    accept_trade,
    create_trade,
    decline_trade,
    process_expired_trade,
    process_expired_trades,
    record_trade_response,
)


class TradeWorkflowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.sender = user_model.objects.create_user(
            username="sender",
            password="safe-password",
        )
        self.receiver = user_model.objects.create_user(
            username="receiver",
            password="safe-password",
        )
        self.third_user = user_model.objects.create_user(
            username="third-user",
            password="safe-password",
        )
        category, _ = Category.objects.get_or_create(name="Food")
        platform, _ = Platform.objects.get_or_create(name="Zomato")
        expiry_date = timezone.localdate() + timedelta(days=30)
        self.offered_coupon = self._create_coupon(
            owner=self.sender,
            title="Sender coupon",
            code="SENDER20",
            category=category,
            platform=platform,
            expiry_date=expiry_date,
        )
        self.requested_coupon = self._create_coupon(
            owner=self.receiver,
            title="Receiver coupon",
            code="RECEIVER20",
            category=category,
            platform=platform,
            expiry_date=expiry_date,
        )
        self.trade = create_trade(
            sender=self.sender,
            receiver=self.receiver,
            offered_coupon=self.offered_coupon,
            requested_coupon=self.requested_coupon,
        )

    @staticmethod
    def _create_coupon(*, owner, title, code, category, platform, expiry_date):
        return Coupon.objects.create(
            owner=owner,
            category=category,
            platform=platform,
            title=title,
            code=code,
            expiry_date=expiry_date,
            proof_image=f"coupons/proofs/{code}.png",
        )

    def accept(self):
        return accept_trade(trade_id=self.trade.pk, receiver=self.receiver)

    def test_proposal_locks_only_senders_coupon(self):
        self.assertEqual(self.trade.status, Trade.Status.PROPOSED)
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(
            self.offered_coupon.status,
            Coupon.Status.LOCKED_PENDING,
        )
        self.assertEqual(
            self.requested_coupon.status,
            Coupon.Status.AVAILABLE,
        )
        self.assertIsNone(self.trade.accepted_at)
        self.assertIsNone(self.trade.expires_at)

    def test_receiver_acceptance_locks_both_and_starts_exact_15_minute_window(self):
        accepted = self.accept()

        self.assertEqual(accepted.status, Trade.Status.ACTIVE_EXCHANGE)
        self.assertEqual(
            accepted.expires_at - accepted.accepted_at,
            timedelta(minutes=15),
        )
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(self.offered_coupon.status, Coupon.Status.LOCKED_ESCROW)
        self.assertEqual(self.requested_coupon.status, Coupon.Status.LOCKED_ESCROW)

    def test_only_receiver_can_accept_or_decline(self):
        with self.assertRaises(PermissionDenied):
            accept_trade(trade_id=self.trade.pk, receiver=self.third_user)
        with self.assertRaises(PermissionDenied):
            decline_trade(trade_id=self.trade.pk, receiver=self.third_user)

        self.assertEqual(self.trade.status, Trade.Status.PROPOSED)

    def test_decline_unlocks_sender_and_leaves_receiver_coupon_available(self):
        declined = decline_trade(trade_id=self.trade.pk, receiver=self.receiver)

        self.assertEqual(declined.status, Trade.Status.CANCELLED)
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(self.offered_coupon.status, Coupon.Status.AVAILABLE)
        self.assertEqual(self.requested_coupon.status, Coupon.Status.AVAILABLE)

    def test_acceptance_cancels_other_proposals_for_requested_coupon(self):
        third_coupon = self._create_coupon(
            owner=self.third_user,
            title="Third coupon",
            code="THIRD20",
            category=self.requested_coupon.category,
            platform=self.requested_coupon.platform,
            expiry_date=self.requested_coupon.expiry_date,
        )
        competing_trade = create_trade(
            sender=self.third_user,
            receiver=self.receiver,
            offered_coupon=third_coupon,
            requested_coupon=self.requested_coupon,
        )

        self.accept()

        competing_trade.refresh_from_db()
        third_coupon.refresh_from_db()
        self.assertEqual(competing_trade.status, Trade.Status.CANCELLED)
        self.assertEqual(third_coupon.status, Coupon.Status.AVAILABLE)

    def test_expired_offered_coupon_cannot_be_proposed(self):
        expired_offer = self._create_coupon(
            owner=self.sender,
            title="Expired offer",
            code="EXPIREDOFFER",
            category=self.requested_coupon.category,
            platform=self.requested_coupon.platform,
            expiry_date=timezone.localdate() - timedelta(days=1),
        )

        with self.assertRaisesMessage(
            ValidationError,
            "The offered coupon is unavailable or has expired.",
        ):
            create_trade(
                sender=self.sender,
                receiver=self.receiver,
                offered_coupon=expired_offer,
                requested_coupon=self.requested_coupon,
            )

        expired_offer.refresh_from_db()
        self.assertEqual(expired_offer.status, Coupon.Status.AVAILABLE)

    def test_expired_requested_coupon_cannot_be_proposed(self):
        fresh_offer = self._create_coupon(
            owner=self.sender,
            title="Fresh offer",
            code="FRESHOFFER",
            category=self.requested_coupon.category,
            platform=self.requested_coupon.platform,
            expiry_date=timezone.localdate() + timedelta(days=30),
        )
        expired_request = self._create_coupon(
            owner=self.receiver,
            title="Expired request",
            code="EXPIREDREQUEST",
            category=self.requested_coupon.category,
            platform=self.requested_coupon.platform,
            expiry_date=timezone.localdate() - timedelta(days=1),
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Expired coupons cannot be traded.",
        ):
            create_trade(
                sender=self.sender,
                receiver=self.receiver,
                offered_coupon=fresh_offer,
                requested_coupon=expired_request,
            )

        fresh_offer.refresh_from_db()
        self.assertEqual(fresh_offer.status, Coupon.Status.AVAILABLE)

    def test_expired_requested_coupon_cancels_acceptance_and_expires_coupon(self):
        self.requested_coupon.expiry_date = timezone.localdate() - timedelta(days=1)
        self.requested_coupon.save(update_fields=("expiry_date",))

        accepted = self.accept()

        self.assertEqual(accepted.status, Trade.Status.CANCELLED)
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(self.offered_coupon.status, Coupon.Status.AVAILABLE)
        self.assertEqual(self.requested_coupon.status, Coupon.Status.EXPIRED)

    def test_unavailable_requested_coupon_cancels_proposal_and_unlocks_sender(self):
        self.requested_coupon.status = Coupon.Status.REDEEMED
        self.requested_coupon.save(update_fields={"status"})

        accepted = self.accept()

        self.assertEqual(accepted.status, Trade.Status.CANCELLED)
        self.offered_coupon.refresh_from_db()
        self.assertEqual(self.offered_coupon.status, Coupon.Status.AVAILABLE)

    def test_flagged_account_cannot_trade_and_flagged_proposal_is_released(self):
        self.sender.requires_review = True
        self.sender.save(update_fields=("requires_review",))

        with self.assertRaisesMessage(
            ValidationError,
            "Your account must complete review before trading.",
        ):
            create_trade(
                sender=self.sender,
                receiver=self.receiver,
                offered_coupon=self.offered_coupon,
                requested_coupon=self.requested_coupon,
            )

        accepted = self.accept()

        self.assertEqual(accepted.status, Trade.Status.CANCELLED)
        self.offered_coupon.refresh_from_db()
        self.assertEqual(self.offered_coupon.status, Coupon.Status.AVAILABLE)

    def test_only_participants_see_codes_and_only_after_acceptance(self):
        self.client.force_login(self.sender)
        sender_pending = self.client.get(
            reverse("trades:room", args=(self.trade.pk,))
        )
        self.assertNotContains(sender_pending, self.offered_coupon.code)
        self.assertNotContains(sender_pending, self.requested_coupon.code)

        outsider = get_user_model().objects.create_user(
            username="outsider",
            password="safe-password",
        )
        self.client.force_login(outsider)
        self.assertEqual(
            self.client.get(reverse("trades:room", args=(self.trade.pk,))).status_code,
            403,
        )

        self.accept()
        self.client.force_login(self.sender)
        sender_room = self.client.get(reverse("trades:room", args=(self.trade.pk,)))
        self.assertContains(sender_room, self.requested_coupon.code)
        self.assertNotContains(sender_room, self.offered_coupon.code)
        self.assertContains(sender_room, "Code verified")
        self.assertContains(sender_room, "Code did not work")

        self.client.force_login(self.receiver)
        receiver_room = self.client.get(reverse("trades:room", args=(self.trade.pk,)))
        self.assertContains(receiver_room, self.offered_coupon.code)
        self.assertNotContains(receiver_room, self.requested_coupon.code)

    def test_receiver_can_accept_or_decline_from_trade_room(self):
        self.client.force_login(self.receiver)
        accept_response = self.client.post(
            reverse("trades:accept", args=(self.trade.pk,))
        )
        self.assertRedirects(
            accept_response,
            reverse("trades:room", args=(self.trade.pk,)),
        )
        self.trade.refresh_from_db()
        self.assertEqual(self.trade.status, Trade.Status.ACTIVE_EXCHANGE)

        another_trade = create_trade(
            sender=self.sender,
            receiver=self.third_user,
            offered_coupon=self._create_coupon(
                owner=self.sender,
                title="Extra sender coupon",
                code="EXTRA20",
                category=self.requested_coupon.category,
                platform=self.requested_coupon.platform,
                expiry_date=self.requested_coupon.expiry_date,
            ),
            requested_coupon=self._create_coupon(
                owner=self.third_user,
                title="Third party coupon",
                code="THIRDPARTY20",
                category=self.requested_coupon.category,
                platform=self.requested_coupon.platform,
                expiry_date=self.requested_coupon.expiry_date,
            ),
        )
        self.client.force_login(self.third_user)
        decline_response = self.client.post(
            reverse("trades:decline", args=(another_trade.pk,))
        )
        self.assertRedirects(
            decline_response,
            reverse("trades:room", args=(another_trade.pk,)),
        )
        another_trade.refresh_from_db()
        self.assertEqual(another_trade.status, Trade.Status.CANCELLED)

    def test_proposal_view_locks_only_the_requesters_offered_coupon(self):
        sender_coupon = self._create_coupon(
            owner=self.third_user,
            title="Third user's offer",
            code="THIRDOFFER20",
            category=self.requested_coupon.category,
            platform=self.requested_coupon.platform,
            expiry_date=self.requested_coupon.expiry_date,
        )
        self.client.force_login(self.third_user)

        response = self.client.post(
            reverse("trades:propose", args=(self.requested_coupon.pk,)),
            {"offered_coupon": sender_coupon.pk},
        )

        proposed_trade = Trade.objects.get(
            sender=self.third_user,
            offered_coupon=sender_coupon,
        )
        self.assertRedirects(
            response,
            reverse("trades:room", args=(proposed_trade.pk,)),
        )
        sender_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(sender_coupon.status, Coupon.Status.LOCKED_PENDING)
        self.assertEqual(self.requested_coupon.status, Coupon.Status.AVAILABLE)

    def test_incoming_requests_only_show_receivers_pending_trades(self):
        self.client.force_login(self.receiver)

        response = self.client.get(reverse("trades:incoming"))

        self.assertContains(response, self.offered_coupon.title)
        self.assertNotContains(response, self.offered_coupon.code)
        self.assertContains(response, reverse("trades:room", args=(self.trade.pk,)))

        self.client.force_login(self.sender)
        sender_response = self.client.get(reverse("trades:incoming"))
        self.assertNotContains(sender_response, self.offered_coupon.title)

    def test_dashboard_lists_scoped_activity_without_coupon_codes(self):
        listing = self._create_coupon(
            owner=self.sender,
            title="Dashboard listing",
            code="DASHSECRET",
            category=self.requested_coupon.category,
            platform=self.requested_coupon.platform,
            expiry_date=self.requested_coupon.expiry_date,
        )
        self.client.force_login(self.sender)

        response = self.client.get(reverse("trades:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active listings")
        self.assertContains(response, "Outgoing requests")
        self.assertContains(response, self.offered_coupon.title)
        self.assertContains(response, listing.title)
        self.assertNotContains(response, self.offered_coupon.code)
        self.assertNotContains(response, self.requested_coupon.code)
        self.assertNotContains(response, listing.code)
        self.assertEqual(
            list(response.context["audit_events"].values_list("trade_id", flat=True)),
            [self.trade.pk],
        )
        self.assertNotIn("code", response.context["listings"][0])

        outsider = get_user_model().objects.create_user(
            username="dashboard-outsider",
        )
        self.client.force_login(outsider)
        outsider_response = self.client.get(reverse("trades:dashboard"))
        self.assertNotContains(outsider_response, self.offered_coupon.title)
        self.assertNotContains(outsider_response, self.requested_coupon.title)
        self.assertFalse(outsider_response.context["audit_events"].exists())

    def test_both_verified_complete_trade_and_reward_both_users(self):
        self.sender.trust_score = 90
        self.receiver.trust_score = 90
        self.sender.save(update_fields=("trust_score",))
        self.receiver.save(update_fields=("trust_score",))
        self.accept()
        record_trade_response(
            trade_id=self.trade.pk,
            user=self.sender,
            response="verified",
        )
        self.trade.refresh_from_db()
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(self.trade.status, Trade.Status.ACTIVE_EXCHANGE)
        self.assertEqual(
            self.offered_coupon.status,
            Coupon.Status.LOCKED_ESCROW,
        )
        self.assertEqual(
            self.requested_coupon.status,
            Coupon.Status.LOCKED_ESCROW,
        )
        completed = record_trade_response(
            trade_id=self.trade.pk,
            user=self.receiver,
            response="verified",
        )

        self.assertEqual(completed.status, Trade.Status.COMPLETED)
        self.sender.refresh_from_db()
        self.receiver.refresh_from_db()
        self.assertEqual(self.sender.trust_score, 93)
        self.assertEqual(self.receiver.trust_score, 93)
        self.assertEqual(self.sender.successful_trades_count, 1)
        self.assertEqual(self.receiver.successful_trades_count, 1)
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(self.offered_coupon.status, Coupon.Status.TRADED)
        self.assertEqual(self.requested_coupon.status, Coupon.Status.TRADED)
        self.assertEqual(
            list(self.trade.events.values_list("event_type", flat=True)),
            [
                TradeEvent.Type.PROPOSED,
                TradeEvent.Type.ACCEPTED,
                TradeEvent.Type.VERIFIED,
                TradeEvent.Type.VERIFIED,
                TradeEvent.Type.COMPLETED,
            ],
        )

    def test_invalid_code_disputes_and_quarantines_both_coupons(self):
        self.accept()
        disputed = record_trade_response(
            trade_id=self.trade.pk,
            user=self.receiver,
            response="invalid",
        )

        self.assertEqual(disputed.status, Trade.Status.DISPUTED)
        self.assertEqual(
            disputed.dispute_reason,
            Trade.DisputeReason.INVALID_CODE,
        )
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(
            self.offered_coupon.status,
            Coupon.Status.SUSPENDED_REVIEW,
        )
        self.assertEqual(
            self.requested_coupon.status,
            Coupon.Status.SUSPENDED_REVIEW,
        )
        self.sender.refresh_from_db()
        self.receiver.refresh_from_db()
        self.assertEqual(self.sender.disputed_trades_count, 1)
        self.assertEqual(self.receiver.disputed_trades_count, 1)
        self.assertTrue(self.sender.requires_review)
        self.assertTrue(self.receiver.requires_review)
        self.assertEqual(self.sender.trust_score, 100)
        self.assertEqual(self.receiver.trust_score, 100)
        self.assertEqual(self.sender.dispute_rate_percent, 100)
        self.assertIsNone(self.third_user.dispute_rate_percent)

        with self.assertRaises(ValidationError):
            record_trade_response(
                trade_id=self.trade.pk,
                user=self.sender,
                response="verified",
            )

    def test_disputed_participants_appear_in_admin_review_filter(self):
        self.accept()
        record_trade_response(
            trade_id=self.trade.pk,
            user=self.receiver,
            response="invalid",
        )
        admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="safe-password",
        )
        self.client.force_login(admin_user)

        response = self.client.get(
            reverse("admin:accounts_user_changelist"),
            {"requires_review__exact": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.sender.username)
        self.assertContains(response, self.receiver.username)
        self.assertContains(response, "Dispute rate")

    def test_one_verified_at_timeout_completes_and_penalizes_silent_user(self):
        self.sender.trust_score = 90
        self.sender.save(update_fields=("trust_score",))
        self.accept()
        self.trade.refresh_from_db()
        self.trade.sender_verified_at = self.trade.accepted_at + timedelta(minutes=1)
        self.trade.expires_at = timezone.now() - timedelta(seconds=1)
        self.trade.save(update_fields=("sender_verified_at", "expires_at"))

        expired = process_expired_trade(self.trade.pk)

        self.assertEqual(expired.status, Trade.Status.COMPLETED)
        self.sender.refresh_from_db()
        self.receiver.refresh_from_db()
        self.assertEqual(self.sender.trust_score, 93)
        self.assertEqual(self.receiver.trust_score, 85)
        self.assertEqual(self.sender.successful_trades_count, 1)
        self.assertEqual(self.receiver.successful_trades_count, 0)
        self.assertEqual(self.receiver.inactivity_timeout_count, 1)
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(self.offered_coupon.status, Coupon.Status.TRADED)
        self.assertEqual(self.requested_coupon.status, Coupon.Status.TRADED)

    def test_no_verification_at_timeout_quarantines_and_penalizes_both(self):
        self.accept()
        self.trade.refresh_from_db()
        self.trade.expires_at = timezone.now() - timedelta(seconds=1)
        self.trade.save(update_fields=("expires_at",))

        expired = process_expired_trade(self.trade.pk)

        self.assertEqual(expired.status, Trade.Status.DISPUTED)
        self.assertEqual(
            expired.dispute_reason,
            Trade.DisputeReason.TIMEOUT_ABANDONMENT,
        )
        self.sender.refresh_from_db()
        self.receiver.refresh_from_db()
        self.assertEqual(self.sender.trust_score, 75)
        self.assertEqual(self.receiver.trust_score, 75)
        self.assertTrue(self.sender.requires_review)
        self.assertTrue(self.receiver.requires_review)
        self.offered_coupon.refresh_from_db()
        self.requested_coupon.refresh_from_db()
        self.assertEqual(
            self.offered_coupon.status,
            Coupon.Status.SUSPENDED_REVIEW,
        )
        self.assertEqual(
            self.requested_coupon.status,
            Coupon.Status.SUSPENDED_REVIEW,
        )

    def test_expiry_job_finds_and_resolves_due_trades(self):
        self.accept()
        Trade.objects.filter(pk=self.trade.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        processed = process_expired_trades()

        self.assertEqual(processed, 1)
        self.trade.refresh_from_db()
        self.assertEqual(self.trade.status, Trade.Status.DISPUTED)

    def test_expired_room_resolves_trade_without_disclosing_codes(self):
        self.accept()
        Trade.objects.filter(pk=self.trade.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.client.force_login(self.sender)

        response = self.client.get(reverse("trades:room", args=(self.trade.pk,)))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.requested_coupon.code)
        self.trade.refresh_from_db()
        self.assertEqual(self.trade.status, Trade.Status.DISPUTED)


class ReputationPenaltyTests(TestCase):
    def test_inactivity_penalty_clamps_trust_and_flags_repeated_timeouts(self):
        user = get_user_model().objects.create_user(
            username="inactive",
            password="safe-password",
        )

        user.apply_inactivity_penalty()
        user.refresh_from_db()
        self.assertEqual(user.trust_score, 85)
        self.assertFalse(user.requires_review)

        user.apply_inactivity_penalty()
        user.refresh_from_db()
        self.assertEqual(user.trust_score, 70)
        self.assertTrue(user.requires_review)

        user.trust_score = 10
        user.save(update_fields=("trust_score",))
        user.apply_inactivity_penalty(heavy=True)
        user.refresh_from_db()
        self.assertEqual(user.trust_score, 0)


class TradeConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        user_model = get_user_model()
        self.sender = user_model.objects.create_user(
            username="race-sender",
        )
        self.receiver = user_model.objects.create_user(
            username="race-receiver",
        )
        self.other_receiver = user_model.objects.create_user(
            username="race-other-receiver",
        )
        self.other_sender = user_model.objects.create_user(
            username="race-other-sender",
        )
        self.category = Category.objects.create(name="Race category")
        self.platform = Platform.objects.create(name="Race platform")
        self.expiry_date = timezone.localdate() + timedelta(days=30)

    def create_coupon(self, owner, title, code):
        return Coupon.objects.create(
            owner=owner,
            category=self.category,
            platform=self.platform,
            title=title,
            code=code,
            expiry_date=self.expiry_date,
            proof_image=f"coupons/proofs/{code}.png",
        )

    @staticmethod
    def run_together(functions):
        barrier = Barrier(len(functions))

        def run(function):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(functions)) as executor:
            futures = [executor.submit(run, function) for function in functions]
            return [future.result(timeout=20) for future in futures]

    def test_competing_proposals_cannot_lock_the_same_offered_coupon(self):
        offered = self.create_coupon(self.sender, "Shared offer", "RACE-SHARED")
        requested = self.create_coupon(self.receiver, "First request", "RACE-FIRST")
        other_requested = self.create_coupon(
            self.other_receiver,
            "Second request",
            "RACE-SECOND",
        )

        def propose(receiver, requested_coupon):
            try:
                trade = create_trade(
                    sender=self.sender,
                    receiver=receiver,
                    offered_coupon=offered,
                    requested_coupon=requested_coupon,
                )
            except ValidationError:
                return None
            return trade.pk

        results = self.run_together(
            (
                lambda: propose(self.receiver, requested),
                lambda: propose(self.other_receiver, other_requested),
            )
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(Trade.objects.count(), 1)
        offered.refresh_from_db()
        self.assertEqual(offered.status, Coupon.Status.LOCKED_PENDING)

    def test_competing_acceptances_only_activate_one_trade_for_a_coupon(self):
        requested = self.create_coupon(self.receiver, "Shared request", "RACE-TARGET")
        first_offer = self.create_coupon(self.sender, "First offer", "RACE-OFFER1")
        second_offer = self.create_coupon(
            self.other_sender,
            "Second offer",
            "RACE-OFFER2",
        )
        first_trade = create_trade(
            sender=self.sender,
            receiver=self.receiver,
            offered_coupon=first_offer,
            requested_coupon=requested,
        )
        second_trade = create_trade(
            sender=self.other_sender,
            receiver=self.receiver,
            offered_coupon=second_offer,
            requested_coupon=requested,
        )

        def accept(trade_id):
            try:
                return accept_trade(
                    trade_id=trade_id,
                    receiver=self.receiver,
                ).status
            except ValidationError:
                return "REJECTED"

        results = self.run_together(
            (
                lambda: accept(first_trade.pk),
                lambda: accept(second_trade.pk),
            )
        )

        self.assertEqual(
            sum(status == Trade.Status.ACTIVE_EXCHANGE for status in results),
            1,
        )
        first_trade.refresh_from_db()
        second_trade.refresh_from_db()
        requested.refresh_from_db()
        self.assertEqual(
            sum(
                trade.status == Trade.Status.ACTIVE_EXCHANGE
                for trade in (first_trade, second_trade)
            ),
            1,
        )
        self.assertEqual(requested.status, Coupon.Status.LOCKED_ESCROW)
