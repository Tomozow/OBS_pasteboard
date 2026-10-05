"""obspython の最小インメモリ実装(スクリプトロジックのユニットテスト用)。

実 OBS の API 契約(参照カウント、obs_data の defaults など)を
テストに必要な範囲で模倣する。
"""

LOG_ERROR = 100
LOG_WARNING = 200
LOG_INFO = 300
OBS_PATH_DIRECTORY = 1
OBS_BOUNDS_SCALE_INNER = 2
# 注意: 実際の obspython には OBS_ALIGN_CENTER が存在しない(SWIG が
# #define 定数を書き出さない)。実環境とのパリティのため、ここでも定義しない。

log_messages: list[tuple[int, str]] = []
hotkeys: dict[str, object] = {}
sources: dict[str, "FakeSource"] = {}
_current_scene_source: "FakeSource | None" = None
_hotkey_bindings: dict[int, list[dict]] = {}
_hotkey_ids: dict[str, int] = {}


class FakeData:
    def __init__(self):
        self.values: dict = {}
        self.defaults: dict = {}

    def get(self, key, fallback):
        if key in self.values:
            return self.values[key]
        return self.defaults.get(key, fallback)


class FakeScene:
    def __init__(self, name: str):
        self.name = name
        self.items: list[FakeItem] = []


class FakeItem:
    def __init__(self, source: "FakeSource"):
        self.source = source
        self.bounds_type = None
        self.bounds = None
        self.bounds_alignment = None
        self.alignment = None
        self.pos = None


class FakeSource:
    def __init__(self, kind: str, name: str, settings: dict):
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.scene: FakeScene | None = None
        self.removed = False


class obs_video_info:  # noqa: N801 (OBS API の命名に合わせる)
    base_width = 0
    base_height = 0


class vec2:  # noqa: N801
    def __init__(self):
        self.x = 0.0
        self.y = 0.0


def reset(scene_name: str = "シーン") -> None:
    global _current_scene_source
    log_messages.clear()
    hotkeys.clear()
    sources.clear()
    _hotkey_bindings.clear()
    _hotkey_ids.clear()
    scene = FakeScene(scene_name)
    scene_source = FakeSource("scene", scene_name, {})
    scene_source.scene = scene
    sources[scene_name] = scene_source
    _current_scene_source = scene_source


def trigger_hotkey(name: str) -> None:
    hotkeys[name](True)
    hotkeys[name](False)


# --- obs_data ---------------------------------------------------------------


def obs_data_create():
    return FakeData()


def obs_data_release(data):
    pass


def obs_data_set_string(data, key, value):
    data.values[key] = str(value)


def obs_data_set_bool(data, key, value):
    data.values[key] = bool(value)


def obs_data_set_int(data, key, value):
    data.values[key] = int(value)


def obs_data_set_obj(data, key, obj):
    data.values[key] = dict(obj.values)


def obs_data_get_string(data, key):
    return data.get(key, "")


def obs_data_get_bool(data, key):
    return data.get(key, False)


def obs_data_get_int(data, key):
    return data.get(key, 0)


def obs_data_set_default_string(data, key, value):
    data.defaults[key] = str(value)


def obs_data_set_default_bool(data, key, value):
    data.defaults[key] = bool(value)


def obs_data_set_default_int(data, key, value):
    data.defaults[key] = int(value)


def obs_data_get_array(data, key):
    return data.values.get(key)


def obs_data_set_array(data, key, array):
    data.values[key] = array


def obs_data_array_release(array):
    pass


class FakeDataArray:
    def __init__(self):
        self.items: list[dict] = []


def obs_data_array_create():
    return FakeDataArray()


def obs_data_array_insert(array, index, item):
    array.items.insert(index, dict(item.values))


def obs_data_array_count(array):
    return len(array.items) if array is not None else 0


# --- hotkey -----------------------------------------------------------------


def obs_hotkey_register_frontend(name, description, callback):
    hotkeys[name] = callback
    # 同名の再登録(スクリプト再読み込み)でも ID が安定するようにする
    return _hotkey_ids.setdefault(name, len(_hotkey_ids) + 1)


def obs_hotkey_load(hotkey_id, array):
    _hotkey_bindings[hotkey_id] = list(array.items) if array is not None else []


def obs_hotkey_save(hotkey_id):
    saved = FakeDataArray()
    saved.items = list(_hotkey_bindings.get(hotkey_id, []))
    return saved


# --- properties(UI 定義は no-op)------------------------------------------


def obs_properties_create():
    return object()


def obs_properties_add_path(*args, **kwargs):
    pass


def obs_properties_add_bool(*args, **kwargs):
    pass


def obs_properties_add_int_slider(*args, **kwargs):
    pass


def obs_properties_add_button(*args, **kwargs):
    pass


# --- source / scene ---------------------------------------------------------


def blog(level, message):
    log_messages.append((level, message))


def obs_source_get_display_name(kind):
    if kind == "text_gdiplus_v3":
        return "テキスト (GDI+)"
    return None


def obs_source_create(kind, name, settings, hotkey_data):
    source = FakeSource(kind, name, settings.values if settings else {})
    sources[name] = source
    return source


def obs_source_release(source):
    pass


def obs_source_update(source, settings):
    source.settings.update(settings.values)


def obs_source_remove(source):
    source.removed = True
    sources.pop(source.name, None)


def obs_source_removed(source):
    return source.removed


def obs_source_get_name(source):
    return source.name


def obs_get_source_by_name(name):
    return sources.get(name)


def obs_frontend_get_current_scene():
    return _current_scene_source


def obs_scene_from_source(source):
    return source.scene


def obs_scene_add(scene, source):
    item = FakeItem(source)
    scene.items.append(item)
    return item


def obs_scene_find_source(scene, name):
    for item in scene.items:
        if item.source.name == name:
            return item
    return None


def obs_sceneitem_remove(item):
    for scene_source in sources.values():
        if scene_source.scene and item in scene_source.scene.items:
            scene_source.scene.items.remove(item)


def obs_sceneitem_set_bounds_type(item, bounds_type):
    item.bounds_type = bounds_type


def obs_sceneitem_set_bounds_alignment(item, alignment):
    item.bounds_alignment = alignment


def obs_sceneitem_set_bounds(item, bounds):
    item.bounds = (bounds.x, bounds.y)


def obs_sceneitem_set_alignment(item, alignment):
    item.alignment = alignment


def obs_sceneitem_set_pos(item, pos):
    item.pos = (pos.x, pos.y)


def obs_get_video_info(video_info):
    video_info.base_width = 1920
    video_info.base_height = 1080
    return True
