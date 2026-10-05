"""confirm_dialog.py の純ロジック(describe_content/preview_rgba)のテスト。

confirm_paste() 自体は実際に Win32 ウィンドウを開くため、ここではテストしない
(実 OBS プロセス内での E2E テストで検証する)。
"""

import struct

from pasteboard_core.confirm_dialog import describe_content, preview_rgba
from pasteboard_core.content import ImageBytesContent, ImageFileContent, TextContent
from pasteboard_core.dib import rgba_to_dib
from pasteboard_core.png_codec import encode_png


def _pattern_rgba(width: int, height: int) -> bytes:
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes((x * 9 % 256, y * 11 % 256, (x + y) * 5 % 256, 255))
    return bytes(out)


def test_describe_content_png_image():
    rgba = _pattern_rgba(10, 5)
    content = ImageBytesContent(encode_png(rgba, 10, 5), ".png")
    text = describe_content(content)
    assert "PNG" in text
    assert "10 x 5 px" in text


def test_describe_content_bmp_image():
    dib = rgba_to_dib(_pattern_rgba(8, 4), 8, 4)
    from pasteboard_core.dib import wrap_dib_as_bmp

    content = ImageBytesContent(wrap_dib_as_bmp(dib), ".bmp")
    text = describe_content(content)
    assert "BMP" in text
    assert "8 x 4 px" in text


def test_describe_content_image_file():
    content = ImageFileContent("C:/Users/test/Pictures/shot.png")
    text = describe_content(content)
    assert "shot.png" in text


def test_describe_content_text_truncates_long_text():
    content = TextContent("あ" * 500)
    text = describe_content(content)
    assert text.count("あ") == 300
    assert text.endswith("…")


def test_describe_content_short_text_not_truncated():
    content = TextContent("短いテキスト")
    text = describe_content(content)
    assert "短いテキスト" in text
    assert "…" not in text


def test_preview_rgba_for_png():
    rgba = _pattern_rgba(20, 10)
    content = ImageBytesContent(encode_png(rgba, 20, 10), ".png")
    result = preview_rgba(content)
    assert result is not None
    preview, width, height = result
    assert (width, height) == (20, 10)  # 480x360 未満なので縮小されない
    assert preview == rgba


def test_preview_rgba_downscales_large_png():
    rgba = _pattern_rgba(960, 720)
    content = ImageBytesContent(encode_png(rgba, 960, 720), ".png")
    result = preview_rgba(content)
    assert result is not None
    _, width, height = result
    assert width <= 480
    assert height <= 360


def test_preview_rgba_for_low_bpp_bmp_passthrough():
    """低 bpp(16bpp 等)は BMP パススルー保存されるが、プレビューは per-pixel 変換で生成できる。"""
    from pasteboard_core.dib import wrap_dib_as_bmp

    header = struct.pack("<IiiHHIIiiII", 40, 4, 2, 1, 16, 3, 0, 0, 0, 0, 0)
    masks = struct.pack("<III", 0xF800, 0x07E0, 0x001F)
    # 4 幅 x 2 行(トップダウンなし = ボトムアップ)、1 行 = 4 ピクセル分
    row = struct.pack("<HHHH", 0xF800, 0x07E0, 0x001F, 0xFFFF)
    dib = header + masks + row + row
    content = ImageBytesContent(wrap_dib_as_bmp(dib), ".bmp")

    result = preview_rgba(content)
    assert result is not None
    preview, width, height = result
    assert (width, height) == (4, 2)
    assert preview[0:4] == bytes((255, 0, 0, 255))  # 赤


def test_preview_rgba_none_for_text():
    assert preview_rgba(TextContent("hello")) is None


def test_preview_rgba_none_for_image_file():
    assert preview_rgba(ImageFileContent("C:/nope.jpg")) is None


def test_preview_rgba_none_for_undecodable_bmp():
    garbage = b"BM" + b"\x00" * 40
    content = ImageBytesContent(garbage, ".bmp")
    assert preview_rgba(content) is None


def test_preview_rgba_none_for_corrupt_png():
    content = ImageBytesContent(b"\x89PNG\r\n\x1a\nnot really a png", ".png")
    assert preview_rgba(content) is None
