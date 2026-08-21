"""
solve_captcha_cli.py - CAPTCHA solver using ddddocr.
Reads raw image bytes from stdin, solves using ddddocr default model.
Writes best candidate to stdout.

Usage:
    python solve_captcha_cli.py < image_bytes
    cat captcha.jpg | python solve_captcha_cli.py
"""
import io
import re
import sys
from collections import Counter

import ddddocr
from PIL import Image, ImageOps

EXPECTED_CAPTCHA_LEN = 5
DEBUG = False

def _dbg(msg):
    if DEBUG:
        print(msg, file=sys.stderr, flush=True)

try:
    _ocr = ddddocr.DdddOcr(show_ad=False)
    _dbg("[CAPTCHA-PY-DEBUG] ddddocr loaded")
except Exception as e:
    print(f"[CAPTCHA-PY-DEBUG] ddddocr init failed: {e}", file=sys.stderr, flush=True)
    _ocr = None


def _build_variants(raw_bytes):
    try:
        base = Image.open(io.BytesIO(raw_bytes))
        _dbg(f"[CAPTCHA-PY-DEBUG] image format={base.format} size={base.size} mode={base.mode}")
        base = base.convert("L")
        base = ImageOps.autocontrast(base)
    except Exception as e:
        print(f"[CAPTCHA-PY-DEBUG] image load failed: {e}", file=sys.stderr, flush=True)
        return []

    out = []
    seen = set()
    for img in [base]:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b = buf.getvalue()
        if b not in seen and len(b) > 10:
            out.append(b)
            seen.add(b)

    _dbg(f"[CAPTCHA-PY-DEBUG] built {len(out)} variants")
    return out


def solve(raw_bytes):
    if not raw_bytes:
        return ""

    variants = _build_variants(raw_bytes)
    if not variants:
        return ""

    raw = _ocr.classification(variants[0])
    digits = re.sub(r"\D", "", raw or "")
    _dbg(f"[CAPTCHA-PY-DEBUG] raw={raw!r} digits={digits!r}")
    if len(digits) == EXPECTED_CAPTCHA_LEN:
        return digits

    near_len = [p for p in [digits] if len(p) >= 4 and p]
    if near_len:
        return digits

    return digits


if __name__ == "__main__":
    raw = sys.stdin.buffer.read()
    if not raw:
        print("", end="")
        sys.exit(1)
    result = solve(raw)
    print(result, end="")
