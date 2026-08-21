"""
solve_captcha_cli.py – Standalone CLI entry point for CAPTCHA solving.

Reads raw image bytes from stdin, solves using ddddocr with multiple
preprocessing variants, and writes the best candidate text to stdout.

Usage:
    python solve_captcha_cli.py < image_bytes
    cat captcha.png | python solve_captcha_cli.py
"""

import io
import re
import sys
from collections import Counter

import ddddocr
from PIL import Image, ImageOps, ImageFilter

EXPECTED_CAPTCHA_LEN = 5

_ocr = ddddocr.DdddOcr(show_ad=False)


def _build_variants(raw_bytes: bytes) -> list[bytes]:
    """Generate multiple preprocessed image variants to improve OCR reliability."""
    base = Image.open(io.BytesIO(raw_bytes)).convert("L")
    base = ImageOps.autocontrast(base)

    variants: list[Image.Image] = [base]

    denoised = base.filter(ImageFilter.MedianFilter(size=3))
    variants.append(denoised)

    for threshold in (90, 110, 130, 150, 170):
        bw = denoised.point(lambda p, t=threshold: 255 if p > t else 0).convert("L")
        variants.append(bw)
        variants.append(ImageOps.invert(bw))

    out: list[bytes] = []
    seen: set[bytes] = set()
    for img in variants:
        scaled = img.resize((img.width * 2, img.height * 2), Image.Resampling.NEAREST)
        buf = io.BytesIO()
        scaled.save(buf, format="PNG")
        b = buf.getvalue()
        if b not in seen:
            out.append(b)
            seen.add(b)

    return out


def solve(raw_bytes: bytes) -> str:
    """Solve a CAPTCHA from raw image bytes and return the recognised digits."""
    try:
        variants = _build_variants(raw_bytes)
    except Exception:
        return ""

    predictions: list[str] = []
    for vb in variants:
        try:
            raw = _ocr.classification(vb)
            digits = re.sub(r"\D", "", raw or "")
            if digits:
                predictions.append(digits)
        except Exception:
            continue

    if not predictions:
        return ""

    exact_len = [p for p in predictions if len(p) == EXPECTED_CAPTCHA_LEN]
    if exact_len:
        return Counter(exact_len).most_common(1)[0][0]

    near_len = [p for p in predictions if len(p) >= 4]
    if near_len:
        return Counter(near_len).most_common(1)[0][0]

    return ""


if __name__ == "__main__":
    raw = sys.stdin.buffer.read()
    if not raw:
        print("", end="")
        sys.exit(1)
    result = solve(raw)
    print(result, end="")
