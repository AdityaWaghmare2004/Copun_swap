from datetime import date

from django.test import TestCase

from ..models import Category, Platform
from .autofill import (
    build_autofill,
    build_title,
    detect_category,
    detect_platform,
    extract_terms,
    parse_date_text,
    pick_code,
    pick_expiry,
)


class AutofillRuleTests(TestCase):
    """Seeded lookups (Zomato, Swiggy, Food & Dining, ...) come from migration 0004."""

    def setUp(self):
        self.platforms = list(Platform.objects.all())
        self.categories = list(Category.objects.all())

    def platform(self, name):
        return Platform.objects.get(name=name)

    def test_code_next_to_a_label_beats_other_code_like_tokens(self):
        text = "Order ABC12345 placed\nUse code: SAVE20X at checkout"
        self.assertEqual(pick_code(text, ("ABC12345", "SAVE20X")), "SAVE20X")

    def test_code_on_the_line_after_a_label_is_preferred(self):
        text = "Order REF98765 placed\nYour coupon code\nFOOD30OFF"
        self.assertEqual(pick_code(text, ("REF98765", "FOOD30OFF")), "FOOD30OFF")

    def test_date_like_tokens_are_not_picked_as_codes(self):
        self.assertEqual(pick_code("15APR2027 SAVE20", ("15APR2027", "SAVE20")), "SAVE20")

    def test_no_codes_gives_empty_string(self):
        self.assertEqual(pick_code("nothing here", ()), "")

    def test_expiry_prefers_a_date_next_to_a_validity_label(self):
        text = "Issued 01/01/2027\nValid till 15 April 2027\nPrinted 2028-01-01"
        self.assertEqual(pick_expiry(text), date(2027, 4, 15))

    def test_expiry_understands_ordinal_days(self):
        self.assertEqual(pick_expiry("Expires on 15th April 2027"), date(2027, 4, 15))

    def test_expiry_without_a_label_uses_the_latest_date(self):
        self.assertEqual(pick_expiry("2027-01-01 and 2027-06-30"), date(2027, 6, 30))

    def test_expiry_none_when_no_valid_date(self):
        self.assertIsNone(pick_expiry("32/13/2027 no real date"))

    def test_two_digit_years_and_dotted_dates_parse(self):
        self.assertEqual(parse_date_text("15/04/27"), date(2027, 4, 15))
        self.assertEqual(parse_date_text("15.04.2027"), date(2027, 4, 15))
        self.assertIsNone(parse_date_text("not a date"))

    def test_platform_matched_by_name_alias_and_ocr_typo(self):
        self.assertEqual(
            detect_platform("Get 20% off on Zomato orders", self.platforms),
            self.platform("Zomato"),
        )
        self.assertEqual(
            detect_platform("visit amzn.in/deal", self.platforms),
            self.platform("Amazon"),
        )
        self.assertEqual(
            detect_platform("Swlggy One voucher", self.platforms),
            self.platform("Swiggy"),
        )

    def test_platform_needs_a_whole_word_for_short_names(self):
        self.assertIsNone(detect_platform("Rubber duck discount", self.platforms))

    def test_platform_none_when_nothing_matches_and_other_is_never_matched(self):
        self.assertIsNone(detect_platform("Other great deal", self.platforms))
        self.assertIsNone(detect_platform("", self.platforms))

    def test_category_follows_the_platform(self):
        self.assertEqual(
            detect_category("anything", self.platform("Uber"), self.categories).name,
            "Travel",
        )
        self.assertEqual(
            detect_category("anything", self.platform("Myntra"), self.categories).name,
            "Shopping",
        )

    def test_category_falls_back_to_keywords_without_a_platform(self):
        category = detect_category(
            "Flat discount on hotel and flight booking", None, self.categories
        )
        self.assertEqual(category.name, "Travel")
        self.assertIsNone(detect_category("zzz", None, self.categories))

    def test_title_uses_platform_and_discount(self):
        zomato = self.platform("Zomato")
        self.assertEqual(build_title("Get 20% off", zomato, "X"), "Zomato 20% off")
        self.assertEqual(build_title("Flat Rs 150 off", zomato, "X"), "Zomato ₹150 off")
        self.assertEqual(build_title("no discount shown", zomato, "X"), "Zomato coupon")

    def test_title_ignores_impossible_percentages_and_unknown_platforms(self):
        other = self.platform("Other")
        self.assertEqual(build_title("500% off", other, "SAVE20"), "Coupon SAVE20")
        self.assertEqual(build_title("10% discount", other, "SAVE20"), "10% off")
        self.assertEqual(build_title("", None, ""), "")

    def test_terms_keep_only_condition_like_lines(self):
        text = (
            "ZOMATO 20% OFF\n"
            "Valid on orders above Rs 199\n"
            "Use code SAVE20\n"
            "Not valid with other offers\n"
            "Valid on orders above Rs 199"
        )
        self.assertEqual(
            extract_terms(text),
            "Valid on orders above Rs 199\nNot valid with other offers",
        )

    def test_build_autofill_fills_everything_from_a_realistic_coupon(self):
        result = build_autofill(
            "ZOMATO\nFLAT 20% OFF\nUse code: SAVE20X\n"
            "Valid till 15 April 2027\nMinimum order Rs 199"
        )

        self.assertEqual(result.code, "SAVE20X")
        self.assertEqual(result.expiry_date, date(2027, 4, 15))
        self.assertEqual(result.platform.name, "Zomato")
        self.assertEqual(result.category.name, "Food & Dining")
        self.assertEqual(result.title, "Zomato 20% off")
        self.assertIn("Minimum order", result.terms)
        self.assertEqual(result.as_initial()["platform"], result.platform.pk)

    def test_build_autofill_on_empty_text_falls_back_to_other_lookups(self):
        result = build_autofill("")

        self.assertEqual(result.code, "")
        self.assertIsNone(result.expiry_date)
        self.assertEqual(result.platform.name, "Other")
        self.assertEqual(result.category.name, "Other")
        self.assertNotIn("code", result.as_initial())

    def test_huge_input_is_bounded(self):
        result = build_autofill("SAVE20X " * 100_000)
        self.assertEqual(result.code, "SAVE20X")
