"""シャットダウン時ソースリーク(`N source(s) were remaining`)の犯人特定ツール。

実コレクションのコピーを専用インスタンス(E2Eと同方式)で起動→正常終了し、
OBSログの残留ソース数を計測する。`--drop-*` オプションでコレクションから
要素を除外した変種を試し、残留が0になる境界を二分探索する。

使い方(本物のOBSを終了してから):
    uv run python tools/diagnose_leak.py                     # 丸ごとコピーで再現確認
    uv run python tools/diagnose_leak.py --drop-scripts      # 登録スクリプト全部外し
    uv run python tools/diagnose_leak.py --drop-script obstweet
    uv run python tools/diagnose_leak.py --drop-kind source-clone --drop-kind source-switcher

本物のOBS設定は変更しない(コレクションはコピー、user.iniのポインタは復元)。
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "e2e"))

import obs_harness as harness

DIAG_COLLECTION = "OBS_Pasteboard_Diag"
LOGS_DIR = harness.OBS_APPDATA / "logs"
CRASHES_DIR = harness.OBS_APPDATA / "crashes"
REMAINING_RE = re.compile(r"(\d+) source\(s\) were remaining")


def load_real_collection() -> tuple[str, dict]:
    """user.ini が指す実コレクション JSON を読み込む(名前, データ)。"""
    name, file_name = harness.read_scene_collection_pointer()
    if not file_name:
        raise SystemExit("user.ini からシーンコレクションを特定できませんでした")
    if not file_name.endswith(".json"):
        file_name += ".json"
    path = harness.SCENES_DIR / file_name
    if not path.is_file():
        raise SystemExit(f"コレクションファイルがありません: {path}")
    return name, json.loads(path.read_text(encoding="utf-8"))


def apply_drops(
    data: dict,
    drop_scripts: bool,
    drop_script_names: list[str],
    drop_kinds: list[str],
    drop_source_names: list[str],
) -> dict:
    """コレクション JSON から指定要素を取り除いた変種を返す(元は変更しない)。"""
    data = json.loads(json.dumps(data))  # deep copy

    modules = data.get("modules", {})
    if drop_scripts:
        modules.pop("scripts-tool", None)
    elif drop_script_names:
        scripts = modules.get("scripts-tool", [])
        modules["scripts-tool"] = [
            s
            for s in scripts
            if not any(pat.lower() in s.get("path", "").lower() for pat in drop_script_names)
        ]

    removed_names: set[str] = set()
    if drop_kinds or drop_source_names:
        kept = []
        for source in data.get("sources", []):
            kind_hit = source.get("id") in drop_kinds or source.get("versioned_id") in drop_kinds
            name_hit = source.get("name") in drop_source_names
            if kind_hit or name_hit:
                removed_names.add(source.get("name", ""))
            else:
                kept.append(source)
        data["sources"] = kept
        # シーン/グループ内の参照(settings.items[].name)も取り除く
        for source in data["sources"]:
            if source.get("id") in ("scene", "group"):
                items = source.get("settings", {}).get("items", [])
                source["settings"]["items"] = [
                    item for item in items if item.get("name") not in removed_names
                ]
    return data


def mutate_switcher_timing(data: dict, interval_ms: int, duration_ms: int) -> dict:
    """source_switcher の切替間隔/トランジション時間を変更した変種を返す。

    トランジション中にシャットダウンが重なる競合を確実に再現するために使う。
    """
    data = json.loads(json.dumps(data))
    for source in data.get("sources", []):
        if source.get("id") == "source_switcher":
            settings = source.setdefault("settings", {})
            settings["time_switch_duration"] = interval_ms
            settings["transition_duration"] = duration_ms
    return data


def write_diag_collection(data: dict) -> None:
    data = dict(data)
    data["name"] = DIAG_COLLECTION
    path = harness.SCENES_DIR / f"{DIAG_COLLECTION}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


def _find_text_source_names(data: dict, limit: int = 3) -> list[str]:
    names = []
    for source in data.get("sources", []):
        if str(source.get("id", "")).startswith("text_") and source.get("name"):
            names.append(source["name"])
        if len(names) >= limit:
            break
    return names


def run_activity(client, data: dict, duration: float) -> None:
    """配信中の操作を模擬する: 録画+字幕テキスト連続更新+シーン切替。

    録画ファイルは tests/e2e/_artifacts に保存する。配信(StartStream)は行わない。
    """
    harness.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        client.send("SetRecordDirectory", {"recordDirectory": str(harness.ARTIFACTS_DIR)})
    except Exception as exc:
        print(f"SetRecordDirectory 失敗(続行): {exc}")
    try:
        client.send("StartRecord", {})
    except Exception as exc:
        print(f"StartRecord 失敗(続行): {exc}")

    scene_names = [
        s["name"] for s in data.get("sources", []) if s.get("id") == "scene"
    ][:3]
    # ゆかコネ字幕系を優先し、なければ適当なテキストソース
    text_names = [
        s.get("name") for s in data.get("sources", [])
        if str(s.get("id", "")).startswith("text_") and "ゆかコネ" in str(s.get("name", ""))
    ] or _find_text_source_names(data)
    print(f"activity: scenes={scene_names} texts={text_names}")

    end = time.monotonic() + duration
    tick = 0
    while time.monotonic() < end:
        for name in text_names:
            try:
                client.send(
                    "SetInputSettings",
                    {"inputName": name, "inputSettings": {"text": f"診断更新 {tick}"},
                     "overlay": True},
                )
            except Exception:
                pass
        if tick % 20 == 10 and scene_names:
            try:
                client.send(
                    "SetCurrentProgramScene",
                    {"sceneName": scene_names[(tick // 20) % len(scene_names)]},
                )
            except Exception:
                pass
        tick += 1
        time.sleep(0.5)

    try:
        client.send("StopRecord", {})
    except Exception:
        pass
    time.sleep(3)


def run_once(
    settle_seconds: float, activity_seconds: float = 0.0, data: dict | None = None
) -> dict:
    """診断インスタンスを1回起動→正常終了し、残留ソース数などを返す。"""
    config = harness.load_websocket_config()
    if config is None or not config.get("server_enabled"):
        raise SystemExit("obs-websocket が無効です")
    if harness.is_obs_running():
        raise SystemExit("OBS が起動中です。終了してから再実行してください")

    logs_before = set(LOGS_DIR.glob("*.txt"))
    crashes_before = set(CRASHES_DIR.glob("*.txt")) if CRASHES_DIR.is_dir() else set()
    original_pointer = harness.read_scene_collection_pointer()

    process = harness.launch_obs_e2e_instance(DIAG_COLLECTION, extra_args=["--verbose"])
    client = None
    started = time.monotonic()
    try:
        import obsws_python

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

        harness.wait_until(try_connect, timeout=180, interval=1.0, desc="obs-websocket への接続")

        def obs_ready() -> bool:
            try:
                current = client.get_scene_collection_list().current_scene_collection_name
                return current == DIAG_COLLECTION
            except Exception:
                return False

        harness.wait_until(obs_ready, timeout=180, interval=1.0, desc="診断コレクションのロード")
        startup = time.monotonic() - started
        if activity_seconds > 0 and data is not None:
            run_activity(client, data, activity_seconds)
        time.sleep(settle_seconds)
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        closed = harness.close_obs_gracefully(timeout=120)
        if original_pointer[0] and original_pointer[0] != DIAG_COLLECTION:
            harness.restore_scene_collection_pointer(*original_pointer)

    time.sleep(2)  # ログのフラッシュ待ち
    new_logs = sorted(set(LOGS_DIR.glob("*.txt")) - logs_before)
    crashes_after = set(CRASHES_DIR.glob("*.txt")) if CRASHES_DIR.is_dir() else set()
    new_crashes = sorted(crashes_after - crashes_before)

    remaining = None
    log_name = None
    late_destroyed: list[str] = []
    if new_logs:
        log_name = new_logs[-1].name
        text = new_logs[-1].read_text(encoding="utf-8", errors="replace")
        match = REMAINING_RE.search(text)
        if match:
            remaining = int(match.group(1))
        elif "Freeing OBS context data" in text:
            remaining = 0
        # --verbose 時: Freeing 以降に破棄されたソース名 = 残留していたソース
        marker = text.find("Freeing OBS context data")
        if marker != -1:
            late_destroyed = re.findall(r"source '([^']+)' destroyed", text[marker:])
    return {
        "remaining": remaining,
        "late_destroyed_sources": late_destroyed,
        "graceful": closed,
        "crash_files": [c.name for c in new_crashes],
        "log": log_name,
        "startup_seconds": round(startup, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop-scripts", action="store_true", help="scripts-tool を丸ごと外す")
    parser.add_argument("--drop-script", action="append", default=[], metavar="SUBSTR",
                        help="パスに SUBSTR を含むスクリプトを外す(複数可)")
    parser.add_argument("--drop-kind", action="append", default=[], metavar="ID",
                        help="この id のソースを外す(例: browser_source, source-clone)")
    parser.add_argument("--drop-source", action="append", default=[], metavar="NAME",
                        help="この名前のソースを外す(複数可)")
    parser.add_argument("--settle", type=float, default=15.0,
                        help="ロード完了後の待機秒数(既定 15)")
    parser.add_argument("--switcher-race", action="store_true",
                        help="source_switcher を切替3秒/遷移2.5秒に変更し、"
                        "トランジション中シャットダウンの競合を高確率で踏ませる")
    parser.add_argument("--activity", type=float, default=0.0, metavar="SECONDS",
                        help="配信相当の模擬操作(録画+字幕テキスト更新+シーン切替)を指定秒実行")
    parser.add_argument("--list-kinds", action="store_true",
                        help="実コレクション内のソース種別と数を表示して終了")
    parser.add_argument("--analyze", metavar="LOGFILE", default=None,
                        help="OBSログファイルを解析し、残留ソース数と(--verbose起動時のみ)"
                        "残留ソース名を表示して終了。'latest' で最新ログ")
    args = parser.parse_args()

    if args.analyze:
        if args.analyze == "latest":
            log_path = max(LOGS_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime)
        else:
            log_path = Path(args.analyze)
        text = log_path.read_text(encoding="utf-8", errors="replace")
        match = REMAINING_RE.search(text)
        marker = text.find("Freeing OBS context data")
        late = re.findall(r"source '([^']+)' destroyed", text[marker:]) if marker != -1 else []
        count = match.group(1) if match else ("0" if marker != -1 else "不明(Freeing未到達)")
        print(f"ログ: {log_path.name}")
        print(f"残留ソース数: {count}")
        if late:
            print("残留していたソース(Freeing以降に破棄):")
            for name in late:
                print(f"  - {name}")
        elif match:
            print("ソース名なし: --verbose 起動でないとソース名は記録されません")
        return

    real_name, data = load_real_collection()

    if args.list_kinds:
        from collections import Counter
        counts = Counter(s.get("id", "?") for s in data.get("sources", []))
        for kind, count in counts.most_common():
            print(f"{count:4d}  {kind}")
        scripts = data.get("modules", {}).get("scripts-tool", [])
        print("\nscripts-tool:")
        for entry in scripts:
            print(f"      {entry.get('path')}")
        return

    variant = apply_drops(
        data, args.drop_scripts, args.drop_script, args.drop_kind, args.drop_source
    )
    if args.switcher_race:
        variant = mutate_switcher_timing(variant, interval_ms=3000, duration_ms=2500)
    write_diag_collection(variant)
    label = []
    if args.drop_scripts:
        label.append("scripts=ALL")
    label += [f"script~{s}" for s in args.drop_script]
    label += [f"kind={k}" for k in args.drop_kind]
    label += [f"source={s}" for s in args.drop_source]
    print(f"実コレクション: {real_name} / 除外: {', '.join(label) or 'なし(丸ごと)'}")

    result = run_once(args.settle, activity_seconds=args.activity, data=variant)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
