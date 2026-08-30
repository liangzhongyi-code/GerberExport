"""
設定檔的讀取與驗證。

parse() 是純函式：吃 dict、吐 Config 或拋 ConfigError，完全不碰檔案系統。
load() 只多做一件事——把檔案讀進來變成 dict。

驗證策略是「嚴格且指名」：
  * 缺欄位、型別錯、值超出範圍，一律拒絕並在訊息裡指名是哪個欄位。
  * **未知欄位也一律拒絕。** 把 timeout_sec 拼成 timout_sec 若被靜默忽略，
    使用者會以為自己調過參數了，而實際上跑的是預設值——這種錯誤在現場
    極難察覺，寧可在啟動時就吵。
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

VALID_FORMATS: Tuple[str, ...] = ("ZIP", "AAMA", "ASTM")
SELECTION_KEYWORD = "SELECTED"
VALID_STRATEGIES: Tuple[str, ...] = ("auto_id", "name", "index")

# 白名單只允許「不會造成覆蓋或資料變更」的處置。
# 允許 Yes/OK 等於把 TD-5 的安全模型整個拆掉——那正是誤觸「是，覆蓋」
# 造成不可回復資料遺失的路徑。
VALID_ACTIONS: Tuple[str, ...] = ("Cancel", "Close", "No")

# 只取樣一次等於完全沒有穩定判定：檔案剛出現就會被當成寫完，
# 於是搬走一個寫到一半的檔案（TD-4 要避免的靜默資料損毀）。
MIN_STABLE_SAMPLES = 2

REQUIRED_CONTROLS: Tuple[str, ...] = (
    "model_list",
    "export_zip",
    "export_aama",
    "export_astm",
    "dialog_path_box",
    "dialog_ok_button",
)

_ENV_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


class ConfigError(ValueError):
    """設定有問題。訊息一律指名出錯的欄位。"""


# ── 結構 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Paths:
    temp_dir: Path
    output_root: Path


@dataclass(frozen=True)
class Detection:
    poll_interval_ms: int
    stable_samples: int
    timeout_sec: int
    verify_exclusive_lock: bool


@dataclass(frozen=True)
class Archival:
    add_format_suffix: bool
    output_dir_pattern: str


@dataclass(frozen=True)
class DialogRule:
    title_like: str
    action: str
    result_status: str


@dataclass(frozen=True)
class Control:
    strategy: str
    value: str


@dataclass(frozen=True)
class Config:
    models: Tuple[str, ...]
    is_selection_mode: bool
    formats: Tuple[str, ...]
    paths: Paths
    detection: Detection
    archival: Archival
    dialog_whitelist: Tuple[DialogRule, ...]
    controls: Mapping[str, Control]

    @property
    def controls_ready(self) -> bool:
        """期一探測完成、六個控制項都填好了才算就緒。"""
        return all(c.value for c in self.controls.values())


# ── 驗證小工具 ───────────────────────────────────────────────────────


def _where(section: str, key: str) -> str:
    return f"{section}.{key}" if section else key


def _require(data: Mapping[str, Any], key: str, section: str = "") -> Any:
    if key not in data:
        raise ConfigError(f"設定缺少必填欄位：{_where(section, key)}")
    return data[key]


def _reject_unknown(data: Mapping[str, Any], allowed, section: str = "") -> None:
    # JSON 沒有註解語法，所以用 "_" 開頭的鍵當說明欄位。
    # 拼錯保護不受影響——拼錯的欄位不會剛好以底線開頭。
    extra = sorted(k for k in data if k not in allowed and not k.startswith("_"))
    if extra:
        names = "、".join(extra)
        raise ConfigError(
            f"設定含無法辨識的欄位：{names}"
            + (f"（位於 {section}）" if section else "")
            + "。是不是拼錯了？"
        )


def _as_int(value: Any, key: str, section: str, minimum: int) -> int:
    where = _where(section, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} 必須是整數，目前是 {type(value).__name__}")
    if value < minimum:
        raise ConfigError(f"{where} 必須至少為 {minimum}，目前是 {value}")
    return value


def _as_bool(value: Any, key: str, section: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"{_where(section, key)} 必須是 true 或 false，"
            f"目前是 {value!r}"
        )
    return value


def _as_str(value: Any, key: str, section: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{_where(section, key)} 必須是字串")
    return value


def _as_mapping(value: Any, key: str, section: str = "") -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{_where(section, key)} 必須是物件")
    return value


def expand_path(raw: str, field: str) -> Path:
    """
    展開 %VAR% 與 ~ 之後回傳 Path。

    留著沒展開的 %VAR% 會變成字面上的資料夾名，讓輸出安靜地建在
    奇怪的地方——所以展不開就直接拒絕。
    """
    expanded = os.path.expandvars(os.path.expanduser(raw))
    leftover = _ENV_VAR.search(expanded)
    if leftover:
        raise ConfigError(
            f"{field} 裡的環境變數 %{leftover.group(1)}% 展不開，"
            "請改成完整路徑或確認該變數存在"
        )
    return Path(expanded)


# ── 各區段 ───────────────────────────────────────────────────────────


def _parse_models(raw: Any) -> Tuple[Tuple[str, ...], bool]:
    if raw == SELECTION_KEYWORD:
        return (), True
    if isinstance(raw, str):
        raise ConfigError(
            f'models 若為字串，只能是 "{SELECTION_KEYWORD}"，目前是 {raw!r}'
        )
    if not isinstance(raw, list):
        raise ConfigError(
            f'models 必須是 "{SELECTION_KEYWORD}" 或 model 名稱清單'
        )
    if not raw:
        raise ConfigError(
            "models 是空清單，這樣沒有任何東西可以處理。"
            f'若要改用 Explorer 的選取項，請填 "{SELECTION_KEYWORD}"'
        )
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"models 含非字串或空白項目：{item!r}")
    return tuple(raw), False


def _parse_formats(raw: Any) -> Tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("formats 必須是非空清單")
    unknown = [f for f in raw if f not in VALID_FORMATS]
    if unknown:
        raise ConfigError(
            f"formats 含不支援的格式：{unknown}，只接受 {list(VALID_FORMATS)}"
        )
    if len(set(raw)) != len(raw):
        raise ConfigError(
            "formats 有重複項目。同一個 model 匯出兩次同格式，"
            "第二次必然撞檔名"
        )
    return tuple(raw)


def _parse_paths(raw: Any) -> Paths:
    data = _as_mapping(raw, "paths")
    _reject_unknown(data, ("temp_dir", "output_root"), "paths")
    temp = expand_path(
        _as_str(_require(data, "temp_dir", "paths"), "temp_dir", "paths"),
        "paths.temp_dir",
    )
    out = expand_path(
        _as_str(_require(data, "output_root", "paths"), "output_root", "paths"),
        "paths.output_root",
    )
    # 暫存夾會被反覆清空；與輸出根目錄相同等於邊產出邊刪掉。
    if temp.resolve() == out.resolve():
        raise ConfigError(
            "paths.temp_dir 不能與 paths.output_root 相同——"
            "暫存夾會被反覆清空，這樣會刪掉已歸檔的產出"
        )
    return Paths(temp_dir=temp, output_root=out)


def _parse_detection(raw: Any) -> Detection:
    data = _as_mapping(raw, "detection")
    allowed = (
        "poll_interval_ms",
        "stable_samples",
        "timeout_sec",
        "verify_exclusive_lock",
    )
    _reject_unknown(data, allowed, "detection")
    return Detection(
        poll_interval_ms=_as_int(
            _require(data, "poll_interval_ms", "detection"),
            "poll_interval_ms",
            "detection",
            minimum=1,
        ),
        stable_samples=_as_int(
            _require(data, "stable_samples", "detection"),
            "stable_samples",
            "detection",
            minimum=MIN_STABLE_SAMPLES,
        ),
        timeout_sec=_as_int(
            _require(data, "timeout_sec", "detection"),
            "timeout_sec",
            "detection",
            minimum=1,
        ),
        verify_exclusive_lock=_as_bool(
            data.get("verify_exclusive_lock", False),
            "verify_exclusive_lock",
            "detection",
        ),
    )


def _parse_archival(raw: Any) -> Archival:
    data = _as_mapping(raw, "archival")
    _reject_unknown(data, ("add_format_suffix", "output_dir_pattern"), "archival")
    return Archival(
        add_format_suffix=_as_bool(
            _require(data, "add_format_suffix", "archival"),
            "add_format_suffix",
            "archival",
        ),
        output_dir_pattern=_as_str(
            _require(data, "output_dir_pattern", "archival"),
            "output_dir_pattern",
            "archival",
        ),
    )


def _parse_whitelist(raw: Any) -> Tuple[DialogRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("dialog_whitelist 必須是清單")
    rules = []
    for i, item in enumerate(raw):
        where = f"dialog_whitelist[{i}]"
        data = _as_mapping(item, str(i), "dialog_whitelist")
        _reject_unknown(data, ("title_like", "action", "result_status"), where)
        action = _as_str(_require(data, "action", where), "action", where)
        if action not in VALID_ACTIONS:
            raise ConfigError(
                f"{where}.action 只接受 {list(VALID_ACTIONS)}，目前是 {action!r}。"
                "允許按下「是／確定」等於拆掉白名單的安全模型"
            )
        rules.append(
            DialogRule(
                title_like=_as_str(
                    _require(data, "title_like", where), "title_like", where
                ),
                action=action,
                result_status=_as_str(
                    _require(data, "result_status", where), "result_status", where
                ),
            )
        )
    return tuple(rules)


def _parse_controls(raw: Any) -> Mapping[str, Control]:
    data = _as_mapping(raw, "controls")
    _reject_unknown(data, REQUIRED_CONTROLS, "controls")
    controls: Dict[str, Control] = {}
    for name in REQUIRED_CONTROLS:
        where = f"controls.{name}"
        entry = _as_mapping(_require(data, name, "controls"), name, "controls")
        _reject_unknown(entry, ("strategy", "value"), where)
        strategy = _as_str(_require(entry, "strategy", where), "strategy", where)
        if strategy not in VALID_STRATEGIES:
            raise ConfigError(
                f"{where}.strategy 只接受 {list(VALID_STRATEGIES)}，"
                f"目前是 {strategy!r}"
            )
        # value 在期一探測完成前是空字串，這在解析階段合法。
        controls[name] = Control(
            strategy=strategy,
            value=_as_str(entry.get("value", ""), "value", where),
        )
    return MappingProxyType(controls)


# ── 入口 ─────────────────────────────────────────────────────────────


def parse(data: Mapping[str, Any]) -> Config:
    """把 dict 驗證並轉成 Config。純函式，不修改輸入。"""
    if not isinstance(data, dict):
        raise ConfigError("設定必須是一個 JSON 物件")

    allowed = (
        "models",
        "formats",
        "paths",
        "detection",
        "archival",
        "dialog_whitelist",
        "controls",
    )
    _reject_unknown(data, allowed)

    models, selection_mode = _parse_models(_require(data, "models"))
    return Config(
        models=models,
        is_selection_mode=selection_mode,
        formats=_parse_formats(_require(data, "formats")),
        paths=_parse_paths(_require(data, "paths")),
        detection=_parse_detection(_require(data, "detection")),
        archival=_parse_archival(_require(data, "archival")),
        dialog_whitelist=_parse_whitelist(data.get("dialog_whitelist")),
        controls=_parse_controls(_require(data, "controls")),
    )


def load(path: Path) -> Config:
    """從檔案讀取設定。JSON 語法錯誤也轉成 ConfigError，訊息含行號。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"讀不到設定檔 {path}：{exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"設定檔 {path} 不是合法的 JSON："
            f"第 {exc.lineno} 行第 {exc.colno} 欄 — {exc.msg}"
        ) from exc
    return parse(data)
