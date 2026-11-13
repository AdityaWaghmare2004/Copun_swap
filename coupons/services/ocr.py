import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pytesseract
from PIL import Image, ImageOps


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


@dataclass(frozen=True)
class ParsedCouponText:
    text: str
    codes: tuple[str, ...]
    dates: tuple[str, ...]


def preprocess_image(image: Image.Image) -> Image.Image:
    grayscale = ImageOps.grayscale(image)
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
    if isinstance(source, Image.Image):
        image = source
        return _extract_coupon_data_from_image(image)

    with Image.open(source) as image:
        return _extract_coupon_data_from_image(image)


def _extract_coupon_data_from_image(image: Image.Image) -> ParsedCouponText:
    cleaned_image = preprocess_image(image)
    text = pytesseract.image_to_string(cleaned_image)
    return parse_coupon_text(text)
