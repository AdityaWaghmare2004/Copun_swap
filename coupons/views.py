from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytesseract
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import FileSystemStorage, default_storage, storages
from django.db import IntegrityError, models, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from PIL import Image

from .forms import CouponConfirmationForm, CouponUploadForm, MarketplaceFilterForm
from .models import Coupon, hash_coupon_code
from .services.ocr import extract_coupon_data


def _pending_storage():
    if settings.USE_S3_STORAGE:
        return storages["coupons_pending"]
    return FileSystemStorage(location=settings.BASE_DIR / ".private_uploads")


def marketplace(request):
    form = MarketplaceFilterForm(request.GET or None)
    coupons = Coupon.objects.filter(status=Coupon.Status.AVAILABLE)
    if request.user.is_authenticated:
        coupons = coupons.exclude(owner=request.user)

    if form.is_bound and not form.is_valid():
        coupons = coupons.none()
    elif form.is_bound:
        keyword = form.cleaned_data["keyword"]
        if keyword:
            coupons = coupons.filter(
                models.Q(title__icontains=keyword)
                | models.Q(terms__icontains=keyword)
                | models.Q(category__name__icontains=keyword)
                | models.Q(platform__name__icontains=keyword)
            )

        category = form.cleaned_data["category"]
        if category:
            coupons = coupons.filter(category=category)

        platform = form.cleaned_data["platform"]
        if platform:
            coupons = coupons.filter(platform=platform)

        expiry_timeline = form.cleaned_data["expiry_timeline"]
        today = timezone.localdate()
        if expiry_timeline == MarketplaceFilterForm.ExpiryTimeline.EXPIRED:
            coupons = coupons.filter(expiry_date__lt=today)
        elif expiry_timeline == MarketplaceFilterForm.ExpiryTimeline.UPCOMING:
            coupons = coupons.filter(expiry_date__gte=today)
        elif expiry_timeline in {
            MarketplaceFilterForm.ExpiryTimeline.NEXT_7_DAYS,
            MarketplaceFilterForm.ExpiryTimeline.NEXT_30_DAYS,
            MarketplaceFilterForm.ExpiryTimeline.NEXT_90_DAYS,
        }:
            days = int(expiry_timeline.removeprefix("next_").removesuffix("_days"))
            coupons = coupons.filter(
                expiry_date__range=(today, today + timedelta(days=days))
            )

    listings = coupons.order_by("expiry_date", "title").values(
        "id",
        "title",
        "terms",
        "expiry_date",
        "category__name",
        "platform__name",
        "owner_id",
    )
    return render(
        request,
        "coupons/marketplace.html",
        {"form": form, "coupons": listings},
    )


def _parse_ocr_date(value):
    parsed = parse_date(value)
    if parsed is not None:
        return parsed
    for date_format in (
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def _render_confirmation(request, pending_upload, form, ocr_notice=None, ocr_text=""):
    return render(
        request,
        "coupons/confirm_upload.html",
        {
            "form": form,
            "title": pending_upload["title"],
            "ocr_notice": ocr_notice,
            "ocr_text": ocr_text,
        },
    )


@login_required
def upload_coupon(request):
    if request.method == "POST" and request.POST.get("step") == "confirm":
        return _confirm_coupon(request)
    if request.method == "POST" and request.POST.get("step") == "cancel":
        return _cancel_upload(request)

    form = CouponUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        image = form.cleaned_data["proof_image"]
        image.seek(0)
        token = uuid4().hex
        pending_storage = _pending_storage()
        pending_file_name = pending_storage.save(
            f"{token}{image.image_extension}",
            image,
        )
        pending_upload = {
            "file_name": pending_file_name,
            "owner_id": request.user.pk,
            "title": form.cleaned_data["title"],
            "category_id": form.cleaned_data["category"].pk,
            "platform_id": form.cleaned_data["platform"].pk,
            "terms": form.cleaned_data["terms"],
        }

        ocr_notice = None
        ocr_text = ""
        try:
            with pending_storage.open(pending_file_name, "rb") as stored_image:
                result = extract_coupon_data(stored_image)
            ocr_text = result.text
        except pytesseract.TesseractNotFoundError:
            result = None
            ocr_notice = (
                "OCR is unavailable because Tesseract is not installed. "
                "You can enter the code and expiry date manually."
            )
        except pytesseract.TesseractError:
            result = None
            ocr_notice = (
                "OCR could not read this image. You can enter the code and "
                "expiry date manually."
            )

        uploads = request.session.get("pending_coupon_uploads", {})
        if not isinstance(uploads, dict):
            uploads = {}
        uploads[token] = pending_upload
        request.session["pending_coupon_uploads"] = uploads

        initial = {"upload_token": token}
        if result is not None:
            if result.codes:
                initial["code"] = result.codes[0]
            if result.dates:
                parsed_date = _parse_ocr_date(result.dates[0])
                if parsed_date is not None:
                    initial["expiry_date"] = parsed_date
        confirmation_form = CouponConfirmationForm(initial=initial)
        return _render_confirmation(
            request,
            pending_upload,
            confirmation_form,
            ocr_notice,
            ocr_text,
        )

    return render(request, "coupons/upload.html", {"form": form})


def _confirm_coupon(request):
    form = CouponConfirmationForm(request.POST)
    token = form.data.get("upload_token", "")
    uploads = request.session.get("pending_coupon_uploads", {})
    pending_upload = uploads.get(token) if isinstance(uploads, dict) else None
    if (
        not pending_upload
        or not isinstance(pending_upload, dict)
        or pending_upload.get("owner_id") != request.user.pk
        or not _pending_storage().exists(pending_upload.get("file_name", ""))
    ):
        raise Http404("The pending coupon upload was not found.")

    if not form.is_valid():
        return _render_confirmation(request, pending_upload, form)

    code = form.cleaned_data["code"]
    code_hash = hash_coupon_code(code)
    if Coupon.objects.filter(owner=request.user, code_hash=code_hash).exists():
        form.add_error("code", "You have already uploaded a voucher with this code.")
        return _render_confirmation(request, pending_upload, form)

    pending_storage = _pending_storage()
    coupon = Coupon(
        owner=request.user,
        title=pending_upload["title"],
        category_id=pending_upload["category_id"],
        platform_id=pending_upload["platform_id"],
        terms=pending_upload["terms"],
        code=code,
        expiry_date=form.cleaned_data["expiry_date"],
    )
    final_image_name = f"{uuid4().hex}{Path(pending_upload['file_name']).suffix}"
    with pending_storage.open(pending_upload["file_name"], "rb") as image_file:
        coupon.proof_image.save(final_image_name, image_file, save=False)

    try:
        with transaction.atomic():
            coupon.save()
    except IntegrityError:
        default_storage.delete(coupon.proof_image.name)
        if Coupon.objects.filter(owner=request.user, code_hash=code_hash).exists():
            form.add_error("code", "You have already uploaded a voucher with this code.")
            return _render_confirmation(request, pending_upload, form)
        raise

    pending_storage.delete(pending_upload["file_name"])
    uploads.pop(token)
    request.session["pending_coupon_uploads"] = uploads
    messages.success(request, "Coupon uploaded successfully.")
    return redirect("coupons:upload")


def _cancel_upload(request):
    token = request.POST.get("upload_token", "")
    uploads = request.session.get("pending_coupon_uploads", {})
    pending_upload = uploads.get(token) if isinstance(uploads, dict) else None
    if (
        not pending_upload
        or not isinstance(pending_upload, dict)
        or pending_upload.get("owner_id") != request.user.pk
    ):
        raise Http404("The pending coupon upload was not found.")

    _pending_storage().delete(pending_upload["file_name"])
    uploads.pop(token)
    request.session["pending_coupon_uploads"] = uploads
    messages.info(request, "Pending coupon upload discarded.")
    return redirect("coupons:upload")
