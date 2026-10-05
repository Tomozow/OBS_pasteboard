"""実 OBS を相手にする E2E テストの補助関数群。

戦略: ユーザーの OBS インスタンスには一切触れない。
OBS が停止していることを前提に(起動中ならテストをスキップ)、
E2E 専用シーンコレクションを JSON として用意し、
`obs64 --collection OBS_Pasteboard_E2E` で専用インスタンスを起動する。
これによりユーザーの(重い)シーンコレクションのロード/アンロードが
テスト中に発生せず、サードパーティプラグイン起因のクラッシュを避けられる。
"""

import configparser
import contextlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

OBS_APPDATA = Path(os.environ["APPDATA"]) / "obs-studio"
OBS_BIN_DIR = Path("C:/Program Files/obs-studio/bin/64bit")
OBS_EXE = OBS_BIN_DIR / "obs64.exe"
SCENES_DIR = OBS_APPDATA / "basic" / "scenes"
WEBSOCKET_CONFIG = OBS_APPDATA / "plugin_config" / "obs-websocket" / "config.json"
USER_INI = OBS_APPDATA / "user.ini"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "obs_pasteboard.py"
ARTIFACTS_DIR = Path(__file__).resolve().parent / "_artifacts"

E2E_COLLECTION = "OBS_Pasteboard_E2E"
E2E_CONFIRM_COLLECTION = "OBS_Pasteboard_E2E_Confirm"


def trigger_hotkey(ws, name: str) -> None:
    """TriggerHotkeyByName を contextName なしで送る。

    obsws-python 1.8 の trigger_hot_key_by_name は contextName: null を
    送ってしまい、obs-websocket の型検証に弾かれるため生リクエストを使う。
    """
    ws.send("TriggerHotkeyByName", {"hotkeyName": name})


def load_websocket_config() -> dict | None:
    if not WEBSOCKET_CONFIG.is_file():
        return None
    return json.loads(WEBSOCKET_CONFIG.read_text(encoding="utf-8"))


def _obs_pids() -> list[int]:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = []
    for line in result.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "obs64.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def is_obs_running() -> bool:
    return bool(_obs_pids())


def launch_obs_e2e_instance(
    collection_name: str, extra_args: list[str] | None = None
) -> subprocess.Popen:
    """指定のシーンコレクションを直接指定して OBS を起動する。"""
    return subprocess.Popen(
        [
            str(OBS_EXE),
            "--collection",
            collection_name,
            "--disable-shutdown-check",
            *(extra_args or []),
        ],
        cwd=str(OBS_BIN_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def close_obs_gracefully(timeout: float = 40.0) -> bool:
    """WM_CLOSE で OBS を正常終了させる。成功したら True。

    強制終了(/F)は「異常終了」フラグを残し次回起動時にセーフモード
    ダイアログが出るため、最終手段としてのみ使う。
    """
    pids = _obs_pids()
    if not pids:
        return True
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, check=False)
    try:
        wait_until(lambda: not is_obs_running(), timeout=timeout, desc="OBS の正常終了")
        return True
    except TimeoutError:
        for pid in _obs_pids():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return False


def read_scene_collection_pointer() -> tuple[str, str]:
    """user.ini が指している現在のシーンコレクション (名前, ファイル名) を返す。"""
    parser = configparser.ConfigParser(strict=False)
    parser.read(USER_INI, encoding="utf-8-sig")
    basic = parser["Basic"] if parser.has_section("Basic") else {}
    return basic.get("SceneCollection", ""), basic.get("SceneCollectionFile", "")


def restore_scene_collection_pointer(name: str, file_name: str) -> None:
    """user.ini のシーンコレクション参照を元に戻す(OBS 停止中に呼ぶこと)。

    configparser での全体書き直しは他セクションの体裁を崩すリスクがあるため、
    該当 2 行だけを置換する。
    """
    raw = USER_INI.read_bytes()
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    text = raw[len(bom) :].decode("utf-8")

    lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped) :]
        if stripped.startswith("SceneCollection="):
            lines.append(f"SceneCollection={name}{ending}")
        elif stripped.startswith("SceneCollectionFile="):
            lines.append(f"SceneCollectionFile={file_name}{ending}")
        else:
            lines.append(line)
    USER_INI.write_bytes(bom + "".join(lines).encode("utf-8"))


def wait_until(
    predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.25, desc: str = ""
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError(f"タイムアウト({timeout}s): {desc}")


def _minimal_collection(name: str, scene: str = "シーン") -> dict:
    """OBS が生成するシーンコレクション JSON の最小構成(フォールバック用)。"""
    return {
        "name": name,
        "current_scene": scene,
        "current_program_scene": scene,
        "scene_order": [{"name": scene}],
        "groups": [],
        "transitions": [],
        "current_transition": "フェード",
        "transition_duration": 300,
        "saved_projectors": [],
        "preview_locked": False,
        "scaling_enabled": False,
        "scaling_level": 0,
        "scaling_off_x": 0.0,
        "scaling_off_y": 0.0,
        "virtual-camera": {},
        "modules": {},
        "version": 2,
        "sources": [
            {
                "id": "scene",
                "versioned_id": "scene",
                "name": scene,
                "uuid": str(uuid.uuid4()),
                "settings": {"id_counter": 0, "custom_size": False, "items": []},
                "enabled": True,
                "flags": 0,
                "volume": 1.0,
                "balance": 0.5,
                "sync": 0,
                "mixers": 255,
                "monitoring_type": 0,
                "muted": False,
                "push-to-mute": False,
                "push-to-mute-delay": 0,
                "push-to-talk": False,
                "push-to-talk-delay": 0,
                "hotkeys": {},
                "deinterlace_mode": 0,
                "deinterlace_field_order": 0,
                "private_settings": {},
                "filters": [],
            }
        ],
    }


def ensure_collection(collection_name: str, script_settings: dict) -> None:
    """指定コレクション JSON を用意し、scripts-tool に本スクリプトを登録する。

    OBS 停止中に呼ぶこと(起動中の書き換えは上書きされる/競合する)。
    """
    collection_file = SCENES_DIR / f"{collection_name}.json"
    if collection_file.is_file():
        try:
            data = json.loads(collection_file.read_text(encoding="utf-8"))
        except ValueError:
            data = _minimal_collection(collection_name)
    else:
        data = _minimal_collection(collection_name)

    modules = data.setdefault("modules", {})
    scripts = modules.setdefault("scripts-tool", [])
    script_path = str(SCRIPT_PATH).replace("\\", "/")

    for entry in scripts:
        if Path(entry.get("path", "")).resolve() == SCRIPT_PATH.resolve():
            entry["path"] = script_path
            entry["settings"] = {**entry.get("settings", {}), **script_settings}
            break
    else:
        scripts.append({"path": script_path, "settings": dict(script_settings)})

    collection_file.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


@contextlib.contextmanager
def run_e2e_instance(collection_name: str, script_settings: dict) -> Iterator["object"]:
    """指定のシーンコレクションで E2E 用 OBS インスタンスを起動し、WebSocket クライアントを返す。

    前提が満たされなければ pytest.skip する(未インストール・websocket 無効・
    ユーザーの OBS が起動中)。終了時は OBS を正常終了し、
    user.ini のシーンコレクション参照を元へ戻す。
    """
    if not OBS_EXE.is_file():
        pytest.skip("OBS Studio がインストールされていません")
    config = load_websocket_config()
    if config is None or not config.get("server_enabled"):
        pytest.skip(
            "obs-websocket が有効になっていません(OBS の [ツール] → [WebSocket サーバー設定])"
        )
    if is_obs_running():
        pytest.skip(
            "OBS が起動中です。E2E テストは専用インスタンスを起動するため、"
            "OBS を終了してから再実行してください"
        )

    original_pointer = read_scene_collection_pointer()
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_collection(collection_name, script_settings)

    process = launch_obs_e2e_instance(collection_name)

    import obsws_python

    client = None

    def try_connect() -> bool:
        nonlocal client
        if process.poll() is not None:
            raise RuntimeError(f"OBS が起動直後に終了しました (exit={process.returncode})")
        try:
            client = obsws_python.ReqClient(
                host="127.0.0.1",
                port=config["server_port"],
                password=config["server_password"],
                timeout=15,
            )
            return True
        except Exception:
            return False

    try:
        wait_until(try_connect, timeout=120, interval=1.0, desc="obs-websocket への接続")

        def obs_ready() -> bool:
            # 起動直後は 207 (OBS is not ready) が返るためリトライする
            try:
                client.get_scene_collection_list()
                return True
            except Exception:
                return False

        wait_until(obs_ready, timeout=90, interval=1.0, desc="OBS フロントエンドの初期化")

        current = client.get_scene_collection_list().current_scene_collection_name
        assert current == collection_name, (
            f"E2E コレクションがロードされていません(現在: {current})。"
            "テストを中断します(ユーザーのコレクションを保護するため)"
        )
        yield client
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        close_obs_gracefully()
        if original_pointer[0] and original_pointer[0] != collection_name:
            restore_scene_collection_pointer(*original_pointer)
