"""QR-code helpers: generation, decoding and signed payloads.

Two QR flows are supported:

1. Student ID QR  - every student owns a permanent, *signed* QR. The teacher scans it
   with a camera (Live QR scan) and attendance is marked. The signature stops anyone
   from forging a QR for another student.
2. Session QR     - the teacher projects a QR that changes every 30 seconds. Students
   scan it with their own phone while logged in and check themselves in. The token is
   time-limited, so a photo of the screen is useless a couple of minutes later.

QR images are produced and decoded with OpenCV, which the project already uses for face
recognition, so there is no extra dependency.
"""
from __future__ import annotations

from urllib.parse import quote

import numpy as np
from django.core import signing

from .face_utils import decode_image

STUDENT_SALT = "attendiq.student-qr.v1"
SESSION_SALT = "attendiq.session-qr.v1"
SESSION_TOKEN_SECONDS = 120   # how long a session QR stays valid
SESSION_QR_REFRESH_SECONDS = 30


def _cv2():
    import cv2
    return cv2


# --------------------------------------------------------------------------- #
# Image generation / decoding
# --------------------------------------------------------------------------- #
def make_qr_png(text: str, box: int = 10, border: int = 4) -> bytes:
    """Render `text` as a crisp black-on-white PNG QR code."""
    cv2 = _cv2()
    modules = cv2.QRCodeEncoder.create().encode(text)
    if modules is None or modules.size == 0:
        raise ValueError("Could not encode QR code")
    image = cv2.resize(modules, None, fx=box, fy=box, interpolation=cv2.INTER_NEAREST)
    pad = box * border  # quiet zone required by the QR standard
    image = cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("Could not encode PNG")
    return buffer.tobytes()


def read_qr(file_or_bytes) -> str | None:
    """Find and decode a QR code in a camera frame. Returns the text or None."""
    cv2 = _cv2()
    image = decode_image(file_or_bytes)
    detector = cv2.QRCodeDetector()
    text, _, _ = detector.detectAndDecode(image)
    if text:
        return text
    # Fallback for small / low-contrast codes: grayscale, upscaled, contrast-normalised.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    for scale in (2.0, 0.6):
        candidate = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        candidate = cv2.normalize(candidate, None, 0, 255, cv2.NORM_MINMAX)
        text, _, _ = detector.detectAndDecode(candidate)
        if text:
            return text
    return None


# --------------------------------------------------------------------------- #
# Student ID QR (permanent, signed)
# --------------------------------------------------------------------------- #
def student_qr_payload(student) -> str:
    return signing.Signer(salt=STUDENT_SALT).sign(f"S{student.pk}")


def student_id_from_payload(payload: str) -> int | None:
    """Verify a scanned student QR and return the student's primary key (or None)."""
    try:
        value = signing.Signer(salt=STUDENT_SALT).unsign((payload or "").strip())
    except signing.BadSignature:
        return None
    if not value.startswith("S") or not value[1:].isdigit():
        return None
    return int(value[1:])


# --------------------------------------------------------------------------- #
# Session QR (short-lived, signed + timestamped)
# --------------------------------------------------------------------------- #
def make_session_token(session) -> str:
    return signing.TimestampSigner(salt=SESSION_SALT).sign(str(session.pk))


def session_id_from_token(token: str) -> int | None:
    """Return the session id from a valid, unexpired token.

    Raises signing.SignatureExpired for an old token and signing.BadSignature for a forged one.
    """
    value = signing.TimestampSigner(salt=SESSION_SALT).unsign(token or "", max_age=SESSION_TOKEN_SECONDS)
    return int(value) if value.isdigit() else None


def session_checkin_url(request, session) -> str:
    from django.urls import reverse
    base = request.build_absolute_uri(reverse("qr_checkin"))
    return f"{base}?t={quote(make_session_token(session), safe='')}"
