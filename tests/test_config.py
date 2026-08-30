"""
A1 設定模組測試（對應 spec: operability「設定外置」、batch-export「model 清單來源」）。

parse() 是純函式：吃 dict、吐 Config 或拋 ConfigError，完全不碰檔案系統，
因此每一種壞掉的設定都能在這裡直接構造出來測。

設計上有一條刻意的嚴格規則：**未知欄位一律報錯**。
把 timeout_sec 拼成 timout_sec 若被靜默忽略，使用者會以為自己調過參數了，
而實際上跑的是預設值——這種錯誤在現場極難察覺。
"""

import copy
import json
from pathlib import Path

import pytest

from lib import config as cfg

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def valid_dict():
    """一份最小但完整的合法設定。各測試在此之上做單點破壞。"""
    return {
        "models": "SELECTED",
        "formats": ["ZIP", "AAMA", "ASTM"],
        "paths": {
            "temp_dir": "%USERPROFILE%\\Desktop\\_accumark_temp",
            "output_root": "%USERPROFILE%\\Desktop\\AccuMark匯出",
        },
        "detection": {
            "poll_interval_ms": 500,
            "stable_samples": 3,
            "timeout_sec": 300,
            "verify_exclusive_lock": False,
        },
        "archival": {
            "add_format_suffix": False,
            "output_dir_pattern": "{root}_{yymmdd}_{HHMM}",
        },
        "dialog_whitelist": [
            {
                "title_like": "*已存在*",
                "action": "Cancel",
                "result_status": "FAILED_TARGET_EXISTS",
            }
        ],
        "controls": {
            "model_list": {"strategy": "auto_id", "value": ""},
            "export_zip": {"strategy": "name", "value": ""},
            "export_aama": {"strategy": "name", "value": ""},
            "export_astm": {"strategy": "name", "value": ""},
            "dialog_path_box": {"strategy": "auto_id", "value": ""},
            "dialog_ok_button": {"strategy": "name", "value": ""},
        },
    }


def without(section, key):
    d = valid_dict()
    if section is None:
        del d[key]
    else:
        del d[section][key]
    return d


def with_value(section, key, value):
    d = valid_dict()
    if section is None:
        d[key] = value
    else:
        d[section][key] = value
    return d


# ── 快樂路徑 ──────────────────────────────────────────────────────────


def test_valid_config_parses():
    c = cfg.parse(valid_dict())
    assert c.formats == ("ZIP", "AAMA", "ASTM")
    assert c.detection.stable_samples == 3
    assert c.archival.add_format_suffix is False


def test_bundled_config_json_is_valid():
    """
    交付包裡附的 config.json 必須真的能被自己的解析器吃下去。
    範本與 schema 走鐘是很容易發生又很難察覺的事。
    """
    data = json.loads((SCRIPTS / "config.json").read_text(encoding="utf-8"))
    cfg.parse(data)  # 不應拋出


# ── models：兩種來源模式 ─────────────────────────────────────────────


def test_selection_mode():
    c = cfg.parse(with_value(None, "models", "SELECTED"))
    assert c.is_selection_mode is True
    assert c.models == ()


def test_explicit_list_mode():
    c = cfg.parse(with_value(None, "models", ["A-1234", "A-1235"]))
    assert c.is_selection_mode is False
    assert c.models == ("A-1234", "A-1235")


def test_empty_model_list_rejected():
    """空清單代表沒東西可做，多半是使用者刪過頭了，不該靜默跑完 0 個任務。"""
    with pytest.raises(cfg.ConfigError, match="models"):
        cfg.parse(with_value(None, "models", []))


def test_unknown_models_keyword_rejected():
    with pytest.raises(cfg.ConfigError, match="models"):
        cfg.parse(with_value(None, "models", "ALL"))


def test_model_list_with_non_string_rejected():
    with pytest.raises(cfg.ConfigError, match="models"):
        cfg.parse(with_value(None, "models", ["A-1234", 123]))


# ── formats ──────────────────────────────────────────────────────────


def test_unknown_format_rejected():
    with pytest.raises(cfg.ConfigError, match="formats"):
        cfg.parse(with_value(None, "formats", ["ZIP", "PDF"]))


def test_empty_formats_rejected():
    with pytest.raises(cfg.ConfigError, match="formats"):
        cfg.parse(with_value(None, "formats", []))


def test_duplicate_formats_rejected():
    """重複會讓同一個 model 匯出兩次同格式，第二次必然撞檔名。"""
    with pytest.raises(cfg.ConfigError, match="formats"):
        cfg.parse(with_value(None, "formats", ["ZIP", "ZIP"]))


# ── 必填欄位 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "section,key",
    [
        (None, "models"),
        (None, "formats"),
        (None, "paths"),
        (None, "detection"),
        (None, "archival"),
        (None, "controls"),
        ("paths", "temp_dir"),
        ("paths", "output_root"),
        ("detection", "poll_interval_ms"),
        ("detection", "stable_samples"),
        ("detection", "timeout_sec"),
        ("archival", "add_format_suffix"),
        ("archival", "output_dir_pattern"),
    ],
)
def test_missing_required_field_is_named_in_error(section, key):
    """
    錯誤訊息必須指名是哪個欄位，否則使用者只能瞎找。

    這裡刻意同時斷言「缺少必填欄位」這句話：只比對欄位名的話，
    下游的型別檢查（"xxx 必須是字串"）會讓測試碰巧通過，
    必填檢查其實被拿掉了也驗不出來。
    """
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without(section, key))
    assert key in str(exc.value)


@pytest.mark.parametrize(
    "control",
    [
        "model_list",
        "export_zip",
        "export_aama",
        "export_astm",
        "dialog_path_box",
        "dialog_ok_button",
    ],
)
def test_missing_control_is_named_in_error(control):
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without("controls", control))
    assert control in str(exc.value)


# ── 未知欄位（拼錯保護）──────────────────────────────────────────────


def test_unknown_top_level_key_rejected():
    with pytest.raises(cfg.ConfigError, match="timout_sec"):
        cfg.parse(with_value(None, "timout_sec", 300))


def test_unknown_detection_key_rejected():
    """把 timeout_sec 拼成 timout_sec 若被忽略，使用者會以為調過參數了。"""
    d = valid_dict()
    d["detection"]["timout_sec"] = 600
    with pytest.raises(cfg.ConfigError, match="timout_sec"):
        cfg.parse(d)


def test_underscore_keys_allowed_as_comments():
    """
    JSON 沒有註解語法，用 "_" 開頭的鍵當說明欄位。
    拼錯保護不受影響——拼錯的欄位不會剛好以底線開頭。
    """
    d = valid_dict()
    d["_說明"] = "這份設定的用途"
    d["detection"]["_說明"] = "輪詢參數"
    d["controls"]["_說明"] = "期一探測後填入"
    cfg.parse(d)  # 不應拋出


# ── 數值範圍 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, -1, -500])
def test_non_positive_poll_interval_rejected(value):
    with pytest.raises(cfg.ConfigError, match="poll_interval_ms"):
        cfg.parse(with_value("detection", "poll_interval_ms", value))


@pytest.mark.parametrize("value", [0, -3])
def test_non_positive_stable_samples_rejected(value):
    with pytest.raises(cfg.ConfigError, match="stable_samples"):
        cfg.parse(with_value("detection", "stable_samples", value))


def test_single_stable_sample_rejected():
    """
    只取樣一次等於完全沒有穩定判定——檔案剛出現就會被當成寫完。
    這正是 TD-4 要避免的靜默資料損毀，下限必須是 2。
    """
    with pytest.raises(cfg.ConfigError, match="stable_samples"):
        cfg.parse(with_value("detection", "stable_samples", 1))


@pytest.mark.parametrize("value", [0, -10])
def test_non_positive_timeout_rejected(value):
    with pytest.raises(cfg.ConfigError, match="timeout_sec"):
        cfg.parse(with_value("detection", "timeout_sec", value))


def test_boolean_field_rejects_non_boolean():
    with pytest.raises(cfg.ConfigError, match="add_format_suffix"):
        cfg.parse(with_value("archival", "add_format_suffix", "yes"))


# ── 路徑處理 ─────────────────────────────────────────────────────────


def test_environment_variable_is_expanded(monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\tester")
    c = cfg.parse(with_value("paths", "temp_dir", "%USERPROFILE%\\tmp"))
    assert "%" not in str(c.paths.temp_dir)
    assert "tester" in str(c.paths.temp_dir)


def test_path_with_chinese_and_spaces_survives(monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\tester")
    raw = "%USERPROFILE%\\桌面 資料夾\\AccuMark 匯出"
    c = cfg.parse(with_value("paths", "output_root", raw))
    text = str(c.paths.output_root)
    assert "桌面 資料夾" in text
    assert "AccuMark 匯出" in text


def test_unexpandable_variable_is_rejected():
    """留著沒展開的 %VAR% 會變成字面上的資料夾名，安靜地建在奇怪的地方。"""
    with pytest.raises(cfg.ConfigError, match="NO_SUCH_VAR"):
        cfg.parse(with_value("paths", "temp_dir", "%NO_SUCH_VAR%\\tmp"))


def test_temp_dir_must_differ_from_output_root():
    """
    暫存夾會被反覆清空，若與輸出根目錄相同，等於邊產出邊刪掉。
    """
    d = valid_dict()
    d["paths"]["temp_dir"] = "%USERPROFILE%\\same"
    d["paths"]["output_root"] = "%USERPROFILE%\\same"
    with pytest.raises(cfg.ConfigError, match="temp_dir"):
        cfg.parse(d)


# ── 對話框白名單 ─────────────────────────────────────────────────────


def test_whitelist_rule_parsed():
    c = cfg.parse(valid_dict())
    assert len(c.dialog_whitelist) == 1
    assert c.dialog_whitelist[0].action == "Cancel"


def test_whitelist_may_be_empty():
    """空白名單是合法的最嚴格狀態：任何對話框都會停機。"""
    c = cfg.parse(with_value(None, "dialog_whitelist", []))
    assert c.dialog_whitelist == ()


def test_whitelist_missing_key_rejected():
    d = valid_dict()
    del d["dialog_whitelist"][0]["action"]
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(d)
    assert "action" in str(exc.value)


def test_whitelist_unknown_action_rejected():
    d = valid_dict()
    d["dialog_whitelist"][0]["action"] = "ClickYes"
    with pytest.raises(cfg.ConfigError, match="action"):
        cfg.parse(d)


# ── 控制項定位策略 ───────────────────────────────────────────────────


def test_unknown_locator_strategy_rejected():
    d = valid_dict()
    d["controls"]["model_list"]["strategy"] = "xpath"
    with pytest.raises(cfg.ConfigError, match="strategy"):
        cfg.parse(d)


def test_controls_may_be_blank_before_probing():
    """期一探測完成前 controls 的 value 是空的，這在解析階段必須合法。"""
    c = cfg.parse(valid_dict())
    assert c.controls["model_list"].value == ""
    assert c.controls_ready is False


def test_controls_ready_when_all_filled():
    d = valid_dict()
    for name in d["controls"]:
        d["controls"][name]["value"] = "something"
    assert cfg.parse(d).controls_ready is True


# ── 不可變 ───────────────────────────────────────────────────────────


def test_config_is_immutable():
    """設定在執行期被改動會讓日誌與實際行為不一致，難以事後追查。"""
    c = cfg.parse(valid_dict())
    with pytest.raises(Exception):
        c.formats = ("ZIP",)


def test_parse_does_not_mutate_input():
    d = valid_dict()
    snapshot = copy.deepcopy(d)
    cfg.parse(d)
    assert d == snapshot
