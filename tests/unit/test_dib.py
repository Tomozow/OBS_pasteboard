import io
import struct

import pytest
from PIL import Image

from pasteboard_core.dib import DibError, dib_to_rgba, rgba_to_dib, wrap_dib_as_bmp


def _pattern_rgba(width: int, height: int, opaque: bool = False) -> bytes:
    out = bytearray()
    for y in range(height):
        for x in range(width):
            alpha = 255 if opaque else 255 - (x * 16 + y) % 128  # 常に 128 以上(非ゼロ)
            out += bytes((x * 11 % 256, y * 17 % 256, (x * y) % 256, alpha))
    return bytes(out)


def test_roundtrip_rgba_dib_rgba():
    rgba = _pattern_rgba(7, 5)
    dib = rgba_to_dib(rgba, 7, 5)
    result, width, height = dib_to_rgba(dib)
    assert (width, height) == (7, 5)
    assert result == rgba


def test_rgba_to_dib_verified_by_pillow():
    """Pillow を独立オラクルとして BMP 経由で検証する。"""
    rgba = _pattern_rgba(9, 4, opaque=True)
    bmp = wrap_dib_as_bmp(rgba_to_dib(rgba, 9, 4))
    image = Image.open(io.BytesIO(bmp)).convert("RGBA")
    assert image.size == (9, 4)
    expected = Image.frombytes("RGBA", (9, 4), rgba).convert("RGB")
    assert image.convert("RGB").tobytes() == expected.tobytes()


def test_all_zero_alpha_treated_as_opaque():
    rgba = bytearray(_pattern_rgba(4, 4, opaque=True))
    rgba[3::4] = b"\x00" * 16  # アルファ全ゼロ
    dib = rgba_to_dib(bytes(rgba), 4, 4)
    result, _, _ = dib_to_rgba(dib)
    assert result[3::4] == b"\xff" * 16


def test_top_down_32bpp():
    # 1x2、負の高さ(トップダウン)。ピクセルは BGRA。
    header = struct.pack("<IiiHHIIiiII", 40, 1, -2, 1, 32, 0, 8, 0, 0, 0, 0)
    pixels = bytes((255, 0, 0, 255)) + bytes((0, 255, 0, 255))  # 青、緑
    rgba, width, height = dib_to_rgba(header + pixels)
    assert (width, height) == (1, 2)
    assert rgba == bytes((0, 0, 255, 255)) + bytes((0, 255, 0, 255))


def test_24bpp_with_stride_padding():
    # 2x2 の 24bpp(1 行 6 バイト + パディング 2 バイト)、ボトムアップ
    header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, 16, 0, 0, 0, 0)
    bottom_row = bytes((255, 0, 0)) + bytes((0, 255, 0)) + b"\x00\x00"  # BGR: 青、緑
    top_row = bytes((0, 0, 255)) + bytes((255, 255, 255)) + b"\x00\x00"  # BGR: 赤、白
    rgba, width, height = dib_to_rgba(header + bottom_row + top_row)
    assert (width, height) == (2, 2)
    expected = (
        bytes((255, 0, 0, 255)) + bytes((255, 255, 255, 255))  # 上段: 赤、白
        + bytes((0, 0, 255, 255)) + bytes((0, 255, 0, 255))  # 下段: 青、緑
    )
    assert rgba == expected


def test_16bpp_565_bitfields():
    # ヘッダ 40 + BI_BITFIELDS では 3 マスクがヘッダ直後に並ぶ
    header = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 16, 3, 4, 0, 0, 0, 0)
    masks = struct.pack("<III", 0xF800, 0x07E0, 0x001F)
    pixels = struct.pack("<HH", 0xF800, 0x07E0)  # 赤、緑
    rgba, _, _ = dib_to_rgba(header + masks + pixels)
    assert rgba == bytes((255, 0, 0, 255)) + bytes((0, 255, 0, 255))


def test_8bpp_palette():
    header = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 8, 0, 4, 0, 0, 2, 0)
    palette = bytes((255, 0, 0, 0)) + bytes((0, 0, 255, 0))  # BGRX: 青、赤
    pixels = bytes((0, 1)) + b"\x00\x00"  # stride=4
    rgba, _, _ = dib_to_rgba(header + palette + pixels)
    assert rgba == bytes((0, 0, 255, 255)) + bytes((255, 0, 0, 255))


def test_v5_header_with_alpha_mask():
    # BITMAPV5HEADER(124 バイト)、BI_BITFIELDS、アルファマスクあり
    header = bytearray(124)
    struct.pack_into("<IiiHHI", header, 0, 124, 1, 1, 1, 32, 3)
    struct.pack_into("<IIII", header, 40, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    pixels = bytes((10, 20, 30, 128))  # BGRA
    rgba, _, _ = dib_to_rgba(bytes(header) + pixels)
    assert rgba == bytes((30, 20, 10, 128))


def test_truncated_dib_raises():
    header = struct.pack("<IiiHHIIiiII", 40, 100, 100, 1, 32, 0, 0, 0, 0, 0, 0)
    with pytest.raises(DibError, match="不足"):
        dib_to_rgba(header + b"\x00" * 100)


def test_rle_compression_raises():
    header = struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 8, 1, 8, 0, 0, 0, 0)
    with pytest.raises(DibError, match="圧縮"):
        dib_to_rgba(header + b"\x00" * 1032)


def test_too_short_header_raises():
    with pytest.raises(DibError):
        dib_to_rgba(b"\x28\x00")


def test_slow_path_rejects_huge_image():
    """per-pixel 変換パスはピクセル数上限を超えると DibError(フリーズ防止)。"""
    # 3000x2000 の 16bpp(スライス代入の高速パスが使えない形式)
    width, height = 3000, 2000
    header = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 16, 3, 0, 0, 0, 0, 0)
    masks = struct.pack("<III", 0xF800, 0x07E0, 0x001F)
    pixels = bytes(width * 2 * height)
    with pytest.raises(DibError, match="大きすぎます"):
        dib_to_rgba(header + masks + pixels)


def test_wrap_dib_as_bmp_offsets():
    dib = rgba_to_dib(_pattern_rgba(3, 3, opaque=True), 3, 3)
    bmp = wrap_dib_as_bmp(dib)
    assert bmp[:2] == b"BM"
    (offset,) = struct.unpack_from("<I", bmp, 10)
    assert offset == 14 + 40  # パレットなし 32bpp

    # RLE 圧縮でも例外なく BMP 化できる(フォールバック用)
    rle_header = struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 8, 1, 8, 0, 0, 0, 0)
    bmp2 = wrap_dib_as_bmp(rle_header + b"\x00" * 64)
    (offset2,) = struct.unpack_from("<I", bmp2, 10)
    assert offset2 == 14 + 40 + 256 * 4  # 8bpp はフルパレット想定
