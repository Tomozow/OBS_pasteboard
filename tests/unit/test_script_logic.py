"""fake obspython を使った OBS スクリプト本体のロジックテスト(擬似 E2E)。"""

import importlib
import sys

import fake_obspython as fake
import pytest
from PIL import Image

from pasteboard_core.clipboard_win import ClipboardImage, ClipboardSnapshot
from pasteboard_core.png_codec import encode_png

_RGBA = bytes((255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 255, 255, 255, 255))
_PNG = encode_png(_RGBA, 2, 2)


def _snapshot(image=None, text=None, files=()):
    return ClipboardSnapshot(image=image, text=text, files=tuple(files))


@pytest.fixture
def obs_script(tmp_path, monkeypatch):
    fake.reset()
    monkeypatch.setitem(sys.modules, "obspython", fake)
    sys.modules.pop("obs_pasteboard", None)
    script = importlib.import_module("obs_pasteboard")

    settings = fake.obs_data_create()
    script.script_defaults(settings)
    fake.obs_data_set_string(settings, "save_dir", str(tmp_path / "saved"))
    # 確認ダイアログはネイティブ Win32 ウィンドウを実際に開くため、
    # コアのペーストロジックを検証する大半のテストでは既定で無効化する。
    # ダイアログ自体の挙動は test_confirm_paste_* が個別に検証する。
    fake.obs_data_set_bool(settings, "confirm_before_paste", False)
    script.script_load(settings)
    script.script_update(settings)
    yield script, settings, tmp_path
    sys.modules.pop("obs_pasteboard", None)


def _scene_items():
    return fake.obs_frontend_get_current_scene().scene.items


def test_hotkeys_registered(obs_script):
    script, _, _ = obs_script
    assert script.HOTKEY_PASTE in fake.hotkeys
    assert script.HOTKEY_REMOVE_LAST in fake.hotkeys


def test_paste_image_creates_centered_image_source(obs_script, monkeypatch):
    script, _, _ = obs_script
    monkeypatch.setattr(
        script, "read_clipboard", lambda: _snapshot(image=ClipboardImage("png", _PNG))
    )
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    items = _scene_items()
    assert len(items) == 1
    item = items[0]
    assert item.source.kind == "image_source"
    assert item.source.name == script.SOURCE_NAME_IMAGE

    saved_file = item.source.settings["file"]
    with Image.open(saved_file) as image:
        assert image.size == (2, 2)
        assert image.tobytes() == _RGBA

    assert item.bounds_type == fake.OBS_BOUNDS_SCALE_INNER
    assert item.pos == (960.0, 540.0)
    assert item.bounds == (1920 * 0.6, 1080 * 0.6)
    assert item.alignment == 0  # OBS_ALIGN_CENTER(obspython に定数が無いため生値)
    assert item.bounds_alignment == 0


def test_paste_text_creates_text_source(obs_script, monkeypatch):
    script, _, _ = obs_script
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot(text="配信テスト 🎥"))
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    items = _scene_items()
    assert len(items) == 1
    assert items[0].source.kind == "text_gdiplus_v3"
    assert items[0].source.settings["text"] == "配信テスト 🎥"


def test_prefer_text_when_disabled(obs_script, monkeypatch):
    script, settings, _ = obs_script
    fake.obs_data_set_bool(settings, "prefer_image", False)
    script.script_update(settings)
    monkeypatch.setattr(
        script,
        "read_clipboard",
        lambda: _snapshot(image=ClipboardImage("png", _PNG), text="テキスト優先"),
    )
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    assert _scene_items()[0].source.kind == "text_gdiplus_v3"


def test_empty_clipboard_is_noop(obs_script, monkeypatch):
    script, _, _ = obs_script
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot())
    fake.trigger_hotkey(script.HOTKEY_PASTE)
    assert _scene_items() == []


def test_clipboard_error_is_caught(obs_script, monkeypatch):
    script, _, _ = obs_script

    def boom():
        raise OSError("clipboard busy")

    monkeypatch.setattr(script, "read_clipboard", boom)
    fake.trigger_hotkey(script.HOTKEY_PASTE)  # 例外が外へ漏れないこと
    assert _scene_items() == []
    assert any(level == fake.LOG_WARNING for level, _ in fake.log_messages)


def test_unique_names_for_multiple_pastes(obs_script, monkeypatch):
    script, _, _ = obs_script
    monkeypatch.setattr(
        script, "read_clipboard", lambda: _snapshot(image=ClipboardImage("png", _PNG))
    )
    fake.trigger_hotkey(script.HOTKEY_PASTE)
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    names = [item.source.name for item in _scene_items()]
    assert names == [script.SOURCE_NAME_IMAGE, f"{script.SOURCE_NAME_IMAGE} (2)"]


def test_reuse_mode_updates_existing_source(obs_script, monkeypatch):
    script, settings, _ = obs_script
    fake.obs_data_set_bool(settings, "reuse_source", True)
    script.script_update(settings)

    texts = iter(["ひとつめ", "ふたつめ"])
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot(text=next(texts)))
    fake.trigger_hotkey(script.HOTKEY_PASTE)
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    items = _scene_items()
    assert len(items) == 1
    assert items[0].source.settings["text"] == "ふたつめ"


def test_remove_last_paste(obs_script, monkeypatch):
    script, _, _ = obs_script
    monkeypatch.setattr(
        script, "read_clipboard", lambda: _snapshot(image=ClipboardImage("png", _PNG))
    )
    fake.trigger_hotkey(script.HOTKEY_PASTE)
    assert len(_scene_items()) == 1

    fake.trigger_hotkey(script.HOTKEY_REMOVE_LAST)
    assert _scene_items() == []
    assert fake.obs_get_source_by_name(script.SOURCE_NAME_IMAGE) is None

    # 履歴が空のときも安全に no-op
    fake.trigger_hotkey(script.HOTKEY_REMOVE_LAST)


def test_remove_last_skips_externally_deleted_sources(obs_script, monkeypatch):
    """手動削除済みのソースが履歴にあっても、直近の実在ペーストを削除できる。"""
    script, _, _ = obs_script
    texts = iter(["最初", "二番目"])
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot(text=next(texts)))
    fake.trigger_hotkey(script.HOTKEY_PASTE)
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    # 2 つめ(履歴の末尾)を OBS 側で外部的に削除する
    newest = _scene_items()[1]
    fake.obs_sceneitem_remove(newest)
    fake.obs_source_remove(newest.source)

    fake.trigger_hotkey(script.HOTKEY_REMOVE_LAST)
    assert _scene_items() == []  # 残っていた 1 つめが削除される


def test_long_text_is_truncated(obs_script, monkeypatch):
    script, _, _ = obs_script
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot(text="あ" * 10000))
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    text = _scene_items()[0].source.settings["text"]
    assert len(text) == script.MAX_TEXT_LENGTH + 1
    assert text.endswith("…")


def test_confirm_paste_called_when_enabled(obs_script, monkeypatch):
    script, settings, _ = obs_script
    fake.obs_data_set_bool(settings, "confirm_before_paste", True)
    script.script_update(settings)

    calls = []
    monkeypatch.setattr(script, "confirm_paste", lambda content: calls.append(content) or True)
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot(text="確認対象"))
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    assert len(calls) == 1  # confirm_paste が実際に呼ばれた
    assert len(_scene_items()) == 1  # OK 相当(True)なので通常どおりペーストされる


def test_confirm_paste_cancel_blocks_paste(obs_script, monkeypatch):
    script, settings, _ = obs_script
    fake.obs_data_set_bool(settings, "confirm_before_paste", True)
    script.script_update(settings)

    monkeypatch.setattr(script, "confirm_paste", lambda content: False)
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot(text="キャンセル対象"))
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    assert _scene_items() == []  # キャンセルされたのでソースは作られない


def test_confirm_paste_skipped_when_disabled(obs_script, monkeypatch):
    script, _, _ = obs_script  # フィクスチャで confirm_before_paste=False 済み

    def fail_if_called(content):
        raise AssertionError("confirm_before_paste=False では confirm_paste は呼ばれない")

    monkeypatch.setattr(script, "confirm_paste", fail_if_called)
    monkeypatch.setattr(script, "read_clipboard", lambda: _snapshot(text="スキップ対象"))
    fake.trigger_hotkey(script.HOTKEY_PASTE)

    assert len(_scene_items()) == 1


def test_default_hotkeys_on_first_load(obs_script):
    """初回ロード時、既定の Ctrl+Shift+V / Ctrl+Shift+Z が割り当てられること。

    obs_hotkey_load() で読み込まれた組み合わせは script_save() 時の
    obs_hotkey_save() に反映される(実 OBS の永続化フロー)ため、
    それを経由して確認する。
    """
    script, settings, _ = obs_script
    script.script_save(settings)

    paste = fake.obs_data_get_array(settings, script.HOTKEY_PASTE)
    assert paste is not None
    assert paste.items == [{"key": "OBS_KEY_V", "control": True, "shift": True}]

    remove = fake.obs_data_get_array(settings, script.HOTKEY_REMOVE_LAST)
    assert remove is not None
    assert remove.items == [{"key": "OBS_KEY_Z", "control": True, "shift": True}]
    assert fake.obs_data_get_bool(settings, f"_{script.HOTKEY_REMOVE_LAST}.default_bound") is True


def test_default_hotkey_not_reapplied_after_manual_clear(obs_script):
    script, settings, _ = obs_script

    # ユーザーが Settings > ホットキー で割り当てを解除した状態を模す(空配列)
    for key in (script.HOTKEY_PASTE, script.HOTKEY_REMOVE_LAST):
        fake.obs_data_set_array(settings, key, fake.obs_data_array_create())

    script.script_load(settings)  # スクリプト再読み込み(OBS 再起動を模す)
    script.script_save(settings)

    for key in (script.HOTKEY_PASTE, script.HOTKEY_REMOVE_LAST):
        saved = fake.obs_data_get_array(settings, key)
        assert fake.obs_data_array_count(saved) == 0  # 既定値が再適用されていない


def test_user_hotkey_binding_is_preserved(obs_script):
    """ユーザーが独自に設定した組み合わせは既定値で上書きされないこと。"""
    script, settings, _ = obs_script

    custom = fake.obs_data_array_create()
    combo = fake.obs_data_create()
    fake.obs_data_set_string(combo, "key", "OBS_KEY_F9")
    fake.obs_data_array_insert(custom, 0, combo)
    fake.obs_data_set_array(settings, script.HOTKEY_PASTE, custom)

    script.script_load(settings)
    script.script_save(settings)

    saved = fake.obs_data_get_array(settings, script.HOTKEY_PASTE)
    assert saved.items == [{"key": "OBS_KEY_F9"}]
