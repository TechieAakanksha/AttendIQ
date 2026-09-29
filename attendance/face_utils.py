"""Local OpenCV face enrollment and recognition helpers.

Pipeline: Haar cascade face detection -> 200x200 normalised grayscale crop ->
LBPH (Local Binary Pattern Histogram) recognition. Everything runs locally;
no image ever leaves the server.
"""
from __future__ import annotations

import io
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

FACE_SIZE = (200, 200)
MATCH_THRESHOLD = 50.0    # confidence >= this  -> attendance is marked
REVIEW_THRESHOLD = 38.0   # confidence >= this  -> flagged for teacher review

_cascade = None
_cascade_lock = threading.Lock()
_model_cache: dict = {}
_model_lock = threading.Lock()
_MODEL_CACHE_SIZE = 8


def _cv2():
    import cv2
    return cv2


def _get_cascade():
    """Load the Haar cascade once instead of on every frame."""
    global _cascade
    if _cascade is None:
        with _cascade_lock:
            if _cascade is None:
                cv2 = _cv2()
                path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
                cascade = cv2.CascadeClassifier(str(path))
                if cascade.empty():
                    raise RuntimeError("OpenCV face cascade could not be loaded.")
                _cascade = cascade
    return _cascade


def decode_image(file_or_bytes):
    """Decode uploaded bytes / file into a BGR numpy array."""
    raw = file_or_bytes.read() if hasattr(file_or_bytes, "read") else file_or_bytes
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)).convert("RGB"))
    return np.array(image)[:, :, ::-1].copy()


def _gray(image):
    cv2 = _cv2()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    return cv2.equalizeHist(gray)


def detect_face(image):
    """Return the largest face as a normalised 200x200 grayscale crop, or None."""
    cv2 = _cv2()
    gray = _gray(image)
    cascade = _get_cascade()
    candidates = []
    for scale in (1.0, 1.5):
        source = (
            cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            if scale != 1.0 else gray
        )
        faces = cascade.detectMultiScale(
            source, scaleFactor=1.08, minNeighbors=4, minSize=(55, 55),
            flags=cv2.CASCADE_SCALE_IMAGE,
        )
        for x, y, w, h in faces:
            candidates.append((int(x / scale), int(y / scale), int(w / scale), int(h / scale)))
        if candidates:
            break  # a face was found at native scale; skip the slower upscaled pass
    if not candidates:
        return None
    x, y, w, h = max(candidates, key=lambda box: box[2] * box[3])
    pad_x, pad_y = int(w * 0.12), int(h * 0.16)
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2, y2 = min(gray.shape[1], x + w + pad_x), min(gray.shape[0], y + h + pad_y)
    crop = gray[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    return cv2.resize(crop, FACE_SIZE, interpolation=cv2.INTER_AREA)


def has_face(file_or_bytes) -> bool:
    """True when a usable face can be found - used to validate enrollment uploads."""
    try:
        return detect_face(decode_image(file_or_bytes)) is not None
    except Exception:
        return False


def _build_recognizer(face_samples):
    cv2 = _cv2()
    if not hasattr(cv2, "face"):
        raise RuntimeError("opencv-contrib-python-headless is required for LBPH recognition")
    faces, labels, label_to_student = [], [], {}
    for sample in face_samples:
        try:
            with sample.image.open("rb") as handle:
                crop = detect_face(decode_image(handle.read()))
            if crop is not None:
                faces.append(crop)
                labels.append(sample.student_id)
                label_to_student[sample.student_id] = sample.student
        except (OSError, ValueError, TypeError):
            continue
    if not faces:
        return None, {}
    recognizer = cv2.face.LBPHFaceRecognizer_create(radius=2, neighbors=16, grid_x=8, grid_y=8)
    recognizer.train(faces, np.asarray(labels, dtype=np.int32))
    return recognizer, label_to_student


def train_recognizer(face_samples):
    """Train (or fetch from cache) a recognizer for exactly this set of samples.

    Training on every video frame is very slow, so the trained model is cached
    keyed by the sample ids; enrolling a new sample changes the key automatically.
    """
    samples = list(face_samples)
    key = tuple(sorted(sample.pk for sample in samples))
    with _model_lock:
        if key in _model_cache:
            return _model_cache[key]
    built = _build_recognizer(samples)
    with _model_lock:
        if len(_model_cache) >= _MODEL_CACHE_SIZE:
            _model_cache.pop(next(iter(_model_cache)))
        _model_cache[key] = built
    return built


def clear_model_cache():
    with _model_lock:
        _model_cache.clear()


def match_frame(frame_bytes, face_samples):
    """Return (result, student, confidence, message) for one camera frame."""
    try:
        face = detect_face(decode_image(frame_bytes))
    except Exception as exc:  # corrupt / unsupported image data
        return "review", None, 0.0, f"Frame could not be decoded: {exc}"
    if face is None:
        return "no_face", None, 0.0, "No face detected. Move closer, face the camera and improve lighting."
    recognizer, mapping = train_recognizer(face_samples)
    if recognizer is None:
        return "review", None, 0.0, "No usable enrolled face samples. Re-enroll with at least 5 clear samples."
    label, distance = recognizer.predict(face)
    # LBPH distance is lower for better matches; convert to a 0-100 confidence.
    confidence = max(0.0, min(99.9, 100.0 - float(distance)))
    student = mapping.get(int(label))
    if student is None or confidence < REVIEW_THRESHOLD:
        return "unknown", None, confidence, "Face detected, but it did not match an enrolled student."
    if confidence < MATCH_THRESHOLD:
        return "review", student, confidence, "Possible match; capture a clearer frame for teacher review."
    return "matched", student, confidence, "Face matched. Attendance marked present for this session."
