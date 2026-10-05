import io

import pytest
from PIL import Image

from pasteboard_core.png_codec import PngError, decode_png, encode_png


def _pattern_rgba(width: int, height: int) -> bytes:
    """座標に依存する再現可能な RGBA パターンを生成する。"""
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes((x * 7 % 256, y * 13 % 256, (x + y) * 3 % 256, 255 - (x % 32)))
    return bytes(out)


@pytest.mark.parametrize(("width", "height"), [(1, 1), (3, 2), (17, 9), (256, 128)])
def test_encode_png_roundtrip_with_pillow(width: int, height: int):
    rgba = _pattern_rgba(width, height)
    png = encode_png(rgba, width, height)

    image = Image.open(io.BytesIO(png))
    assert image.size == (width, height)
    assert image.mode == "RGBA"
    assert image.tobytes() == rgba


def test_encode_png_rejects_wrong_length():
    with pytest.raises(ValueError, match="RGBA データ長"):
        encode_png(b"\x00" * 10, 2, 2)


def test_encode_png_rejects_zero_size():
    with pytest.raises(ValueError):
        encode_png(b"", 0, 0)


def test_encode_png_large_image_is_fast():
    """フル HD 画像が現実的な時間で変換できること(性能ガード)。"""
    import time

    width, height = 1920, 1080
    rgba = bytes(width * height * 4)
    started = time.perf_counter()
    png = encode_png(rgba, width, height)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"PNG エンコードが遅すぎます: {elapsed:.2f}s"
    assert png.startswith(b"\x89PNG")


@pytest.mark.parametrize(("width", "height"), [(1, 1), (3, 2), (17, 9), (256, 128)])
def test_decode_png_roundtrips_own_encoder(width: int, height: int):
    rgba = _pattern_rgba(width, height)
    png = encode_png(rgba, width, height)
    decoded, decoded_width, decoded_height = decode_png(png)
    assert (decoded_width, decoded_height) == (width, height)
    assert decoded == rgba


@pytest.mark.parametrize(
    "mode",
    ["RGB", "RGBA", "L", "LA", "P"],
)
def test_decode_png_matches_pillow_for_all_color_types(mode: str):
    """Pillow をオラクルとして、全カラータイプで正しく RGBA へ変換できることを検証する。"""
    width, height = 13, 7
    if mode == "P":
        # パレット画像: 単純なグラデーションでインデックスを構成
        image = Image.new("P", (width, height))
        palette = []
        for i in range(256):
            palette += [i, (i * 2) % 256, (i * 3) % 256]
        image.putpalette(palette)
        for y in range(height):
            for x in range(width):
                image.putpixel((x, y), (x * 17 + y * 5) % 256)
    else:
        channels = len(mode)
        image = Image.new(mode, (width, height))
        for y in range(height):
            for x in range(width):
                if channels == 1:
                    value = (x * 20 + y * 7) % 256
                else:
                    value = tuple((x * 20 + y * 7 + c * 50) % 256 for c in range(channels))
                image.putpixel((x, y), value)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    decoded, decoded_width, decoded_height = decode_png(buffer.getvalue())

    assert (decoded_width, decoded_height) == (width, height)
    expected = image.convert("RGBA").tobytes()
    assert decoded == expected


def test_decode_png_rejects_bad_signature():
    with pytest.raises(PngError, match="シグネチャ"):
        decode_png(b"not a png")


def test_decode_png_rejects_16bit_depth():
    image = Image.new("I;16", (2, 2))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    with pytest.raises(PngError):
        decode_png(buffer.getvalue())
