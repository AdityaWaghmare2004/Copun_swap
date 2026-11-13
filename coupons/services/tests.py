from unittest.mock import patch

from django.test import SimpleTestCase
from PIL import Image, ImageDraw

from .ocr import extract_coupon_data, parse_coupon_text, preprocess_image


class CouponOCRTests(SimpleTestCase):
    def test_preprocessing_converts_to_thresholded_grayscale(self):
        image = Image.new("RGB", (20, 10), "white")
        ImageDraw.Draw(image).rectangle((0, 0, 9, 9), fill="black")

        processed = preprocess_image(image)

        self.assertEqual(processed.mode, "1")
        self.assertEqual(processed.getpixel((2, 2)), 0)
        self.assertEqual(processed.getpixel((17, 2)), 255)

    def test_parsing_detects_codes_and_common_date_formats(self):
        result = parse_coupon_text(
            "Use SAVE20 or FOOD30 by 2027-04-15. "
            "Valid until 15/04/2027 or April 20, 2027."
        )

        self.assertEqual(result.codes, ("SAVE20", "FOOD30"))
        self.assertEqual(
            result.dates,
            ("2027-04-15", "15/04/2027", "April 20, 2027"),
        )

    @patch(
        "coupons.services.ocr.pytesseract.image_to_string",
        return_value="CODE99 2028-01-02",
    )
    def test_image_extraction_preprocesses_and_parses_image(self, image_to_string):
        image = Image.new("RGB", (30, 15), "white")

        result = extract_coupon_data(image)

        image_to_string.assert_called_once()
        self.assertEqual(image_to_string.call_args.args[0].mode, "1")
        self.assertEqual(result.codes, ("CODE99",))
        self.assertEqual(result.dates, ("2028-01-02",))
