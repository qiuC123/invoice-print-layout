"""Synthetic QR photographs only; private invoice images never become fixtures."""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from invoice_print_layout import mail_search

cv = pytest.importorskip('cv2')
np = pytest.importorskip('numpy')

VALUE = 'https://qr.synthetic.invalid/claim?token=' + 'AbCd0123456789' * 14


def qr(text: str, scale: int = 1):
    code = cv.QRCodeEncoder_create().encode(text)
    code = cv.copyMakeBorder(code, 4, 4, 4, 4, cv.BORDER_CONSTANT, value=255)
    return cv.resize(code, None, fx=scale, fy=scale, interpolation=cv.INTER_NEAREST)


def image_bytes(pixels, suffix: str = '.png') -> bytes:
    ok, encoded = cv.imencode(suffix, pixels)
    assert ok
    return bytes(encoded)


@pytest.mark.parametrize('scale', [1, 4, 20])
def test_tiny_clear_and_large_codes(scale):
    assert mail_search.qr_value(image_bytes(qr(VALUE, scale))) == VALUE


@pytest.mark.parametrize('rotation', [0, 1, 2, 3])
def test_photographed_screen_perspective_and_moire(rotation):
    code = cv.resize(qr(VALUE), (610, 610), interpolation=cv.INTER_NEAREST)
    source = np.float32([[0, 0], [609, 0], [609, 609], [0, 609]])
    corners = np.float32([[377, 679], [996, 704], [982, 1295], [386, 1275]])
    photo = cv.warpPerspective(code, cv.getPerspectiveTransform(source, corners),
                              (1368, 1824), borderValue=235)
    ripple = (np.sin(np.arange(1824) * 2 * np.pi / 2.5) * 45).astype(np.int16)
    photo = np.clip(photo.astype(np.int16) + ripple[:, None], 0, 255).astype(np.uint8)
    payload = image_bytes(np.rot90(photo, rotation), '.jpg')
    assert mail_search.qr_value(payload) == VALUE


@pytest.mark.parametrize('same_value', [False, True])
def test_two_codes_do_not_choose_an_arbitrary_invoice(same_value):
    first = qr('https://qr.synthetic.invalid/claim?invoice=first', 5)
    second = qr('https://qr.synthetic.invalid/claim?invoice=' + ('first' if same_value else 'second'), 5)
    canvas = np.full((max(first.shape[0], second.shape[0]) + 80,
                      first.shape[1] + second.shape[1] + 140), 255, dtype=np.uint8)
    canvas[40:40 + first.shape[0], 40:40 + first.shape[1]] = first
    x = first.shape[1] + 100
    canvas[40:40 + second.shape[0], x:x + second.shape[1]] = second
    assert mail_search.qr_value(image_bytes(canvas)) == ''


def test_regular_invoice_without_code_and_invalid_image_remain_unmatched():
    invoice = Image.new('L', (1200, 900), 'white')
    drawing = ImageDraw.Draw(invoice)
    drawing.rectangle((40, 100, 1160, 850), outline='black', width=3)
    for row in range(180, 800, 80):
        drawing.line((40, row, 1160, row), fill='black', width=2)
    drawing.text((80, 140), 'Invoice no: 12345678901234567890', fill='black')
    drawing.text((80, 240), 'Supplier Synthetic Example / Total 154.00', fill='black')
    stream = io.BytesIO()
    invoice.save(stream, format='PNG')
    assert mail_search.qr_value(stream.getvalue()) == ''
    assert mail_search.qr_value(b'not an image') == ''
    assert mail_search.qr_value(b'') == ''


def test_oversized_images_are_rejected_before_opencv_decode(monkeypatch):
    stream = io.BytesIO()
    Image.new('L', (4001, 4001), 'white').save(stream, format='PNG')

    def unexpected_decode(*args, **kwargs):
        pytest.fail('oversized images must be rejected before OpenCV allocates pixels')

    monkeypatch.setattr(cv, 'imdecode', unexpected_decode)
    assert mail_search.qr_value(stream.getvalue()) == ''


def test_decode_keeps_original_bytes_and_bounds_large_photo_work(monkeypatch):
    canvas = np.full((4000, 4000), 255, dtype=np.uint8)
    code = qr('https://qr.synthetic.invalid/claim?invoice=bounded', 20)
    canvas[1000:1000 + code.shape[0], 1000:1000 + code.shape[1]] = code
    payload = image_bytes(canvas)
    original = bytes(payload)
    resize = cv.resize
    attempted_sizes = []

    def observe_resize(pixels, size, **kwargs):
        attempted_sizes.append(size)
        return resize(pixels, size, **kwargs)

    monkeypatch.setattr(cv, 'resize', observe_resize)
    assert mail_search.qr_value(payload) == 'https://qr.synthetic.invalid/claim?invoice=bounded'
    assert payload == original
    assert 1 <= len(attempted_sizes) <= 4
    assert max(max(size) for size in attempted_sizes) <= 1600
