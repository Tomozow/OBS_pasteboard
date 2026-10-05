"""実 Windows クリップボードとの読み書きテスト。

注意: 実行中はクリップボードの内容が一時的に書き換わる。
終了時にテキストのみベストエフォートで復元する。
"""

import io
import struct

import pytest
from PIL import Image

from pasteboard_core.clipboard_win import (
    CF_HDROP,
    _open_clipboard,
    _set_format,
    _user32,
    read_clipboard,
    set_clipboard_image,
    set_clipboard_text,
)
from pasteboard_core.dib import dib_to_rgba
from pasteboard_core.png_codec import encode_png


@pytest.fixture(autouse=True, scope="module")
def restore_clipboard_text():
    try:
        before = read_clipboard()
    except OSError:
        before = None
    yield
    if before is not None and before.text:
        try:
            set_clipboard_text(before.text)
        except OSError:
            pass


def _pattern_rgba(width: int, height: int) -> bytes:
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes((x * 40 % 256, y * 90 % 256, (x + y) * 25 % 256, 255))
    return bytes(out)


def test_text_roundtrip():
    text = "OBS ペーストテスト 🎬\n2 行目\tタブ"
    set_clipboard_text(text)
    snapshot = read_clipboard()
    assert snapshot.text == text


def test_image_roundtrip_prefers_png():
    rgba = _pattern_rgba(6, 4)
    set_clipboard_image(rgba, 6, 4)
    snapshot = read_clipboard()

    assert snapshot.image is not None
    assert snapshot.image.kind == "png"
    with Image.open(io.BytesIO(snapshot.image.data)) as image:
        assert image.size == (6, 4)
        assert image.convert("RGBA").tobytes() == rgba


def test_dib_only_read_via_synthesized_formats():
    """CF_DIB だけを置いた場合も DIB(V5) 経由で読めること。"""
    from pasteboard_core.dib import rgba_to_dib

    rgba = _pattern_rgba(5, 3)
    dib = rgba_to_dib(rgba, 5, 3)
    with _open_clipboard():
        _user32.EmptyClipboard()
        _set_format(8, dib)  # CF_DIB

    snapshot = read_clipboard()
    assert snapshot.image is not None
    assert snapshot.image.kind in ("dib", "dibv5")
    result, width, height = dib_to_rgba(snapshot.image.data)
    assert (width, height) == (5, 3)
    # Windows が DIBV5 へ変換する過程でアルファが落ちることがあるため RGB のみ比較
    assert result[0::4] == rgba[0::4]
    assert result[1::4] == rgba[1::4]
    assert result[2::4] == rgba[2::4]


def test_hdrop_file_list(tmp_path):
    """CF_HDROP(コピーしたファイル)の読み取り。"""
    png_path = tmp_path / "画像テスト.png"
    png_path.write_bytes(encode_png(b"\xff\x00\x00\xff", 1, 1))

    # DROPFILES 構造体 + ワイド文字列(二重 NUL 終端)
    wide = str(png_path).encode("utf-16-le") + b"\x00\x00\x00\x00"
    dropfiles = struct.pack("<IiiII", 20, 0, 0, 0, 1) + wide
    with _open_clipboard():
        _user32.EmptyClipboard()
        _set_format(CF_HDROP, dropfiles)

    snapshot = read_clipboard()
    assert snapshot.files == (str(png_path),)


def test_empty_clipboard_snapshot():
    with _open_clipboard():
        _user32.EmptyClipboard()
    snapshot = read_clipboard()
    assert snapshot.is_empty
