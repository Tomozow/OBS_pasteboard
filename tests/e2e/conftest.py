import obs_harness as harness
import pytest

from pasteboard_core.clipboard_win import read_clipboard, set_clipboard_text
from pasteboard_core.names import (
    HOTKEY_PASTE,
    HOTKEY_REMOVE_LAST,
    SOURCE_NAME_IMAGE,
    SOURCE_NAME_TEXT,
)

__all__ = ["HOTKEY_PASTE", "HOTKEY_REMOVE_LAST", "SOURCE_NAME_IMAGE", "SOURCE_NAME_TEXT"]


@pytest.fixture(scope="module")
def ws():
    """E2E 専用の OBS インスタンスを起動し、WebSocket 接続を返す。

    確認ダイアログは実 OBS の別プロセスへ人手なしで応答できないため、
    このメインの E2E スイートでは明示的に無効化する
    (確認ダイアログ自体は test_confirm_dialog_e2e.py が専用インスタンスで検証する)。
    """
    settings = {
        "save_dir": str(harness.ARTIFACTS_DIR).replace("\\", "/"),
        "confirm_before_paste": False,
    }
    with harness.run_e2e_instance(harness.E2E_COLLECTION, settings) as client:
        yield client


@pytest.fixture(scope="module")
def obs_env(ws):
    """スクリプトのロード完了(ホットキー登録)を待ち、テスト用シーン名を返す。"""
    harness.wait_until(
        lambda: HOTKEY_PASTE in ws.get_hotkey_list().hotkeys,
        timeout=60,
        desc="obs_pasteboard.py のロード(ホットキー登録)。"
        "OBS の [ツール] → [スクリプト] で Python パスの設定を確認してください",
    )
    scene_name = ws.get_scene_list().current_program_scene_name
    return {"ws": ws, "scene": scene_name}


def _pasted_input_names(ws, scene: str) -> list[str]:
    items = ws.get_scene_item_list(scene).scene_items
    return [
        item["sourceName"]
        for item in items
        if item["sourceName"].startswith((SOURCE_NAME_IMAGE, SOURCE_NAME_TEXT))
    ]


@pytest.fixture
def clean_scene(obs_env):
    """各テストの前後で、ペースト由来のソースをシーンから一掃する。"""
    ws, scene = obs_env["ws"], obs_env["scene"]

    def cleanup():
        for name in _pasted_input_names(ws, scene):
            try:
                ws.remove_input(name)
            except Exception:
                pass
        # シーンアイテムの消滅まで待つ(削除は非同期のため)
        try:
            harness.wait_until(
                lambda: not _pasted_input_names(ws, scene), timeout=5,
                desc="ペーストソースの後片付け",
            )
        except TimeoutError:
            pass

    cleanup()
    yield obs_env
    cleanup()


@pytest.fixture(scope="session", autouse=True)
def restore_clipboard():
    """テスト終了後、クリップボードのテキストをベストエフォートで復元する。"""
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
