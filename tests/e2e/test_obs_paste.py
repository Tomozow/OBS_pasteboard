"""実 OBS に対するエンドツーエンドテスト。

前提: OBS Studio が起動しており(起動していなければ自動起動)、
obs-websocket が有効であること。配信・録画中は自動でスキップされる。

フロー: クリップボードへ書き込み → WebSocket でホットキー発火 →
スクリプトがソースを生成 → シーンアイテムとレンダリング結果を検証。
"""

import base64
import io
import uuid
from pathlib import Path

import obs_harness as harness
import pytest
from conftest import HOTKEY_PASTE, HOTKEY_REMOVE_LAST, SOURCE_NAME_IMAGE, SOURCE_NAME_TEXT
from PIL import Image

from pasteboard_core.clipboard_win import set_clipboard_image, set_clipboard_text

pytestmark = pytest.mark.e2e

# 四隅判定用パターン: 左上=赤、右上=緑、左下=青、右下=白
_QUAD_W, _QUAD_H = 96, 64
_QUAD_COLORS = {
    (24, 16): (255, 0, 0),
    (72, 16): (0, 255, 0),
    (24, 48): (0, 0, 255),
    (72, 48): (255, 255, 255),
}


def _quadrant_rgba() -> bytes:
    out = bytearray()
    for y in range(_QUAD_H):
        for x in range(_QUAD_W):
            if y < _QUAD_H // 2:
                color = (255, 0, 0) if x < _QUAD_W // 2 else (0, 255, 0)
            else:
                color = (0, 0, 255) if x < _QUAD_W // 2 else (255, 255, 255)
            out += bytes(color) + b"\xff"
    return bytes(out)


def _pasted_item_names(ws, scene: str, prefix: str) -> list[str]:
    return [
        item["sourceName"]
        for item in ws.get_scene_item_list(scene).scene_items
        if item["sourceName"].startswith(prefix)
    ]


def _wait_for_item_name(ws, scene: str, prefix: str) -> str:
    found: list[str] = []

    def check() -> bool:
        found[:] = _pasted_item_names(ws, scene, prefix)
        return bool(found)

    harness.wait_until(check, timeout=10, desc=f"シーンに「{prefix}」が追加される")
    return found[0]


def _screenshot(ws, source_name: str, width: int, height: int) -> Image.Image:
    response = ws.get_source_screenshot(source_name, "png", width, height, -1)
    payload = response.image_data.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(payload))).convert("RGB")


def test_script_hotkeys_registered(obs_env):
    hotkeys = obs_env["ws"].get_hotkey_list().hotkeys
    assert HOTKEY_PASTE in hotkeys
    assert HOTKEY_REMOVE_LAST in hotkeys


def test_paste_text_end_to_end(clean_scene):
    ws, scene = clean_scene["ws"], clean_scene["scene"]
    token = f"E2E ペースト {uuid.uuid4().hex[:8]} 🎬"
    set_clipboard_text(token)

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    name = _wait_for_item_name(ws, scene, SOURCE_NAME_TEXT)

    settings = ws.get_input_settings(name).input_settings
    assert settings["text"] == token


def test_paste_image_end_to_end(clean_scene):
    ws, scene = clean_scene["ws"], clean_scene["scene"]
    set_clipboard_image(_quadrant_rgba(), _QUAD_W, _QUAD_H)

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    name = _wait_for_item_name(ws, scene, SOURCE_NAME_IMAGE)

    # 保存ファイルが指定ディレクトリに生成されている
    settings = ws.get_input_settings(name).input_settings
    saved = Path(settings["file"])
    assert saved.is_file()
    assert saved.parent.resolve() == harness.ARTIFACTS_DIR.resolve()

    # OBS のレンダリング結果をピクセル検証(読み込みは非同期のためポーリング)
    def rendered_correctly() -> bool:
        image = _screenshot(ws, name, _QUAD_W, _QUAD_H)
        if image.size != (_QUAD_W, _QUAD_H):
            return False
        return all(
            all(abs(a - b) <= 8 for a, b in zip(image.getpixel(pos), expected, strict=True))
            for pos, expected in _QUAD_COLORS.items()
        )

    harness.wait_until(rendered_correctly, timeout=10, desc="画像ソースのレンダリング内容")


def test_image_priority_over_text(clean_scene):
    """画像とテキストが同居する場合は画像が優先される(既定設定)。"""
    ws, scene = clean_scene["ws"], clean_scene["scene"]
    set_clipboard_image(_quadrant_rgba(), _QUAD_W, _QUAD_H)

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    _wait_for_item_name(ws, scene, SOURCE_NAME_IMAGE)
    assert _pasted_item_names(ws, scene, SOURCE_NAME_TEXT) == []


def test_remove_last_paste_end_to_end(clean_scene):
    ws, scene = clean_scene["ws"], clean_scene["scene"]
    set_clipboard_text(f"削除テスト {uuid.uuid4().hex[:8]}")

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    name = _wait_for_item_name(ws, scene, SOURCE_NAME_TEXT)

    harness.trigger_hotkey(ws, HOTKEY_REMOVE_LAST)
    harness.wait_until(
        lambda: _pasted_item_names(ws, scene, SOURCE_NAME_TEXT) == [],
        timeout=10,
        desc="ペーストしたソースの削除",
    )
    # 入力自体の破棄は非同期のため、入力リストから消えるまで待つ
    harness.wait_until(
        lambda: name not in (entry["inputName"] for entry in ws.get_input_list().inputs),
        timeout=10,
        desc="ペーストした入力の破棄",
    )


def test_multiple_pastes_get_unique_names(clean_scene):
    ws, scene = clean_scene["ws"], clean_scene["scene"]
    set_clipboard_text(f"複数回 {uuid.uuid4().hex[:8]}")

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    _wait_for_item_name(ws, scene, SOURCE_NAME_TEXT)
    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    harness.wait_until(
        lambda: len(_pasted_item_names(ws, scene, SOURCE_NAME_TEXT)) == 2,
        timeout=10,
        desc="2 つめのテキストソース追加",
    )

    # 破棄処理中のソース名が一時的に残ることがあるため、連番の具体値ではなく
    # 「2 つのソースが一意な名前で共存できること」を検証する
    names = _pasted_item_names(ws, scene, SOURCE_NAME_TEXT)
    assert len(names) == 2
    assert len(set(names)) == 2
    assert all(name.startswith(SOURCE_NAME_TEXT) for name in names)
