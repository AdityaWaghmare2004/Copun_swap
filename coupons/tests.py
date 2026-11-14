import hashlib
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytesseract
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .forms import CouponUploadForm, MAX_COUPON_IMAGE_SIZE
from .models import Category, Coupon, Platform
from .services.ocr import OCRBusyError, OCRTimeoutError, ParsedCouponText
from .views import _pending_storage


class CouponModelTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="coupon-owner",
            password="safe-password",
        )
        self.category, _ = Category.objects.get_or_create(name="Food")
        self.platform, _ = Platform.objects.get_or_create(name="Zomato")

    def test_coupon_defaults_and_relations(self):
        coupon = Coupon.objects.create(
            owner=self.owner,
            category=self.category,
            platform=self.platform,
            title="Lunch discount",
            code="LUNCH20",
            expiry_date="2030-12-31",
            proof_image="coupons/proofs/lunch.png",
        )

        self.assertEqual(coupon.status, Coupon.Status.AVAILABLE)
        self.assertEqual(coupon.owner, self.owner)
        self.assertEqual(coupon.category, self.category)
        self.assertEqual(coupon.platform, self.platform)
        self.assertEqual(coupon.terms, "")

    def test_coupon_accepts_each_status_choice(self):
        for status, _label in Coupon.Status.choices:
            with self.subTest(status=status):
                coupon = Coupon.objects.create(
                    owner=self.owner,
                    category=self.category,
                    platform=self.platform,
                    title=f"Coupon {status}",
                    code=f"CODE-{status}",
                    expiry_date="2030-12-31",
                    proof_image="coupons/proofs/proof.png",
                    status=status,
                )
                self.assertEqual(coupon.status, status)

    def test_category_names_are_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Category.objects.create(name="Food")

    def test_platform_names_are_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Platform.objects.create(name="Zomato")

    def test_coupon_code_is_sanitized_and_hashed(self):
        coupon = Coupon.objects.create(
            owner=self.owner,
            category=self.category,
            platform=self.platform,
            title="Discount",
            code=" save-20 ",
            expiry_date="2030-12-31",
            proof_image="coupons/proofs/discount.png",
        )

        self.assertEqual(coupon.code, "SAVE20")
        self.assertEqual(
            coupon.code_hash,
            hashlib.sha256(b"SAVE20").hexdigest(),
        )

    def test_updating_other_fields_keeps_code_hash_synchronized(self):
        coupon = Coupon.objects.create(
            owner=self.owner,
            category=self.category,
            platform=self.platform,
            title="Discount",
            code="SAVE20",
            expiry_date="2030-12-31",
            proof_image="coupons/proofs/discount.png",
        )

        coupon.code = "NEW-30"
        coupon.title = "Updated discount"
        coupon.save(update_fields={"code", "title"})
        coupon.refresh_from_db()

        self.assertEqual(coupon.code, "NEW30")
        self.assertEqual(coupon.code_hash, hashlib.sha256(b"NEW30").hexdigest())

    def test_sanitized_coupon_codes_are_unique_per_owner(self):
        Coupon.objects.create(
            owner=self.owner,
            category=self.category,
            platform=self.platform,
            title="First",
            code="SAVE-20",
            expiry_date="2030-12-31",
            proof_image="coupons/proofs/first.png",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Coupon.objects.create(
                owner=self.owner,
                category=self.category,
                platform=self.platform,
                title="Duplicate",
                code="save 20",
                expiry_date="2030-12-31",
                proof_image="coupons/proofs/duplicate.png",
            )

    def test_same_code_can_be_used_by_different_owners(self):
        Coupon.objects.create(
            owner=self.owner,
            category=self.category,
            platform=self.platform,
            title="First owner's coupon",
            code="SAVE20",
            expiry_date="2030-12-31",
            proof_image="coupons/proofs/first.png",
        )
        other_owner = get_user_model().objects.create_user(
            username="other-owner",
            password="safe-password",
        )

        coupon = Coupon.objects.create(
            owner=other_owner,
            category=self.category,
            platform=self.platform,
            title="Other owner's coupon",
            code="SAVE20",
            expiry_date="2030-12-31",
            proof_image="coupons/proofs/other.png",
        )

        self.assertEqual(coupon.code, "SAVE20")


class CouponUploadTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings(
            BASE_DIR=Path(self.temp_dir.name),
            MEDIA_ROOT=Path(self.temp_dir.name) / "media",
        )
        self.owner = get_user_model().objects.create_user(
            username="uploader",
            password="safe-password",
        )
        self.category, _ = Category.objects.get_or_create(name="Food")
        self.platform, _ = Platform.objects.get_or_create(name="Zomato")
        self.client.force_login(self.owner)

    def _image_upload(self, content_type="image/png"):
        image_buffer = BytesIO()
        Image.new("RGB", (20, 20), "white").save(image_buffer, format="PNG")
        return SimpleUploadedFile(
            "proof.png",
            image_buffer.getvalue(),
            content_type=content_type,
        )

    def _start_upload(self, result=None):
        if result is None:
            result = ParsedCouponText(
                text="SAVE20 2030-12-31",
                codes=("SAVE20",),
                dates=("2030-12-31",),
            )
        with patch("coupons.views.extract_coupon_data", return_value=result):
            response = self.client.post(
                reverse("coupons:upload"),
                {
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                    "terms": "One use only",
                    "proof_image": self._image_upload(),
                },
            )
        return response

    def test_upload_shows_ocr_prefilled_confirmation_fields(self):
        response = self._start_upload()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="SAVE20"')
        self.assertContains(response, 'value="2030-12-31"')
        self.assertContains(response, "Confirm coupon details")

    def test_upload_stages_image_before_reading_it_for_ocr(self):
        uploaded_image = self._image_upload()
        original_bytes = uploaded_image.read()
        uploaded_image.seek(0)
        result = ParsedCouponText(
            text="SAVE20 2030-12-31",
            codes=("SAVE20",),
            dates=("2030-12-31",),
        )
        ocr_inputs = []

        def read_staged_image(source):
            ocr_inputs.append((source.name, source.read()))
            return result

        with patch(
            "coupons.views.extract_coupon_data",
            side_effect=read_staged_image,
        ):
            response = self.client.post(
                reverse("coupons:upload"),
                {
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                    "terms": "One use only",
                    "proof_image": uploaded_image,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(ocr_inputs), 1)
        self.assertNotEqual(ocr_inputs[0][0], uploaded_image.name)
        self.assertEqual(ocr_inputs[0][1], original_bytes)

    @override_settings(USE_S3_STORAGE=True)
    def test_pending_upload_storage_uses_s3_storage_alias_when_enabled(self):
        s3_storage = object()
        storage_handler = MagicMock()
        storage_handler.__getitem__.return_value = s3_storage

        with patch("coupons.views.storages", storage_handler):
            self.assertIs(_pending_storage(), s3_storage)

        storage_handler.__getitem__.assert_called_once_with("coupons_pending")

    def test_confirmation_saves_coupon_and_removes_pending_image(self):
        self._start_upload()
        token, pending = next(
            iter(self.client.session["pending_coupon_uploads"].items())
        )

        response = self.client.post(
            reverse("coupons:upload"),
            {
                "step": "confirm",
                "upload_token": token,
                "title": "Lunch voucher",
                "platform": self.platform.pk,
                "category": self.category.pk,
                "terms": "One use only",
                "code": "SAVE-20",
                "expiry_date": "2030-12-31",
            },
        )

        self.assertRedirects(response, reverse("coupons:upload"))
        coupon = Coupon.objects.get(owner=self.owner)
        self.assertEqual(coupon.code, "SAVE20")
        self.assertEqual(coupon.terms, "One use only")
        self.assertEqual(coupon.title, "Lunch voucher")
        self.assertEqual(coupon.platform, self.platform)
        self.assertEqual(coupon.category, self.category)
        self.assertTrue(coupon.proof_image.storage.exists(coupon.proof_image.name))
        self.assertNotIn(token, self.client.session.get("pending_coupon_uploads", {}))
        self.assertFalse(
            Path(self.temp_dir.name, ".private_uploads", pending["file_name"]).exists()
        )


class MarketplaceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="marketplace-owner",
            password="safe-password",
        )
        self.food, _ = Category.objects.get_or_create(name="Food")
        self.travel, _ = Category.objects.get_or_create(name="Travel")
        self.zomato, _ = Platform.objects.get_or_create(name="Zomato")
        self.amazon, _ = Platform.objects.get_or_create(name="Amazon")

    def create_coupon(
        self,
        *,
        title,
        code,
        expiry_date,
        category=None,
        platform=None,
        status=Coupon.Status.AVAILABLE,
    ):
        return Coupon.objects.create(
            owner=self.owner,
            category=category or self.food,
            platform=platform or self.zomato,
            title=title,
            code=code,
            terms="Marketplace search terms",
            expiry_date=expiry_date,
            proof_image=f"coupons/proofs/{code}.png",
            status=status,
        )

    def test_marketplace_navigation_exposes_authentication_routes(self):
        response = self.client.get(reverse("coupons:marketplace"))

        self.assertContains(response, reverse("accounts:login"))
        self.assertContains(response, reverse("accounts:signup"))
        self.assertNotContains(response, reverse("trades:dashboard"))

    def test_authenticated_marketplace_navigation_exposes_app_sections(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("coupons:marketplace"))

        self.assertContains(response, reverse("coupons:upload"))
        self.assertContains(response, reverse("accounts:profile"))
        self.assertContains(response, reverse("trades:dashboard"))
        self.assertContains(response, reverse("trades:incoming"))

    def test_marketplace_shows_only_available_coupons_without_codes(self):
        today = timezone.localdate()
        listed = self.create_coupon(
            title="Available voucher",
            code="SECRET123",
            expiry_date=today + timedelta(days=10),
        )
        for index, status in enumerate((
            Coupon.Status.IN_ESCROW,
            Coupon.Status.TRADED,
            Coupon.Status.EXPIRED,
        ), start=1):
            self.create_coupon(
                title=f"Hidden {status}",
                code=f"PRIVATE{index}",
                expiry_date=today + timedelta(days=10),
                status=status,
            )

        response = self.client.get(reverse("coupons:marketplace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, listed.title)
        for status in (
            Coupon.Status.IN_ESCROW,
            Coupon.Status.TRADED,
            Coupon.Status.EXPIRED,
        ):
            self.assertNotContains(response, f"Hidden {status}")
        self.assertNotContains(response, "SECRET123")
        self.assertNotIn("code", response.context["coupons"][0])
        self.assertNotIn("code_hash", response.context["coupons"][0])

    def test_marketplace_excludes_logged_in_users_own_listings(self):
        other_owner = get_user_model().objects.create_user(
            username="other-owner",
        )
        own_listing = self.create_coupon(
            title="My coupon",
            code="OWNSECRET123",
            expiry_date=timezone.localdate() + timedelta(days=10),
        )
        other_listing = Coupon.objects.create(
            owner=other_owner,
            category=self.food,
            platform=self.zomato,
            title="Other user's coupon",
            code="OTHERSECRET456",
            terms="Marketplace search terms",
            expiry_date=timezone.localdate() + timedelta(days=8),
            proof_image="coupons/proofs/othersecret.png",
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("coupons:marketplace"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, own_listing.title)
        self.assertEqual(
            [coupon["title"] for coupon in response.context["coupons"]],
            [other_listing.title],
        )
        self.assertNotContains(response, "OWNSECRET123")
        self.assertNotContains(response, "OTHERSECRET456")

    def test_marketplace_filters_by_keyword_category_and_platform(self):
        match = self.create_coupon(
            title="Family dinner",
            code="DINNER123",
            expiry_date=timezone.localdate() + timedelta(days=10),
            category=self.food,
            platform=self.zomato,
        )
        self.create_coupon(
            title="Weekend hotel",
            code="HOTEL123",
            expiry_date=timezone.localdate() + timedelta(days=10),
            category=self.travel,
            platform=self.amazon,
        )

        response = self.client.get(
            reverse("coupons:marketplace"),
            {
                "keyword": "dinner",
                "category": self.food.pk,
                "platform": self.zomato.pk,
            },
        )

        self.assertEqual(
            [coupon["title"] for coupon in response.context["coupons"]],
            [match.title],
        )

    def test_marketplace_search_includes_platform_and_terms(self):
        platform_match = self.create_coupon(
            title="Restaurant reward",
            code="PLATFORM123",
            expiry_date=timezone.localdate() + timedelta(days=10),
            platform=self.amazon,
        )
        terms_match = self.create_coupon(
            title="Another reward",
            code="TERMS123",
            expiry_date=timezone.localdate() + timedelta(days=10),
            platform=self.zomato,
        )

        platform_response = self.client.get(
            reverse("coupons:marketplace"),
            {"keyword": "Amazon"},
        )
        terms_response = self.client.get(
            reverse("coupons:marketplace"),
            {"keyword": "Marketplace search terms"},
        )

        self.assertEqual(
            [coupon["title"] for coupon in platform_response.context["coupons"]],
            [platform_match.title],
        )
        self.assertEqual(
            {coupon["title"] for coupon in terms_response.context["coupons"]},
            {platform_match.title, terms_match.title},
        )

    def test_marketplace_filters_by_expiry_timeline(self):
        today = timezone.localdate()
        expiring_soon = self.create_coupon(
            title="Soon",
            code="SOON123",
            expiry_date=today + timedelta(days=5),
        )
        self.create_coupon(
            title="Later",
            code="LATER123",
            expiry_date=today + timedelta(days=45),
        )
        already_expired = self.create_coupon(
            title="Expired",
            code="OLD123",
            expiry_date=today - timedelta(days=1),
        )

        soon_response = self.client.get(
            reverse("coupons:marketplace"),
            {"expiry_timeline": "next_7_days"},
        )
        expired_response = self.client.get(
            reverse("coupons:marketplace"),
            {"expiry_timeline": "expired"},
        )

        self.assertEqual(
            [coupon["title"] for coupon in soon_response.context["coupons"]],
            [expiring_soon.title],
        )
        self.assertEqual(
            [coupon["title"] for coupon in expired_response.context["coupons"]],
            [already_expired.title],
        )

    def test_invalid_filters_show_no_listings(self):
        self.create_coupon(
            title="Available voucher",
            code="AVAILABLE123",
            expiry_date=timezone.localdate() + timedelta(days=10),
        )

        response = self.client.get(
            reverse("coupons:marketplace"),
            {"expiry_timeline": "unrecognized"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["coupons"]), 0)
        self.assertContains(response, "Select a valid choice")


class CouponUploadValidationTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings(
            BASE_DIR=Path(self.temp_dir.name),
            MEDIA_ROOT=Path(self.temp_dir.name) / "media",
        )
        self.owner = get_user_model().objects.create_user(
            username="uploader",
            password="safe-password",
        )
        self.category, _ = Category.objects.get_or_create(name="Food")
        self.platform, _ = Platform.objects.get_or_create(name="Zomato")
        self.client.force_login(self.owner)

    def _image_upload(self, content_type="image/png"):
        image_buffer = BytesIO()
        Image.new("RGB", (20, 20), "white").save(image_buffer, format="PNG")
        return SimpleUploadedFile(
            "proof.png",
            image_buffer.getvalue(),
            content_type=content_type,
        )

    def _start_upload(self, result=None):
        if result is None:
            result = ParsedCouponText(
                text="SAVE20 2030-12-31",
                codes=("SAVE20",),
                dates=("2030-12-31",),
            )
        with patch("coupons.views.extract_coupon_data", return_value=result):
            return self.client.post(
                reverse("coupons:upload"),
                {
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                    "terms": "One use only",
                    "proof_image": self._image_upload(),
                },
            )

    def test_upload_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("coupons:upload"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('coupons:upload')}",
        )

    def test_upload_rejects_unsupported_image_format(self):
        image_buffer = BytesIO()
        Image.new("RGB", (20, 20), "white").save(image_buffer, format="GIF")
        uploaded_image = SimpleUploadedFile(
            "proof.gif",
            image_buffer.getvalue(),
            content_type="image/gif",
        )
        form = CouponUploadForm(
            data={
                "title": "Lunch voucher",
                "category": self.category.pk,
                "platform": self.platform.pk,
            },
            files={"proof_image": uploaded_image},
        )

        self.assertFalse(form.is_valid())
        self.assertIn(
            "Upload a valid JPEG, PNG, or WebP image.",
            form.errors["proof_image"],
        )

    def test_upload_rejects_images_over_size_limit(self):
        image = self._image_upload()
        image.size = MAX_COUPON_IMAGE_SIZE + 1
        form = CouponUploadForm(
            data={
                "title": "Lunch voucher",
                "category": self.category.pk,
                "platform": self.platform.pk,
            },
            files={"proof_image": image},
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Image must be no larger than 10 MB.", form.errors["proof_image"])

    def test_upload_rejects_images_over_pixel_limit(self):
        with patch("coupons.forms.MAX_COUPON_IMAGE_PIXELS", 100):
            form = CouponUploadForm(
                data={
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                },
                files={"proof_image": self._image_upload()},  # 20x20 = 400 px
            )
            self.assertFalse(form.is_valid())
        self.assertIn("Image resolution is too high", form.errors["proof_image"][0])

    def test_upload_accepts_images_at_pixel_limit(self):
        with patch("coupons.forms.MAX_COUPON_IMAGE_PIXELS", 400):
            form = CouponUploadForm(
                data={
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                },
                files={"proof_image": self._image_upload()},
            )
            self.assertTrue(form.is_valid(), form.errors)

    def _post_upload_with_ocr_error(self, error):
        with patch("coupons.views.extract_coupon_data", side_effect=error):
            return self.client.post(
                reverse("coupons:upload"),
                {
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                    "terms": "",
                    "proof_image": self._image_upload(),
                },
            )

    def test_ocr_timeout_allows_manual_confirmation_with_notice(self):
        response = self._post_upload_with_ocr_error(OCRTimeoutError())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reading the image took too long")
        self.assertContains(response, 'name="code"')
        self.assertIn("pending_coupon_uploads", self.client.session)

    def test_ocr_busy_allows_manual_confirmation_with_notice(self):
        response = self._post_upload_with_ocr_error(OCRBusyError())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The reader is busy")
        self.assertContains(response, 'name="expiry_date"')

    def _post_upload(self, **extra):
        return self.client.post(
            reverse("coupons:upload"),
            {"proof_image": self._image_upload(), **extra},
        )

    def test_upload_needs_only_the_image(self):
        with patch(
            "coupons.views.extract_coupon_data",
            return_value=ParsedCouponText(text="", codes=(), dates=()),
        ):
            response = self._post_upload()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm coupon details")

    def test_browser_ocr_text_autofills_every_confirmation_field(self):
        food, _ = Category.objects.get_or_create(name="Food & Dining")
        zomato, _ = Platform.objects.get_or_create(name="Zomato")
        text = "ZOMATO 20% OFF\nUse code: SAVE20X\nValid till 15 April 2027\nMinimum order Rs 199"

        with patch("coupons.views.extract_coupon_data") as server_ocr:
            response = self._post_upload(ocr_status="done", ocr_text=text)

        server_ocr.assert_not_called()
        initial = response.context["form"].initial
        self.assertEqual(initial["code"], "SAVE20X")
        self.assertEqual(str(initial["expiry_date"]), "2027-04-15")
        self.assertEqual(initial["platform"], zomato.pk)
        self.assertEqual(initial["category"], food.pk)
        self.assertEqual(initial["title"], "Zomato 20% off")
        self.assertIn("Minimum order", initial["terms"])

    def test_browser_ocr_with_no_text_shows_notice(self):
        with patch("coupons.views.extract_coupon_data") as server_ocr:
            response = self._post_upload(ocr_status="done", ocr_text="  ")

        server_ocr.assert_not_called()
        self.assertContains(response, "could not find readable text")

    def test_browser_ocr_timeout_skips_server_ocr(self):
        with patch("coupons.views.extract_coupon_data") as server_ocr:
            response = self._post_upload(ocr_status="timeout")

        server_ocr.assert_not_called()
        self.assertContains(response, "Reading the image took too long")

    def test_server_ocr_is_the_fallback_when_browser_ocr_failed_or_is_missing(self):
        result = ParsedCouponText(text="Use code SAVE20", codes=("SAVE20",), dates=())
        for extra in ({}, {"ocr_status": "failed"}, {"ocr_status": "bogus"}):
            with self.subTest(extra=extra), patch(
                "coupons.views.extract_coupon_data", return_value=result
            ) as server_ocr:
                response = self._post_upload(**extra)

            server_ocr.assert_called_once()
            self.assertEqual(response.context["form"].initial["code"], "SAVE20")

    def test_oversized_browser_text_is_truncated(self):
        from .forms import MAX_CLIENT_OCR_CHARS

        form = CouponUploadForm(
            data={"ocr_text": "A" * (MAX_CLIENT_OCR_CHARS + 500), "ocr_status": "done"},
            files={"proof_image": self._image_upload()},
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(form.cleaned_data["ocr_text"]), MAX_CLIENT_OCR_CHARS)

    def test_confirmation_requires_title_platform_and_category(self):
        self._start_upload()
        token = next(iter(self.client.session["pending_coupon_uploads"]))

        response = self.client.post(
            reverse("coupons:upload"),
            {
                "step": "confirm",
                "upload_token": token,
                "code": "SAVE20",
                "expiry_date": "2030-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Coupon.objects.count(), 0)
        for field in ("title", "platform", "category"):
            self.assertIn(field, response.context["form"].errors)

    def test_duplicate_code_is_reported_during_confirmation(self):
        Coupon.objects.create(
            owner=self.owner,
            category=self.category,
            platform=self.platform,
            title="Already uploaded",
            code="SAVE20",
            expiry_date="2030-12-31",
            proof_image="coupons/proofs/existing.png",
        )
        self._start_upload()
        token = next(iter(self.client.session["pending_coupon_uploads"]))

        response = self.client.post(
            reverse("coupons:upload"),
            {
                "step": "confirm",
                "upload_token": token,
                "title": "Lunch voucher",
                "platform": self.platform.pk,
                "category": self.category.pk,
                "terms": "One use only",
                "code": "SAVE-20",
                "expiry_date": "2030-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already uploaded a voucher with this code")
        self.assertEqual(Coupon.objects.filter(owner=self.owner).count(), 1)
        self.assertIn(token, self.client.session["pending_coupon_uploads"])

    def test_missing_tesseract_allows_manual_confirmation_with_notice(self):
        with patch(
            "coupons.views.extract_coupon_data",
            side_effect=pytesseract.TesseractNotFoundError(),
        ):
            response = self.client.post(
                reverse("coupons:upload"),
                {
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                    "terms": "",
                    "proof_image": self._image_upload(),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Automatic reading is unavailable")
        self.assertContains(response, 'name="code"')
        self.assertContains(response, 'name="expiry_date"')

    def test_tesseract_read_failure_allows_manual_confirmation_with_notice(self):
        with patch(
            "coupons.views.extract_coupon_data",
            side_effect=pytesseract.TesseractError(1, "image could not be read"),
        ):
            response = self.client.post(
                reverse("coupons:upload"),
                {
                    "title": "Lunch voucher",
                    "category": self.category.pk,
                    "platform": self.platform.pk,
                    "terms": "",
                    "proof_image": self._image_upload(),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "We could not read this image")
        self.assertContains(response, 'name="code"')
        self.assertContains(response, 'name="expiry_date"')

    def test_discarding_confirmation_removes_pending_image(self):
        self._start_upload()
        token, pending = next(
            iter(self.client.session["pending_coupon_uploads"].items())
        )

        response = self.client.post(
            reverse("coupons:upload"),
            {"step": "cancel", "upload_token": token},
        )

        self.assertRedirects(response, reverse("coupons:upload"))
        self.assertNotIn(token, self.client.session.get("pending_coupon_uploads", {}))
        self.assertFalse(
            Path(self.temp_dir.name, ".private_uploads", pending["file_name"]).exists()
        )
