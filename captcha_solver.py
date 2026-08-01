"""Solve numeric CAPTCHA images used by the IMS login page."""

import io
import re
import logging
from collections import Counter

import requests
import ddddocr
from PIL import Image, ImageOps, ImageFilter

logger = logging.getLogger(__name__)

# Initialize ddddocr instance once to save overhead
ocr = ddddocr.DdddOcr(show_ad=False)
EXPECTED_CAPTCHA_LEN = 5

def solve_captcha(image_url: str, session: requests.Session | None = None) -> str:
    """
    Download a CAPTCHA image from *image_url* and return the recognised digits.
    """
    try:
        getter = session or requests
        resp = getter.get(image_url, timeout=15)
        resp.raise_for_status()
        return solve_captcha_from_bytes(resp.content)
    except Exception:
        logger.exception("Failed to download CAPTCHA image from %s", image_url)
        return ""


def solve_captcha_from_bytes(raw_bytes: bytes) -> str:
    """Solve CAPTCHA when raw image bytes are available."""
    chosen, _predictions = solve_captcha_with_debug(raw_bytes)
    return chosen


def solve_captcha_with_debug(raw_bytes: bytes) -> tuple[str, list[str]]:
    """Solve CAPTCHA and return (chosen_text, all_digit_predictions)."""
    try:
        variants = _build_variants(raw_bytes)
    except Exception:
        logger.exception("Failed to build CAPTCHA image variants")
        return "", []

    predictions: list[str] = []
    for vb in variants:
        try:
            raw = ocr.classification(vb)
            digits = re.sub(r"\D", "", raw or "")
            if digits:
                predictions.append(digits)
        except Exception:
            continue

    if not predictions:
        logger.warning("No OCR prediction produced from CAPTCHA variants")
        return "", []

    # Prefer stable prediction with expected length first, then most common fallback.
    exact_len = [p for p in predictions if len(p) == EXPECTED_CAPTCHA_LEN]
    if exact_len:
        chosen = Counter(exact_len).most_common(1)[0][0]
    else:
        near_len = [p for p in predictions if len(p) >= 4]
        if near_len:
            chosen = Counter(near_len).most_common(1)[0][0]
        else:
            chosen = ""

    logger.info("CAPTCHA OCR candidates=%s chosen=%r", predictions[:8], chosen)
    return chosen, predictions


def _build_variants(raw_bytes: bytes) -> list[bytes]:
    """Generate multiple preprocessed image variants to improve OCR reliability."""
    base = Image.open(io.BytesIO(raw_bytes)).convert("L")
    base = ImageOps.autocontrast(base)

    variants: list[Image.Image] = [base]

    # Slight denoise + binarization at multiple thresholds.
    denoised = base.filter(ImageFilter.MedianFilter(size=3))
    variants.append(denoised)

    for threshold in (90, 110, 130, 150, 170):
        bw = denoised.point(lambda p, t=threshold: 255 if p > t else 0).convert("L")
        variants.append(bw)
        variants.append(ImageOps.invert(bw))

    out: list[bytes] = []
    seen: set[bytes] = set()
    for img in variants:
        # Upscale helps OCR on tiny CAPTCHA glyphs.
        scaled = img.resize((img.width * 2, img.height * 2), Image.Resampling.NEAREST)
        buf = io.BytesIO()
        scaled.save(buf, format="PNG")
        b = buf.getvalue()
        if b not in seen:
            out.append(b)
            seen.add(b)

    return out


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    # Test with a local image if available
    print("CAPTCHA solver module loaded OK.")
