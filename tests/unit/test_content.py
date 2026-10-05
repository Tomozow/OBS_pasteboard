from datetime import datetime

from pasteboard_core.clipboard_win import ClipboardImage, ClipboardSnapshot
from pasteboard_core.content import (
    ImageBytesContent,
    ImageFileContent,
    TextContent,
    image_to_content,
    make_paste_filename,
    select_content,
)
from pasteboard_core.dib import rgba_to_dib
from pasteboard_core.png_codec import encode_png

_RGBA_2X2 = bytes(range(16))
_PNG = encode_png(_RGBA_2X2, 2, 2)


def _snapshot(image=None, text=None, files=()):
    return ClipboardSnapshot(image=image, text=text, files=tuple(files))


def test_png_passthrough():
    result = image_to_content(ClipboardImage("png", _PNG))
    assert isinstance(result, ImageBytesContent)
    assert result.extension == ".png"
    assert result.data == _PNG


def test_dib_converted_to_png():
    dib = rgba_to_dib(_RGBA_2X2, 2, 2)
    result = image_to_content(ClipboardImage("dib", dib))
    assert result.extension == ".png"
    assert result.data.startswith(b"\x89PNG")


def test_broken_dib_falls_back_to_bmp():
    import struct

    rle = struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 8, 1, 8, 0, 0, 0, 0) + b"\x00" * 1032
    result = image_to_content(ClipboardImage("dib", rle))
    assert result.extension == ".bmp"
    assert result.data.startswith(b"BM")


def test_low_bpp_dib_saved_as_bmp_without_conversion():
    """16bpp・パレット DIB は per-pixel 変換せず BMP パススルーする(性能対策)。"""
    import struct

    # 2x1 の 16bpp 565(有効な DIB だが 24bpp 未満)
    header = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 16, 3, 4, 0, 0, 0, 0)
    masks = struct.pack("<III", 0xF800, 0x07E0, 0x001F)
    pixels = struct.pack("<HH", 0xF800, 0x07E0)
    dib = header + masks + pixels

    result = image_to_content(ClipboardImage("dib", dib))
    assert result.extension == ".bmp"
    assert result.data.startswith(b"BM")
    assert result.data[14:] == dib  # 中身は無変換(ファイルヘッダ付与のみ)


def test_prefer_image_over_text():
    snapshot = _snapshot(image=ClipboardImage("png", _PNG), text="hello")
    assert isinstance(select_content(snapshot, prefer_image=True), ImageBytesContent)
    assert isinstance(select_content(snapshot, prefer_image=False), TextContent)


def test_text_only():
    result = select_content(_snapshot(text="こんにちは"), prefer_image=True)
    assert isinstance(result, TextContent)
    assert result.text == "こんにちは"


def test_whitespace_text_is_ignored():
    assert select_content(_snapshot(text="   \n\t ")) is None


def test_empty_snapshot():
    assert select_content(_snapshot()) is None


def test_image_file_from_clipboard_files(tmp_path):
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(_PNG)
    other = tmp_path / "doc.txt"
    other.write_text("x")

    result = select_content(_snapshot(files=(str(other), str(image_path))))
    assert isinstance(result, ImageFileContent)
    assert result.path == str(image_path)


def test_nonexistent_image_file_skipped_falls_back_to_text():
    snapshot = _snapshot(text="fallback", files=("Z:/nope/missing.png",))
    result = select_content(snapshot)
    assert isinstance(result, TextContent)


def test_make_paste_filename():
    name = make_paste_filename(".png", datetime(2026, 7, 29, 12, 34, 56, 789000))
    assert name == "paste_20260729-123456-789.png"
