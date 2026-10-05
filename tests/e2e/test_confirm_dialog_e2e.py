"""確認ダイアログ機能の E2E テスト(専用の OBS インスタンスを別途起動)。

実 OBS プロセス内でネイティブ Win32 ダイアログを実際に開き、
FindWindowW + BM_CLICK でボタンをクリックして OK/キャンセルの両方を検証する。
確認ダイアログを開いたまま OBS が異常終了しないことも、この一連の
E2E フローが最後まで正常終了すること自体が裏付けとなる。
"""

import ctypes
import time
import uuid
from ctypes import wintypes

import obs_harness as harness
import pytest
from conftest import HOTKEY_PASTE, SOURCE_NAME_IMAGE, SOURCE_NAME_TEXT

from pasteboard_core.clipboard_win import set_clipboard_image, set_clipboard_text
from pasteboard_core.confirm_dialog import DIALOG_TITLE

pytestmark = pytest.mark.e2e

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetDlgItem.restype = wintypes.HWND
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = ctypes.c_long
_user32.IsWindow.argtypes = [wintypes.HWND]

_BM_CLICK = 0x00F5
_IDOK = 1
_IDCANCEL = 2


@pytest.fixture(scope="module")
def ws_confirm():
    """確認ダイアログ有効の専用コレクションで OBS を起動する(メインの E2E とは別インスタンス)。"""
    settings = {
        "save_dir": str(harness.ARTIFACTS_DIR).replace("\\", "/"),
        "confirm_before_paste": True,
    }
    with harness.run_e2e_instance(harness.E2E_CONFIRM_COLLECTION, settings) as client:
        yield client


@pytest.fixture(scope="module")
def obs_env_confirm(ws_confirm):
    harness.wait_until(
        lambda: HOTKEY_PASTE in ws_confirm.get_hotkey_list().hotkeys,
        timeout=60,
        desc="obs_pasteboard.py のロード(ホットキー登録)",
    )
    scene_name = ws_confirm.get_scene_list().current_program_scene_name
    return {"ws": ws_confirm, "scene": scene_name}


def _pasted_item_names(ws, scene: str) -> list[str]:
    return [
        item["sourceName"]
        for item in ws.get_scene_item_list(scene).scene_items
        if item["sourceName"].startswith((SOURCE_NAME_TEXT, SOURCE_NAME_IMAGE))
    ]


@pytest.fixture
def clean_scene_confirm(obs_env_confirm):
    ws, scene = obs_env_confirm["ws"], obs_env_confirm["scene"]

    def cleanup():
        for name in _pasted_item_names(ws, scene):
            try:
                ws.remove_input(name)
            except Exception:
                pass

    cleanup()
    yield obs_env_confirm
    cleanup()


def _find_dialog(timeout: float = 10.0) -> int:
    hwnd = 0

    def check() -> bool:
        nonlocal hwnd
        hwnd = _user32.FindWindowW(None, DIALOG_TITLE)
        return bool(hwnd)

    harness.wait_until(check, timeout=timeout, desc="確認ダイアログの表示")
    return hwnd


def _click_dialog_button(hwnd: int, control_id: int) -> None:
    button = _user32.GetDlgItem(hwnd, control_id)
    assert button, f"ダイアログにボタン (id={control_id}) が見つかりません"
    _user32.SendMessageW(button, _BM_CLICK, 0, 0)


def test_confirm_dialog_ok_proceeds_with_paste(clean_scene_confirm):
    ws, scene = clean_scene_confirm["ws"], clean_scene_confirm["scene"]
    token = f"確認ダイアログ OK テスト {uuid.uuid4().hex[:8]}"
    set_clipboard_text(token)

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    hwnd = _find_dialog()
    _click_dialog_button(hwnd, _IDOK)

    harness.wait_until(
        lambda: bool(_pasted_item_names(ws, scene)), timeout=10, desc="OK 後のペースト完了"
    )
    name = _pasted_item_names(ws, scene)[0]
    settings = ws.get_input_settings(name).input_settings
    assert settings["text"] == token

    # ダイアログ自体は閉じてプロセスが生き続けていること
    harness.wait_until(
        lambda: not _user32.IsWindow(hwnd), timeout=5, desc="ダイアログウィンドウの破棄"
    )
    assert ws.get_version().obs_version  # OBS プロセスが生きていることの確認


def test_confirm_dialog_with_image_preview_ok(clean_scene_confirm):
    """画像プレビュー(LoadImageW によるビットマップ表示)を含むダイアログでも正常動作すること。

    プレビュー用の GDI ビットマップ生成・破棄が実 OBS プロセス内で
    問題なく行えることを検証する(ここが最もクラッシュリスクの高い経路)。
    """
    ws, scene = clean_scene_confirm["ws"], clean_scene_confirm["scene"]
    width, height = 64, 48
    rgba = bytearray()
    for y in range(height):
        for x in range(width):
            rgba += bytes((x * 4 % 256, y * 5 % 256, (x + y) % 256, 255))
    set_clipboard_image(bytes(rgba), width, height)

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    hwnd = _find_dialog()
    _click_dialog_button(hwnd, _IDOK)

    harness.wait_until(
        lambda: bool(_pasted_item_names(ws, scene)),
        timeout=10,
        desc="画像プレビュー後のペースト完了",
    )
    harness.wait_until(
        lambda: not _user32.IsWindow(hwnd), timeout=5, desc="ダイアログウィンドウの破棄"
    )
    assert ws.get_version().obs_version  # OBS プロセスが生きていることの確認


def test_confirm_dialog_cancel_blocks_paste(clean_scene_confirm):
    ws, scene = clean_scene_confirm["ws"], clean_scene_confirm["scene"]
    set_clipboard_text(f"確認ダイアログ Cancel テスト {uuid.uuid4().hex[:8]}")

    harness.trigger_hotkey(ws, HOTKEY_PASTE)
    hwnd = _find_dialog()
    _click_dialog_button(hwnd, _IDCANCEL)

    harness.wait_until(
        lambda: not _user32.IsWindow(hwnd), timeout=5, desc="ダイアログウィンドウの破棄"
    )
    # キャンセルしたのでソースが作られていないこと(少し待って確認)
    time.sleep(1.0)
    assert _pasted_item_names(ws, scene) == []
    assert ws.get_version().obs_version  # OBS プロセスが生きていることの確認
