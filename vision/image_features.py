"""Small image feature helpers used by live HUD recognition."""

from __future__ import annotations

import re

import cv2
import numpy as np
from scipy.fftpack import dct


MAX_VARIANTS_PER_HERO = 16  # Includes the required base portrait.
ALLOWED_HERO_VARIANT_NAMES = frozenset(
    {"arcana", "death", "dim", "inset08", "inset16", "persona"}
)
_DYNAMIC_HERO_VARIANT_NAME = re.compile(
    r"(?:arcana|persona|style)[0-9]{2}|calibration_[0-9a-f]{12}"
)


def valid_hero_variant_name(name: str) -> bool:
    return name in ALLOWED_HERO_VARIANT_NAMES or _DYNAMIC_HERO_VARIANT_NAME.fullmatch(
        name
    ) is not None


def compute_phash(image: np.ndarray, hash_size: int = 8) -> np.ndarray:
    """Return a perceptual hash as a flat uint8 bit array."""
    if hash_size < 2:
        raise ValueError("hash_size must be at least 2")
    size = hash_size * 4
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    pixels = cv2.resize(gray, (size, size), interpolation=cv2.INTER_LANCZOS4)
    coefficients = dct(dct(pixels, axis=0), axis=1)
    low_frequency = coefficients[:hash_size, :hash_size]
    return (low_frequency > np.median(low_frequency)).ravel().astype(np.uint8)


def color_histogram(image: np.ndarray) -> np.ndarray:
    """Return the normalized HSV histogram used by hero recognition."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    return cv2.normalize(histogram, histogram).flatten()
