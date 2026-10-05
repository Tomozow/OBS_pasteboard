"""OBS Pasteboard — クリップボードの画像・テキストを OBS へ直接ペーストするスクリプト。

インストール: OBS の [ツール] → [スクリプト] でこのファイルを追加し、
[設定] → [ホットキー] で「クリップボードをペースト」にキーを割り当てる。

- 画像(スクリーンショット、ブラウザでコピーした画像、コピーした画像ファイル)は
  PNG として保存され、image_source として現在のシーン中央に配置される。
- テキストは text_gdiplus ソースとして配置される。
- 初回起動時の既定ホットキー: ペースト = Ctrl+Shift+V、
  最後のペーストを削除 = Ctrl+Shift+Z(変更・解除は [設定] → [ホットキー] から可能)。
- 既定でペースト前に内容確認のダイアログを表示する(設定でスキップ可能)。
"""

import os
import sys
import traceback

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def _reload_core() -> None:
    """OBS のスクリプト再読み込み時に pasteboard_core の旧キャッシュを破棄する。"""
    import importlib

    # 再読み込み前に旧 confirm_dialog の Win32 資源(ウィンドウクラス登録・GDI
    # オブジェクト)を解放する。怠ると登録済みクラスの wndproc が旧モジュールの
    # 解放済み ctypes トランポリンを指したままになりクラッシュする。
    old_dialog = sys.modules.get("pasteboard_core.confirm_dialog")
    cleanup = getattr(old_dialog, "cleanup", None)
    if cleanup is not None:
        try:
            cleanup()
        except Exception:
            traceback.print_exc()

    core_modules = (
        "names", "png_codec", "dib", "imaging", "clipboard_win", "content", "confirm_dialog"
    )
    for name in core_modules:
        module = sys.modules.get(f"pasteboard_core.{name}")
        if module is not None:
            importlib.reload(module)
    package = sys.modules.get("pasteboard_core")
    if package is not None:
        importlib.reload(package)


_reload_core()

import obspython as obs  # noqa: E402  (OBS 内でのみ存在するモジュール)

from pasteboard_core.clipboard_win import read_clipboard  # noqa: E402
from pasteboard_core.confirm_dialog import confirm_paste  # noqa: E402
from pasteboard_core.content import (  # noqa: E402
    ImageBytesContent,
    ImageFileContent,
    PasteContent,
    TextContent,
    make_paste_filename,
    select_content,
)
from pasteboard_core.names import (  # noqa: E402
    HOTKEY_PASTE,
    HOTKEY_REMOVE_LAST,
    SOURCE_NAME_IMAGE,
    SOURCE_NAME_TEXT,
)

MAX_TEXT_LENGTH = 5000

_TEXT_SOURCE_KINDS = ("text_gdiplus_v3", "text_gdiplus_v2", "text_gdiplus", "text_ft2_source_v2")

# obspython は #define 由来の定数(OBS_ALIGN_*)を持たないことがあるため
# ABI 固定値でフォールバックする(obs-defs.h: OBS_ALIGN_CENTER = 0)
_ALIGN_CENTER = getattr(obs, "OBS_ALIGN_CENTER", 0)

_settings = {
    "save_dir": "",
    "reuse_source": False,
    "prefer_image": True,
    "fit_percent": 60,
    "confirm_before_paste": True,
}
_hotkey_paste_id = None
_hotkey_remove_last_id = None
_paste_history: list[dict] = []  # [{"source": name, "scene": name}]


def _log(message: str) -> None:
    obs.blog(obs.LOG_INFO, f"[obs-pasteboard] {message}")


def _log_error(message: str) -> None:
    obs.blog(obs.LOG_WARNING, f"[obs-pasteboard] {message}")


def _default_save_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Pictures", "OBS_Pasteboard")


def _text_source_kind() -> str | None:
    for kind in _TEXT_SOURCE_KINDS:
        if obs.obs_source_get_display_name(kind):
            return kind
    return None


def _unique_source_name(base: str) -> str:
    name = base
    suffix = 1
    while True:
        existing = obs.obs_get_source_by_name(name)
        if existing is None:
            return name
        obs.obs_source_release(existing)
        suffix += 1
        name = f"{base} ({suffix})"


def _save_image_bytes(content: ImageBytesContent) -> str:
    save_dir = _settings["save_dir"] or _default_save_dir()
    try:
        os.makedirs(save_dir, exist_ok=True)
    except OSError:
        save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pasted_images")
        os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, make_paste_filename(content.extension))
    with open(path, "wb") as file:
        file.write(content.data)
    return path


def _center_scene_item(item) -> None:
    """シーンアイテムをキャンバス中央に、指定比率の枠内へ収める。

    配置は装飾的な処理なので、失敗してもペースト自体は成立させる。
    """
    try:
        video_info = obs.obs_video_info()
        if not obs.obs_get_video_info(video_info):
            return
        fit = max(10, min(100, int(_settings["fit_percent"]))) / 100.0

        bounds = obs.vec2()
        bounds.x = video_info.base_width * fit
        bounds.y = video_info.base_height * fit
        position = obs.vec2()
        position.x = video_info.base_width / 2
        position.y = video_info.base_height / 2

        obs.obs_sceneitem_set_bounds_type(item, obs.OBS_BOUNDS_SCALE_INNER)
        obs.obs_sceneitem_set_bounds_alignment(item, _ALIGN_CENTER)
        obs.obs_sceneitem_set_bounds(item, bounds)
        obs.obs_sceneitem_set_alignment(item, _ALIGN_CENTER)
        obs.obs_sceneitem_set_pos(item, position)
    except Exception:
        _log_error("中央配置に失敗しました(ペースト自体は成立):\n" + traceback.format_exc())


def _place_source(kind: str, base_name: str, source_settings) -> None:
    """現在のシーンにソースを作成(または再利用)して配置する。"""
    scene_source = obs.obs_frontend_get_current_scene()
    if scene_source is None:
        _log_error("現在のシーンが取得できませんでした")
        return
    try:
        scene = obs.obs_scene_from_source(scene_source)
        scene_name = obs.obs_source_get_name(scene_source)

        if _settings["reuse_source"]:
            existing = obs.obs_get_source_by_name(base_name)
            if existing is not None:
                try:
                    obs.obs_source_update(existing, source_settings)
                    if obs.obs_scene_find_source(scene, base_name) is None:
                        item = obs.obs_scene_add(scene, existing)
                        _center_scene_item(item)
                finally:
                    obs.obs_source_release(existing)
                _log(f"既存ソースを更新しました: {base_name}")
                return
            source_name = base_name
        else:
            source_name = _unique_source_name(base_name)

        source = obs.obs_source_create(kind, source_name, source_settings, None)
        if source is None:
            _log_error(f"ソースを作成できませんでした: {kind}")
            return
        try:
            item = obs.obs_scene_add(scene, source)
            if item is not None:
                _center_scene_item(item)
        finally:
            obs.obs_source_release(source)

        _paste_history.append({"source": source_name, "scene": scene_name})
        _log(f"ペーストしました: {source_name} → シーン「{scene_name}」")
    finally:
        obs.obs_source_release(scene_source)


def _paste_image_file(path: str) -> None:
    source_settings = obs.obs_data_create()
    try:
        obs.obs_data_set_string(source_settings, "file", path)
        obs.obs_data_set_bool(source_settings, "unload", True)
        _place_source("image_source", SOURCE_NAME_IMAGE, source_settings)
    finally:
        obs.obs_data_release(source_settings)


def _paste_text(text: str) -> None:
    kind = _text_source_kind()
    if kind is None:
        _log_error("テキストソース(text_gdiplus)が見つかりません")
        return
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH] + "…"

    source_settings = obs.obs_data_create()
    font = obs.obs_data_create()
    try:
        obs.obs_data_set_string(font, "face", "Yu Gothic UI")
        obs.obs_data_set_string(font, "style", "Regular")
        obs.obs_data_set_int(font, "size", 96)
        obs.obs_data_set_obj(source_settings, "font", font)
        obs.obs_data_set_string(source_settings, "text", text)
        obs.obs_data_set_bool(source_settings, "outline", True)
        _place_source(kind, SOURCE_NAME_TEXT, source_settings)
    finally:
        obs.obs_data_release(font)
        obs.obs_data_release(source_settings)


def _dispatch(content: PasteContent) -> None:
    if isinstance(content, ImageBytesContent):
        path = _save_image_bytes(content)
        _log(f"クリップボード画像を保存しました: {path}")
        _paste_image_file(path)
    elif isinstance(content, ImageFileContent):
        _paste_image_file(content.path)
    elif isinstance(content, TextContent):
        _paste_text(content.text)


def paste_from_clipboard() -> None:
    """クリップボードを読み取り、内容に応じたソースを現在のシーンへ追加する。"""
    try:
        snapshot = read_clipboard()
        if snapshot.is_empty:
            _log("クリップボードが空です")
            return
        content = select_content(snapshot, prefer_image=_settings["prefer_image"])
        if content is None:
            _log("ペースト可能な内容がありません(画像・テキスト以外)")
            return
        if _settings["confirm_before_paste"] and not confirm_paste(content):
            _log("ユーザーがペーストをキャンセルしました")
            return
        _dispatch(content)
    except Exception:
        _log_error("ペースト処理でエラーが発生しました:\n" + traceback.format_exc())


def remove_last_paste() -> None:
    """直前にペーストしたソースをシーンから外し、入力自体も削除する。

    ユーザーが手動で削除済みのソースは履歴から読み飛ばし、
    実在する直近のペーストを削除する。
    """
    try:
        while _paste_history:
            entry = _paste_history.pop()
            source = obs.obs_get_source_by_name(entry["source"])
            if source is None:
                continue  # 既に存在しない(手動削除など)→ さらに前の履歴へ
            if obs.obs_source_removed(source):
                obs.obs_source_release(source)
                continue  # 削除処理中の名残
            scene_source = obs.obs_get_source_by_name(entry["scene"])
            if scene_source is not None:
                try:
                    scene = obs.obs_scene_from_source(scene_source)
                    item = obs.obs_scene_find_source(scene, entry["source"])
                    if item is not None:
                        obs.obs_sceneitem_remove(item)
                finally:
                    obs.obs_source_release(scene_source)
            try:
                obs.obs_source_remove(source)
            finally:
                obs.obs_source_release(source)
            _log(f"削除しました: {entry['source']}")
            return
        _log("削除対象のペーストがありません")
    except Exception:
        _log_error("削除処理でエラーが発生しました:\n" + traceback.format_exc())


def _on_paste_hotkey(pressed: bool) -> None:
    if pressed:
        paste_from_clipboard()


def _on_remove_hotkey(pressed: bool) -> None:
    if pressed:
        remove_last_paste()


# --- OBS スクリプト API ---------------------------------------------------


def script_description() -> str:
    return (
        "<b>OBS Pasteboard</b><br>"
        "クリップボードの画像・テキストを現在のシーンに直接ペーストします。<br>"
        "既定ホットキー: ペースト = Ctrl+Shift+V、最後のペーストを削除 = Ctrl+Shift+Z"
        "([設定] → [ホットキー] から変更・解除できます)。<br>"
        "既定でペースト前に確認ダイアログを表示します(下の設定でスキップできます)。"
    )


def script_defaults(settings) -> None:
    obs.obs_data_set_default_string(settings, "save_dir", _default_save_dir())
    obs.obs_data_set_default_bool(settings, "reuse_source", False)
    obs.obs_data_set_default_bool(settings, "prefer_image", True)
    obs.obs_data_set_default_int(settings, "fit_percent", 60)
    obs.obs_data_set_default_bool(settings, "confirm_before_paste", True)


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_path(
        props, "save_dir", "画像の保存先", obs.OBS_PATH_DIRECTORY, "", _default_save_dir()
    )
    obs.obs_properties_add_bool(props, "reuse_source", "毎回同じソースを上書きする")
    obs.obs_properties_add_bool(props, "prefer_image", "画像とテキスト両方がある場合は画像を優先")
    obs.obs_properties_add_bool(
        props, "confirm_before_paste", "貼り付け前に確認ダイアログを表示する"
    )
    obs.obs_properties_add_int_slider(
        props, "fit_percent", "配置サイズ(キャンバス比 %)", 10, 100, 5
    )
    obs.obs_properties_add_button(props, "paste_now", "今すぐペースト", _on_paste_button)
    obs.obs_properties_add_button(props, "remove_last", "最後のペーストを削除", _on_remove_button)
    return props


def _on_paste_button(props, prop) -> bool:
    paste_from_clipboard()
    return False


def _on_remove_button(props, prop) -> bool:
    remove_last_paste()
    return False


def script_update(settings) -> None:
    _settings["save_dir"] = obs.obs_data_get_string(settings, "save_dir")
    _settings["reuse_source"] = obs.obs_data_get_bool(settings, "reuse_source")
    _settings["prefer_image"] = obs.obs_data_get_bool(settings, "prefer_image")
    _settings["fit_percent"] = obs.obs_data_get_int(settings, "fit_percent")
    _settings["confirm_before_paste"] = obs.obs_data_get_bool(settings, "confirm_before_paste")


def _default_hotkey_combo(key_name: str, *, control: bool = False, shift: bool = False):
    """指定キーの既定ホットキー組み合わせを表す obs_data_array を 1 件生成する。"""
    combo = obs.obs_data_create()
    array = obs.obs_data_array_create()
    try:
        obs.obs_data_set_string(combo, "key", key_name)
        if control:
            obs.obs_data_set_bool(combo, "control", True)
        if shift:
            obs.obs_data_set_bool(combo, "shift", True)
        obs.obs_data_array_insert(array, 0, combo)
    finally:
        obs.obs_data_release(combo)
    return array


def _load_hotkey_with_default(settings, hotkey_id, key: str, default_array_factory=None) -> None:
    """保存済みの組み合わせを読み込む。未設定(初回)なら既定値を一度だけ割り当てる。

    ユーザーが後から手動で解除した場合は再度既定値を割り当てない
    (フラグ済みなら空配列のままにする)。
    """
    saved = obs.obs_data_get_array(settings, key)
    if default_array_factory is not None:
        flag_key = f"_{key}.default_bound"
        is_unset = saved is None or obs.obs_data_array_count(saved) == 0
        if is_unset and not obs.obs_data_get_bool(settings, flag_key):
            if saved is not None:
                obs.obs_data_array_release(saved)
            saved = default_array_factory()
            obs.obs_data_set_bool(settings, flag_key, True)
    obs.obs_hotkey_load(hotkey_id, saved)
    if saved is not None:
        obs.obs_data_array_release(saved)


def script_load(settings) -> None:
    global _hotkey_paste_id, _hotkey_remove_last_id
    _hotkey_paste_id = obs.obs_hotkey_register_frontend(
        HOTKEY_PASTE, "クリップボードをペースト (OBS Pasteboard)", _on_paste_hotkey
    )
    _hotkey_remove_last_id = obs.obs_hotkey_register_frontend(
        HOTKEY_REMOVE_LAST, "最後のペーストを削除 (OBS Pasteboard)", _on_remove_hotkey
    )
    _load_hotkey_with_default(
        settings,
        _hotkey_paste_id,
        HOTKEY_PASTE,
        default_array_factory=lambda: _default_hotkey_combo(
            "OBS_KEY_V", control=True, shift=True
        ),
    )
    _load_hotkey_with_default(
        settings,
        _hotkey_remove_last_id,
        HOTKEY_REMOVE_LAST,
        default_array_factory=lambda: _default_hotkey_combo(
            "OBS_KEY_Z", control=True, shift=True
        ),
    )
    _log("スクリプトを読み込みました")


def script_save(settings) -> None:
    hotkey_pairs = ((_hotkey_paste_id, HOTKEY_PASTE), (_hotkey_remove_last_id, HOTKEY_REMOVE_LAST))
    for hotkey_id, key in hotkey_pairs:
        if hotkey_id is not None:
            saved = obs.obs_hotkey_save(hotkey_id)
            obs.obs_data_set_array(settings, key, saved)
            obs.obs_data_array_release(saved)


def script_unload() -> None:
    """Win32 資源(ウィンドウクラス登録・GDI オブジェクト)を解放する。

    アンロード中に OBS API へは触らない(シャットダウン時にも呼ばれるため)。
    """
    try:
        from pasteboard_core import confirm_dialog

        confirm_dialog.cleanup()
    except Exception:
        traceback.print_exc()
