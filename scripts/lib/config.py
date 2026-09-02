"""
設定檔的讀取與驗證（schema 依 design.md §4.1）。

parse() 是純函式：吃 dict、吐 Config 或拋 ConfigError，完全不碰檔案系統。
load() 只多做一件事——把檔案讀進來變成 dict。

驗證策略是「嚴格且指名」：
  * 缺欄位、型別錯、值超出範圍，一律拒絕並在訊息裡指名是哪個欄位。
  * **未知欄位也一律拒絕。** 把 timeout_sec 拼成 timout_sec 若被靜默忽略，
    使用者會以為自己調過參數了，而實際上跑的是預設值——這種錯誤在現場
    極難察覺，寧可在啟動時就吵。
  * 所有欄位皆必填，不設預設值。設定檔是交付物的一部分，範本本來就會
    把每個欄位寫齊；「少一個就吵」比「少一個就悄悄用預設」安全得多。

這一層只 import 標準庫與 reporting（拿狀態名稱對照）；reporting 不會反過來
import 這裡，所以沒有循環。
"""

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple, Union

from .reporting import Status

VALID_FORMATS: Tuple[str, ...] = ("ZIP", "AAMA", "ASTM")
ZIP_FORMAT = "ZIP"
SELECTION_KEYWORD = "SELECTED"

# 定位策略。前三個與 uia.py 的 STRATEGY_* 逐字相同（使用者照探測報告抄）；
# title_re／control_type 是 TD-10 預填用的——那是官方文件與標準 Windows
# 對話框能給的東西。
VALID_STRATEGIES: Tuple[str, ...] = ("name", "auto_id", "title_re", "control_type", "index")
STRATEGY_INDEX = "index"
STRATEGY_TITLE_RE = "title_re"

# 白名單只允許「不會造成覆蓋或資料變更」的處置。
# 允許 Yes/OK 等於把 TD-5 的安全模型整個拆掉——那正是誤觸「是，覆蓋」
# 造成不可回復資料遺失的路徑。
VALID_ACTIONS: Tuple[str, ...] = ("Cancel", "Close", "No")

# 只取樣一次等於完全沒有穩定判定：檔案剛出現就會被當成寫完，
# 於是搬走一個寫到一半的檔案（TD-4 要避免的靜默資料損毀）。
MIN_STABLE_SAMPLES = 2

# DXF 完成偵測的兩種模式（TD-4）：files 靠預期檔案數、results_text 靠
# DCU 的 Results 窗格。預設 files，因為 Results 能否讀到要 dry-run 才知道。
VALID_COMPLETION_MODES: Tuple[str, ...] = ("files", "results_text")

# 兩個視窗各自的控制項名單（§4.1）。主流程按名字取，名單即介面。
EXPLORER_CONTROLS: Tuple[str, ...] = (
    "window",
    "model_list",
    "menu_file",
    "menu_export_zip",
    "export_to_dialog",
    "export_to_path",
    "export_to_ok",
    "export_screen_ok",
)
DCU_CONTROLS: Tuple[str, ...] = (
    "window",
    "file_type",
    "source_list",
    "destination_path",
    "run_button",
    "results",
)
CONTROL_GROUPS: Mapping[str, Tuple[str, ...]] = MappingProxyType(
    {"explorer": EXPLORER_CONTROLS, "dcu": DCU_CONTROLS}
)
WINDOW_CONTROL = "window"

# result_status 的合法值就是 reporting.Status 的名稱。白名單命中後主流程
# 會拿它去查 Status，查不到會在對話框已經被按掉之後才炸 KeyError。
STATUS_NAMES: Tuple[str, ...] = tuple(s.name for s in Status)

_ENV_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_WILDCARDS = str.maketrans("", "", "*?")


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
    quiet_period_sec: float
    timeout_sec: int


@dataclass(frozen=True)
class CompleteDialog:
    """ZIP 任務結束時跳出的對話框。OK 只按在它身上（TD-4）。"""

    title_like: str
    ok_button: str


@dataclass(frozen=True)
class ZipSettings:
    complete_dialog: CompleteDialog


@dataclass(frozen=True)
class DxfSettings:
    completion: str
    file_type_labels: Mapping[str, str]


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
class ControlSpec:
    """
    一個控制項怎麼找。index 的 value 是 int，其餘是非空字串。
    """

    strategy: str
    value: Union[str, int]


@dataclass(frozen=True)
class Controls:
    """兩個視窗各一組（TD-9／TD-10）。"""

    explorer: Mapping[str, ControlSpec]
    dcu: Mapping[str, ControlSpec]


@dataclass(frozen=True)
class Config:
    models: Tuple[str, ...]
    is_selection_mode: bool
    formats: Tuple[str, ...]
    paths: Paths
    expected_outputs: Mapping[str, Tuple[str, ...]]
    detection: Detection
    zip: ZipSettings
    dxf: DxfSettings
    archival: Archival
    dialog_whitelist: Tuple[DialogRule, ...]
    controls: Controls

    @property
    def dxf_formats(self) -> Tuple[str, ...]:
        """走 DCU 的格式：formats 扣掉 ZIP。下游分流靠這個，不要自己再算。"""
        return _dxf_formats(self.formats)


def _dxf_formats(formats: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(f for f in formats if f != ZIP_FORMAT)


# ── 驗證小工具 ───────────────────────────────────────────────────────


def _where(section: str, key: str) -> str:
    return f"{section}.{key}" if section else key


def _require(data: Mapping[str, Any], key: str, section: str = "") -> Any:
    if key not in data:
        raise ConfigError(f"設定缺少必填欄位：{_where(section, key)}")
    return data[key]


def _extra_keys(data: Mapping[str, Any], allowed) -> list:
    # JSON 沒有註解語法，所以用 "_" 開頭的鍵當說明欄位。
    # 拼錯保護不受影響——拼錯的欄位不會剛好以底線開頭。
    return sorted(k for k in data if k not in allowed and not k.startswith("_"))


def _reject_unknown(data: Mapping[str, Any], allowed, section: str = "") -> None:
    extra = _extra_keys(data, allowed)
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


def _as_number(value: Any, key: str, section: str, minimum: float) -> float:
    """整數或浮點都收（JSON 裡 1 與 1.0 都合法），回傳 float。"""
    where = _where(section, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必須是數字，目前是 {type(value).__name__}")
    if not math.isfinite(value):
        raise ConfigError(f"{where} 必須是有限的數字，目前是 {value!r}")
    if value < minimum:
        raise ConfigError(f"{where} 必須至少為 {minimum}，目前是 {value}")
    return float(value)


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


def _as_nonempty_str(value: Any, key: str, section: str) -> str:
    text = _as_str(value, key, section)
    if not text.strip():
        raise ConfigError(f"{_where(section, key)} 不能是空字串")
    return text


def _as_glob_title(value: Any, key: str, section: str, consequence: str) -> str:
    """
    視窗標題的 glob 樣式：非空，且不能只有萬用字元。

    `*` 會匹配任何視窗。用在完成對話框上，OK 會按在第一個冒出來的東西上；
    用在白名單上，真正該停下來讓人看的對話框會被安靜地按掉——兩者都是
    TD-5 要杜絕的路徑，所以在設定階段就擋，不等到目標機。
    """
    text = _as_nonempty_str(value, key, section)
    if not text.translate(_WILDCARDS).strip():
        raise ConfigError(
            f"{_where(section, key)} 不能只有萬用字元（目前是 {text!r}）——{consequence}"
        )
    return text


def _as_mapping(value: Any, key: str, section: str = "") -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{_where(section, key)} 必須是物件")
    return value


def _as_choice(value: Any, key: str, section: str, choices: Tuple[str, ...], why: str = "") -> str:
    where = _where(section, key)
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(
            f"{where} 只接受 {list(choices)}，目前是 {value!r}" + (f"。{why}" if why else "")
        )
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


def _canonical(path: Path) -> str:
    """
    拿來比對用的正規形式：絕對路徑、去掉 ..、統一斜線與大小寫。

    刻意用 abspath 而不是 resolve：abspath 純粹是字串運算，目錄還沒建也
    判得出來，也不會被符號連結帶去別的地方。normcase 在 Windows 上會統一
    斜線方向並轉小寫；再補一次 lower() 是把「不分大小寫」寫成明確意圖，
    而不是依賴平台行為。
    """
    return os.path.normcase(os.path.abspath(str(path))).lower()


def _is_same_or_inside(inner: str, outer: str) -> bool:
    """inner 等於 outer，或位於 outer 底下。兩者都要先過 _canonical。"""
    # join(outer, "") 會補上結尾分隔符，讓 "…\\out" 不會誤中 "…\\out_tmp"。
    return inner == outer or inner.startswith(os.path.join(outer, ""))


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
    # 暫存夾會被反覆清空。與輸出根目錄相同、或任一方在另一方底下，
    # 都等於邊產出邊刪掉——不只擋「相等」。
    temp_c, out_c = _canonical(temp), _canonical(out)
    if _is_same_or_inside(temp_c, out_c) or _is_same_or_inside(out_c, temp_c):
        raise ConfigError(
            "paths.temp_dir 與 paths.output_root 不能相同，也不能一個在另一個底下——"
            "暫存夾會被反覆清空，這樣會刪掉已歸檔的產出"
            f"（temp_dir={temp}，output_root={out}）"
        )
    return Paths(temp_dir=temp, output_root=out)


def _parse_format_table(raw: Any, section: str, formats: Tuple[str, ...]) -> Mapping[str, Any]:
    """
    以格式名為 key 的對照表：formats 裡每個都要有，多的拒絕。

    多出來的格式多半是使用者從 formats 拿掉了卻忘了這裡；靜默接受會讓
    兩個區段各說各話，之後很難看出哪邊才是真的。
    """
    data = _as_mapping(raw, section)
    extra = _extra_keys(data, formats)
    if extra:
        raise ConfigError(
            f"{section} 有 formats 裡沒列的格式：{'、'.join(extra)}。"
            "要嘛把它加回 formats，要嘛把這一項刪掉"
        )
    for fmt in formats:
        _require(data, fmt, section)
    return data


def _parse_expected_outputs(raw: Any, formats: Tuple[str, ...]) -> Mapping[str, Tuple[str, ...]]:
    section = "expected_outputs"
    data = _parse_format_table(raw, section, formats)
    table: Dict[str, Tuple[str, ...]] = {}
    for fmt in formats:
        where = _where(section, fmt)
        exts = data[fmt]
        # 空清單代表「預期 0 個檔案」——任務會在還沒產出時就被判定完成。
        if not isinstance(exts, list) or not exts:
            raise ConfigError(f"{where} 必須是非空清單，例如 [\".dxf\"]")
        for ext in exts:
            # 沒有前導點的副檔名比對不到任何檔案，效果是永遠逾時。
            if not isinstance(ext, str) or not ext.startswith(".") or len(ext) < 2:
                raise ConfigError(
                    f"{where} 的每一項都必須是以 . 開頭的副檔名字串，"
                    f"目前有 {ext!r}"
                )
        table[fmt] = tuple(exts)
    return MappingProxyType(table)


def _parse_detection(raw: Any) -> Detection:
    data = _as_mapping(raw, "detection")
    allowed = ("poll_interval_ms", "stable_samples", "quiet_period_sec", "timeout_sec")
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
        quiet_period_sec=_as_number(
            _require(data, "quiet_period_sec", "detection"),
            "quiet_period_sec",
            "detection",
            minimum=0,
        ),
        timeout_sec=_as_int(
            _require(data, "timeout_sec", "detection"),
            "timeout_sec",
            "detection",
            minimum=1,
        ),
    )


def _parse_zip(raw: Any) -> ZipSettings:
    data = _as_mapping(raw, "zip")
    _reject_unknown(data, ("complete_dialog",), "zip")
    where = "zip.complete_dialog"
    dialog = _as_mapping(_require(data, "complete_dialog", "zip"), "complete_dialog", "zip")
    _reject_unknown(dialog, ("title_like", "ok_button"), where)

    title = _as_glob_title(
        _require(dialog, "title_like", where),
        "title_like",
        where,
        "那會把任何視窗都當成完成對話框，OK 會按在錯的東西上",
    )
    return ZipSettings(
        complete_dialog=CompleteDialog(
            title_like=title,
            ok_button=_as_nonempty_str(_require(dialog, "ok_button", where), "ok_button", where),
        )
    )


def _parse_dxf(raw: Any, formats: Tuple[str, ...]) -> DxfSettings:
    data = _as_mapping(raw, "dxf")
    _reject_unknown(data, ("completion", "file_type_labels"), "dxf")
    completion = _as_choice(
        _require(data, "completion", "dxf"),
        "completion",
        "dxf",
        VALID_COMPLETION_MODES,
        "files 靠預期檔案數，results_text 靠 DCU 的 Results 窗格",
    )
    section = "dxf.file_type_labels"
    labels = _parse_format_table(
        _require(data, "file_type_labels", "dxf"), section, _dxf_formats(formats)
    )
    table = {
        fmt: _as_nonempty_str(labels[fmt], fmt, section) for fmt in _dxf_formats(formats)
    }
    return DxfSettings(completion=completion, file_type_labels=MappingProxyType(table))


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
        status = _require(data, "result_status", where)
        if not isinstance(status, str) or status not in STATUS_NAMES:
            raise ConfigError(
                f"{where}.result_status 必須是任務狀態代碼之一：{list(STATUS_NAMES)}，"
                f"目前是 {status!r}"
            )
        rules.append(
            DialogRule(
                title_like=_as_glob_title(
                    _require(data, "title_like", where),
                    "title_like",
                    where,
                    "那會把每個未知視窗都按掉，「未知一律停機」就失效了",
                ),
                action=action,
                result_status=status,
            )
        )
    return tuple(rules)


def _parse_control(raw: Any, name: str, section: str) -> ControlSpec:
    where = _where(section, name)
    entry = _as_mapping(raw, name, section)
    _reject_unknown(entry, ("strategy", "value"), where)
    strategy = _as_choice(
        _require(entry, "strategy", where), "strategy", where, VALID_STRATEGIES
    )
    # window 是整組控制項的搜尋根。用 name 找頂層視窗會拿到第一個名字相符的
    # 任何東西（包含別的程式），底下每一項定位都會跟著錯。
    if name == WINDOW_CONTROL and strategy != STRATEGY_TITLE_RE:
        raise ConfigError(
            f"{where}.strategy 必須是 {STRATEGY_TITLE_RE!r}（視窗只能用標題正規式定位），"
            f"目前是 {strategy!r}"
        )
    value = _require(entry, "value", where)
    if strategy == STRATEGY_INDEX:
        # 字串 "3" 會被當成名字去找；True 是 int 的子型別、會安靜地變成 1；
        # 負數會從尾端數——全都不是使用者的意思。
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(
                f"{where}.value 在 strategy 為 index 時必須是 0 或正整數，目前是 {value!r}"
            )
        return ControlSpec(strategy=strategy, value=value)
    text = _as_nonempty_str(value, "value", where)
    if strategy == STRATEGY_TITLE_RE:
        # 使用者很容易把 * 當萬用字元寫進來；編譯失敗若拖到目標機才爆，
        # 錯誤會出現在 UI 層的堆疊裡，離設定檔很遠。
        try:
            re.compile(text)
        except re.error as exc:
            raise ConfigError(
                f"{where}.value 不是合法的正規表示式（strategy 為 title_re）：{exc}。"
                "任意文字請用 .* 而不是 *"
            ) from exc
    return ControlSpec(strategy=strategy, value=text)


def _parse_control_group(raw: Any, group: str, names: Tuple[str, ...]) -> Mapping[str, ControlSpec]:
    section = f"controls.{group}"
    data = _as_mapping(raw, group, "controls")
    _reject_unknown(data, names, section)
    specs = {
        name: _parse_control(_require(data, name, section), name, section) for name in names
    }
    return MappingProxyType(specs)


def _parse_controls(raw: Any) -> Controls:
    data = _as_mapping(raw, "controls")
    _reject_unknown(data, tuple(CONTROL_GROUPS), "controls")
    groups = {
        group: _parse_control_group(_require(data, group, "controls"), group, names)
        for group, names in CONTROL_GROUPS.items()
    }
    return Controls(explorer=groups["explorer"], dcu=groups["dcu"])


# ── 入口 ─────────────────────────────────────────────────────────────


def parse(data: Mapping[str, Any]) -> Config:
    """把 dict 驗證並轉成 Config。純函式，不修改輸入。"""
    if not isinstance(data, dict):
        raise ConfigError("設定必須是一個 JSON 物件")

    allowed = (
        "models",
        "formats",
        "paths",
        "expected_outputs",
        "detection",
        "zip",
        "dxf",
        "archival",
        "dialog_whitelist",
        "controls",
    )
    _reject_unknown(data, allowed)

    models, selection_mode = _parse_models(_require(data, "models"))
    formats = _parse_formats(_require(data, "formats"))
    return Config(
        models=models,
        is_selection_mode=selection_mode,
        formats=formats,
        paths=_parse_paths(_require(data, "paths")),
        expected_outputs=_parse_expected_outputs(_require(data, "expected_outputs"), formats),
        detection=_parse_detection(_require(data, "detection")),
        zip=_parse_zip(_require(data, "zip")),
        dxf=_parse_dxf(_require(data, "dxf"), formats),
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
