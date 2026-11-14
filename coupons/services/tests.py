import threading
from io import BytesIO
from unittest.mock import patch

import pytesseract
from django.test import SimpleTestCase
from PIL import Image, ImageDraw

from . import ocr
from .ocr import (
    OCRBusyError,
    OCRTimeoutError,
    extract_coupon_data,
    parse_coupon_text,
    preprocess_image,
)


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

    def test_preprocessing_downscales_large_images_without_upscaling_small_ones(self):
        large = Image.new("RGB", (ocr.OCR_MAX_DIMENSION * 2, 400), "white")
        small = Image.new("RGB", (300, 200), "white")

        self.assertEqual(
            preprocess_image(large).size, (ocr.OCR_MAX_DIMENSION, 200)
        )
        self.assertEqual(preprocess_image(small).size, (300, 200))
        self.assertEqual(large.size, (ocr.OCR_MAX_DIMENSION * 2, 400))

    @patch("coupons.services.ocr.pytesseract.image_to_string", return_value="")
    def test_jpeg_file_is_decoded_at_reduced_size(self, image_to_string):
        buffer = BytesIO()
        Image.new("RGB", (ocr.OCR_MAX_DIMENSION * 4, 800), "white").save(
            buffer, format="JPEG"
        )
        buffer.seek(0)

        extract_coupon_data(buffer)

        sent = image_to_string.call_args.args[0]
        self.assertLessEqual(max(sent.size), ocr.OCR_MAX_DIMENSION)

    @patch("coupons.services.ocr.pytesseract.image_to_string", return_value="")
    def test_tesseract_is_called_with_a_timeout(self, image_to_string):
        extract_coupon_data(Image.new("RGB", (30, 15), "white"))

        self.assertEqual(
            image_to_string.call_args.kwargs["timeout"], ocr.OCR_TIMEOUT_SECONDS
        )

    def test_timeout_is_translated_to_ocr_timeout_error(self):
        with patch(
            "coupons.services.ocr.pytesseract.image_to_string",
            side_effect=RuntimeError("Tesseract process timeout"),
        ):
            with self.assertRaises(OCRTimeoutError):
                extract_coupon_data(Image.new("RGB", (30, 15), "white"))

    def test_other_runtime_errors_are_not_reported_as_timeouts(self):
        with patch(
            "coupons.services.ocr.pytesseract.image_to_string",
            side_effect=RuntimeError("something else"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                extract_coupon_data(Image.new("RGB", (30, 15), "white"))
        self.assertNotIsInstance(caught.exception, OCRTimeoutError)

    def test_tesseract_errors_pass_through_unchanged(self):
        with patch(
            "coupons.services.ocr.pytesseract.image_to_string",
            side_effect=pytesseract.TesseractError(1, "bad image"),
        ):
            with self.assertRaises(pytesseract.TesseractError):
                extract_coupon_data(Image.new("RGB", (30, 15), "white"))

    def test_second_job_gets_busy_error_and_slot_is_released_afterwards(self):
        started = threading.Event()
        release = threading.Event()

        def slow_ocr(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            return ""

        image = Image.new("RGB", (30, 15), "white")
        with patch("coupons.services.ocr.OCR_QUEUE_WAIT_SECONDS", 0.05), patch(
            "coupons.services.ocr.pytesseract.image_to_string",
            side_effect=slow_ocr,
        ):
            worker = threading.Thread(target=extract_coupon_data, args=(image,))
            worker.start()
            self.assertTrue(started.wait(timeout=5))
            with self.assertRaises(OCRBusyError):
                extract_coupon_data(image)
            release.set()
            worker.join(timeout=5)

        with patch(
            "coupons.services.ocr.pytesseract.image_to_string", return_value=""
        ):
            extract_coupon_data(image)  # slot is free again

    def test_slot_is_released_after_a_failure(self):
        image = Image.new("RGB", (30, 15), "white")
        with patch(
            "coupons.services.ocr.pytesseract.image_to_string",
            side_effect=RuntimeError("Tesseract process timeout"),
        ):
            with self.assertRaises(OCRTimeoutError):
                extract_coupon_data(image)
        with patch(
            "coupons.services.ocr.pytesseract.image_to_string", return_value=""
        ):
            extract_coupon_data(image)
