"""Turn raw OCR text into suggested coupon details, using plain rules.

Everything here is a *suggestion* shown on the confirmation screen; the user
can correct any field before saving. The text may come from the browser, so it
is treated as untrusted input: it is only searched with bounded regexes and is
never executed, stored, or rendered unescaped.
"""

import difflib
import re
from dataclasses import dataclass
from datetime import date, datetime

from ..models import Category, Platform
from .ocr import DATE_PATTERN, parse_coupon_text

MAX_TEXT_CHARS = 20_000
MAX_TITLE_CHARS = 200
MAX_TERMS_CHARS = 500
OTHER = "other"

# Extra spellings seen on coupons. Platforms added later through the admin are
# still matched by their own name.
PLATFORM_ALIASES = {
    "amazon": ("amzn", "amazon pay"),
    "flipkart": ("fkrt",),
    "myntra": (),
    "swiggy": ("instamart", "swiggy one"),
    "zomato": ("zomato gold", "blinkit"),
    "uber": ("uber eats",),
}

# Platform name -> category name (the seeded defaults).
PLATFORM_CATEGORIES = {
    "amazon": "Shopping",
    "flipkart": "Shopping",
    "myntra": "Shopping",
    "swiggy": "Food & Dining",
    "zomato": "Food & Dining",
    "uber": "Travel",
}

# Used when the platform is unknown: category name -> keywords.
CATEGORY_KEYWORDS = {
    "Food & Dining": (
        "food", "pizza", "burger", "restaurant", "meal", "dine", "dining",
        "cafe", "delivery", "biryani", "grocery",
    ),
    "Travel": (
        "flight", "hotel", "cab", "ride", "trip", "bus", "train", "booking",
        "holiday", "airline", "stay",
    ),
    "Shopping": (
        "fashion", "shopping", "cart", "clothing", "shoes", "electronics",
        "wear", "beauty",
    ),
    "Entertainment": (
        "movie", "movies", "ticket", "tickets", "streaming", "subscription",
        "music", "concert", "show",
    ),
    "Gaming": ("game", "games", "gaming", "gift card", "console", "esports"),
}

_CODE_LABEL = re.compile(r"\b(?:code|coupon|promo|voucher|use)\b", re.IGNORECASE)
_DATE_LIKE_CODE = re.compile(
    r"^\d{1,2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2,4}$"
)
_EXPIRY_LABEL = re.compile(
    r"\b(?:valid|expir\w*|until|till|before|ends?|by)\b", re.IGNORECASE
)
_ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
_PERCENT = re.compile(r"\b(\d{1,3})\s?%\s*(?:off|discount|cashback)?", re.IGNORECASE)
_MONEY = re.compile(
    r"(?:flat\s+)?(?:₹|rs\.?|inr)\s?(\d[\d,]{0,8})\s*(?:off|discount|cashback)?",
    re.IGNORECASE,
)
_TERMS_LINE = re.compile(
    r"\b(?:valid|minimum|min\.?|applicable|only|per user|not valid|terms|"
    r"t&c|tnc|new users?|cannot|cap(?:ped)?|max(?:imum)?)\b",
    re.IGNORECASE,
)

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d.%m.%y",
)


@dataclass(frozen=True)
class CouponAutofill:
    code: str = ""
    expiry_date: date | None = None
    title: str = ""
    terms: str = ""
    platform: Platform | None = None
    category: Category | None = None

    def as_initial(self):
        initial = {}
        if self.code:
            initial["code"] = self.code
        if self.expiry_date:
            initial["expiry_date"] = self.expiry_date
        if self.title:
            initial["title"] = self.title
        if self.terms:
            initial["terms"] = self.terms
        if self.platform:
            initial["platform"] = self.platform.pk
        if self.category:
            initial["category"] = self.category.pk
        return initial


def parse_date_text(value):
    value = re.sub(r"\s+", " ", value.strip())
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def pick_code(text, codes):
    """Prefer a code that sits next to a 'code' / 'coupon' / 'use' label."""
    if not codes:
        return ""
    lines = text.splitlines()

    def score(code):
        total = 0
        if _DATE_LIKE_CODE.match(code):
            total -= 2
        pattern = re.compile(re.escape(code), re.IGNORECASE)
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            if _CODE_LABEL.search(line):
                total += 2
            elif index and _CODE_LABEL.search(lines[index - 1]):
                total += 1
        return total

    # max() keeps the earliest code on ties because it returns the first best.
    return max(codes, key=score)


def pick_expiry(text):
    """Return the most likely expiry date found in the text, or None."""
    searchable = _ORDINAL.sub("", text)
    labelled = []
    other = []
    for match in DATE_PATTERN.finditer(searchable):
        parsed = parse_date_text(match.group(0))
        if parsed is None:
            continue
        before = searchable[max(0, match.start() - 25) : match.start()]
        (labelled if _EXPIRY_LABEL.search(before) else other).append(parsed)
    if labelled:
        return labelled[0]
    if other:
        # Without a label, the latest date is the best guess for an expiry.
        return max(other)
    return None


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def detect_platform(text, platforms):
    normalized = _normalize(text)
    if not normalized:
        return None
    padded = f" {normalized} "
    words = {word for word in normalized.split() if len(word) >= 5}

    best, best_score = None, 0
    for platform in platforms:
        if platform.name.strip().lower() == OTHER:
            continue
        names = [_normalize(platform.name)]
        names += [_normalize(alias) for alias in PLATFORM_ALIASES.get(names[0], ())]
        score = 0
        for name in filter(None, names):
            if f" {name} " in padded:
                score += 3 * padded.count(f" {name} ")
            elif len(name) >= 6 and " " not in name:
                # Tolerate small OCR slips such as "Zomat0" or "Swlggy".
                if difflib.get_close_matches(name, words, n=1, cutoff=0.8):
                    score += 1
        if score > best_score:
            best, best_score = platform, score
    return best


def detect_category(text, platform, categories):
    by_name = {category.name.lower(): category for category in categories}
    if platform is not None:
        mapped = PLATFORM_CATEGORIES.get(platform.name.strip().lower())
        if mapped and mapped.lower() in by_name:
            return by_name[mapped.lower()]

    words = _normalize(text)
    padded = f" {words} "
    best_name, best_score = None, 0
    for name, keywords in CATEGORY_KEYWORDS.items():
        score = sum(padded.count(f" {keyword} ") for keyword in keywords)
        if score > best_score and name.lower() in by_name:
            best_name, best_score = name, score
    return by_name[best_name.lower()] if best_name else None


def build_title(text, platform, code):
    discount = ""
    percent = _PERCENT.search(text)
    if percent and 0 < int(percent.group(1)) <= 100:
        discount = f"{percent.group(1)}% off"
    else:
        money = _MONEY.search(text)
        if money:
            discount = f"₹{money.group(1).rstrip(',')} off"

    brand = ""
    if platform is not None and platform.name.strip().lower() != OTHER:
        brand = platform.name
    parts = [part for part in (brand, discount) if part]
    if not parts:
        return f"Coupon {code}"[:MAX_TITLE_CHARS] if code else ""
    if not discount:
        parts.append("coupon")
    return " ".join(parts)[:MAX_TITLE_CHARS]


def extract_terms(text):
    kept = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) >= 12 and _TERMS_LINE.search(line) and line not in kept:
            kept.append(line)
        if len(kept) == 6:
            break
    return "\n".join(kept)[:MAX_TERMS_CHARS]


def build_autofill(text):
    """Suggest every confirmation-form field from OCR text."""
    text = (text or "")[:MAX_TEXT_CHARS]
    platforms = list(Platform.objects.all())
    categories = list(Category.objects.all())

    parsed = parse_coupon_text(text)
    code = pick_code(text, parsed.codes)
    platform = detect_platform(text, platforms)
    category = detect_category(text, platform, categories)

    # Nothing recognised: fall back to the "Other" entries so every field has a value.
    if platform is None:
        platform = next(
            (item for item in platforms if item.name.strip().lower() == OTHER), None
        )
    if category is None:
        category = next(
            (item for item in categories if item.name.strip().lower() == OTHER), None
        )

    return CouponAutofill(
        code=code,
        expiry_date=pick_expiry(text),
        title=build_title(text, platform, code),
        terms=extract_terms(text),
        platform=platform,
        category=category,
    )
