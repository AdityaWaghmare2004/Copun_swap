import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pytesseract
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Longest image side handed to Tesseract. Larger images are downscaled first.
OCR_MAX_DIMENSION = 2000
# Hard cap on a single Tesseract run. Must stay well below Gunicorn's --timeout.
OCR_TIMEOUT_SECONDS = 25
# How long a request will wait for another OCR job to finish before giving up.
OCR_QUEUE_WAIT_SECONDS = 5
# One OCR job at a time per process keeps memory and CPU bounded.
_ocr_slot = threading.BoundedSemaphore(1)


class OCRError(Exception):
    """Base class for OCR failures the user can recover from manually."""


class OCRTimeoutError(OCRError):
    """Tesseract ran longer than OCR_TIMEOUT_SECONDS and was killed."""


class OCRBusyError(OCRError):
    """Another OCR job is already running in this process."""


_COUPON_CODE_PATTERN = re.compile(
    r"\b(?=[A-Z0-9]{5,20}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]+\b",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"\b(?:"
    r"(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.](?:\d{2}|\d{4})"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}"
    r")\b",
    re.IGNORECASE,
)

# Public so other services can locate dates (with positions) in OCR text.
DATE_PATTERN = _DATE_PATTERN


@dataclass(frozen=True)
class ParsedCouponText:
    text: str
    codes: tuple[str, ...]
    dates: tuple[str, ...]


def preprocess_image(image: Image.Image) -> Image.Image:
    # Grayscale first (smaller), then shrink; thumbnail() works on our own copy
    # and never enlarges, so small images are left alone.
    grayscale = ImageOps.grayscale(image)
    grayscale.thumbnail((OCR_MAX_DIMENSION, OCR_MAX_DIMENSION))
    grayscale = ImageOps.autocontrast(grayscale)
    threshold = _otsu_threshold(grayscale)
    return grayscale.point(lambda pixel: 255 if pixel > threshold else 0, mode="1")


def _otsu_threshold(image: Image.Image) -> int:
    histogram = image.histogram()
    total = sum(histogram)
    weighted_total = sum(level * count for level, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    best_threshold = 127

    for threshold, count in enumerate(histogram):
        background_weight += count
        if background_weight == 0:
            continue

        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break

        background_sum += threshold * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = (
            background_weight
            * foreground_weight
            * (background_mean - foreground_mean) ** 2
        )
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold

    return best_threshold


def parse_coupon_text(text: str) -> ParsedCouponText:
    codes = tuple(
        dict.fromkeys(match.upper() for match in _COUPON_CODE_PATTERN.findall(text))
    )
    dates = tuple(dict.fromkeys(_DATE_PATTERN.findall(text)))
    return ParsedCouponText(text=text, codes=codes, dates=dates)


def extract_coupon_data(
    source: Image.Image | str | Path | BinaryIO,
) -> ParsedCouponText:
    if not _ocr_slot.acquire(timeout=OCR_QUEUE_WAIT_SECONDS):
        raise OCRBusyError("Another OCR job is already running.")
    try:
        if isinstance(source, Image.Image):
            return _extract_coupon_data_from_image(source)

        with Image.open(source) as image:
            # For JPEGs this makes the decoder produce a reduced-size grayscale
            # image directly, so a huge photo is never fully decoded in memory.
            # It is a no-op for PNG/WebP.
            image.draft("L", (OCR_MAX_DIMENSION, OCR_MAX_DIMENSION))
            return _extract_coupon_data_from_image(image)
    finally:
        _ocr_slot.release()


def _extract_coupon_data_from_image(image: Image.Image) -> ParsedCouponText:
    started = time.monotonic()
    original_size = image.size
    cleaned_image = preprocess_image(image)
    try:
        text = pytesseract.image_to_string(
            cleaned_image, timeout=OCR_TIMEOUT_SECONDS
        )
    except pytesseract.TesseractError:
        raise
    except RuntimeError as error:
        # pytesseract kills the Tesseract process and raises a plain
        # RuntimeError when `timeout` expires.
        if "timeout" in str(error).lower():
            logger.warning(
                "OCR timed out after %ss (source %s, sent %s)",
                OCR_TIMEOUT_SECONDS,
                original_size,
                cleaned_image.size,
            )
            raise OCRTimeoutError("Tesseract timed out.") from error
        raise
    logger.info(
        "OCR finished in %.1fs (source %s, sent %s)",
        time.monotonic() - started,
        original_size,
        cleaned_image.size,
    )
    return parse_coupon_text(text)
