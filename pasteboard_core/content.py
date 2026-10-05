"""クリップボード内容から「何をどうペーストするか」を決める純ロジック。

OBS API に依存しないため、OBS なしで完全にテストできる。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pasteboard_core.clipboard_win import ClipboardImage, ClipboardSnapshot
from pasteboard_core.dib import (
    DibError,
    dib_bits_per_pixel,
    dib_to_rgba,
    wrap_dib_as_bmp,
)
from pasteboard_core.png_codec import encode_png

# OBS の image_source が読み込める拡張子
IMAGE_FILE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tga", ".psd", ".jxr"}
)


@dataclass(frozen=True)
class ImageBytesContent:
    """ファイルに保存してから image_source にする画像データ。"""

    data: bytes
    extension: str  # ".png" または ".bmp"


@dataclass(frozen=True)
class ImageFileContent:
    """既存の画像ファイルをそのまま image_source にする。"""

    path: str


@dataclass(frozen=True)
class TextContent:
    text: str


PasteContent = ImageBytesContent | ImageFileContent | TextContent


def image_to_content(image: ClipboardImage) -> ImageBytesContent:
    """クリップボード画像を保存可能なバイト列に変換する。

    PNG はそのまま、DIB は PNG へ変換。24bpp 未満(16bpp・パレット)は
    per-pixel 変換が大画像で秒単位かかるため、標準的な BMP として
    そのまま保存する(OBS の画像ローダが直接読める)。解釈できない DIB も
    同じく BMP ラップにフォールバックする。
    """
    if image.kind == "png":
        return ImageBytesContent(image.data, ".png")
    try:
        if dib_bits_per_pixel(image.data) < 24:
            return ImageBytesContent(wrap_dib_as_bmp(image.data), ".bmp")
        rgba, width, height = dib_to_rgba(image.data)
        return ImageBytesContent(encode_png(rgba, width, height), ".png")
    except DibError:
        return ImageBytesContent(wrap_dib_as_bmp(image.data), ".bmp")


def _first_image_file(files: tuple[str, ...]) -> str | None:
    for path in files:
        if Path(path).suffix.lower() in IMAGE_FILE_EXTENSIONS and Path(path).is_file():
            return path
    return None


def select_content(snapshot: ClipboardSnapshot, prefer_image: bool = True) -> PasteContent | None:
    """スナップショットからペースト対象を 1 つ選ぶ。

    prefer_image=True: 画像データ → 画像ファイル → テキスト の優先順。
    prefer_image=False: テキストを最優先する。
    """
    text = snapshot.text.strip() if snapshot.text else ""

    def image_candidate() -> PasteContent | None:
        if snapshot.image is not None:
            return image_to_content(snapshot.image)
        file_path = _first_image_file(snapshot.files)
        if file_path:
            return ImageFileContent(file_path)
        return None

    if not prefer_image and text:
        return TextContent(text)
    image = image_candidate()
    if image is not None:
        return image
    if text:
        return TextContent(text)
    return None


def make_paste_filename(extension: str, now: datetime | None = None) -> str:
    """衝突しないタイムスタンプ付きファイル名を生成する。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"paste_{stamp}{extension}"
