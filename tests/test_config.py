"""
A1 設定模組測試（對應 spec: operability「設定外置」、batch-export「model 清單來源」
「完成偵測」「每個任務恰含一個 model」；schema 依 design.md §4.1）。

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
from lib.reporting import Status

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# 用一個明顯不存在的根目錄，確保路徑比對不依賴真實檔案系統。
NOWHERE = "C:\\__accumark_test_nowhere__"


def valid_dict():
    """一份最小但完整的合法設定（design.md §4.1）。各測試在此之上做單點破壞。"""
    return {
        "models": "SELECTED",
        "formats": ["ZIP", "AAMA", "ASTM"],
        "paths": {
            "temp_dir": "%USERPROFILE%\\Desktop\\_accumark_temp",
            "output_root": "%USERPROFILE%\\Desktop\\AccuMark匯出",
        },
        "expected_outputs": {
            "ZIP": [".zip"],
            "AAMA": [".dxf", ".rul"],
            "ASTM": [".dxf"],
        },
        "detection": {
            "poll_interval_ms": 500,
            "stable_samples": 3,
            "quiet_period_sec": 1.0,
            "timeout_sec": 300,
        },
        "zip": {
            "complete_dialog": {"title_like": "*Process Complete*", "ok_button": "OK"},
        },
        "dxf": {
            "completion": "files",
            "file_type_labels": {"AAMA": "AAMA", "ASTM": "ASTM"},
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
            "explorer": {
                "window": {"strategy": "title_re", "value": "AccuMark Explorer.*"},
                "model_list": {"strategy": "control_type", "value": "List"},
                "menu_file": {"strategy": "name", "value": "File"},
                "menu_export_zip": {"strategy": "name", "value": "Export Zip"},
                "export_to_dialog": {"strategy": "title_re", "value": "Export To.*"},
                "export_to_path": {"strategy": "control_type", "value": "Edit"},
                "export_to_ok": {"strategy": "name", "value": "OK"},
                "export_screen": {"strategy": "title_re", "value": "Export.*"},
                "export_screen_ok": {"strategy": "name", "value": "OK"},
            },
            "dcu": {
                "window": {"strategy": "title_re", "value": "Data Conversion.*"},
                "file_type": {"strategy": "name", "value": "File Type"},
                "source_list": {"strategy": "name", "value": "Source File Name"},
                "destination_path": {"strategy": "name", "value": "Destination Path"},
                "run_button": {"strategy": "name", "value": "Export"},
                "results": {"strategy": "name", "value": "Results"},
            },
        },
    }


def _walk(d, path):
    for key in path:
        d = d[key]
    return d


def without(*path):
    """刪掉巢狀路徑上的最後一個鍵，例如 without("zip", "complete_dialog", "ok_button")。"""
    d = valid_dict()
    del _walk(d, path[:-1])[path[-1]]
    return d


def with_value(*path_and_value):
    """把巢狀路徑上的鍵設成新值，例如 with_value("detection", "timeout_sec", 0)。"""
    *path, value = path_and_value
    d = valid_dict()
    _walk(d, path[:-1])[path[-1]] = value
    return d


def only_zip():
    """只匯出 ZIP 的設定：DXF 相關區段必須跟著縮到空。"""
    d = valid_dict()
    d["formats"] = ["ZIP"]
    d["expected_outputs"] = {"ZIP": [".zip"]}
    d["dxf"]["file_type_labels"] = {}
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


def test_bundled_config_prefills_controls_from_official_docs():
    """
    TD-10：controls 以官方文件的顯示名稱預填，dry-run 才有東西可對照。
    範本若被清成空字串，使用者第一次跑 dry-run 會得到「全部找不到」，
    而那正是「介面自繪、方案翻船」的訊號——假警報的代價太高。
    """
    data = json.loads((SCRIPTS / "config.json").read_text(encoding="utf-8"))
    c = cfg.parse(data)
    assert c.controls.explorer["menu_export_zip"].value == "Export Zip"
    assert c.controls.dcu["source_list"].value == "Source File Name"
    assert c.controls.explorer["window"].strategy == "title_re"
    assert c.controls.dcu["window"].strategy == "title_re"


def test_bundled_config_explains_every_section_to_the_pattern_maker():
    """
    設定檔是給打版師改的，不是給工程師看的。每個物件區段都要有 _說明
    講「這是什麼、什麼時候要改」；少一段，那一段就只能靠猜。
    """
    data = json.loads((SCRIPTS / "config.json").read_text(encoding="utf-8"))
    for name, section in data.items():
        if isinstance(section, dict):
            assert "_說明" in section, f"config.json 的 {name} 區段缺少 _說明"
    for group in ("explorer", "dcu"):
        assert "_說明" in data["controls"][group], f"controls.{group} 缺少 _說明"


# ── models：兩種來源模式 ─────────────────────────────────────────────


def test_selection_mode():
    c = cfg.parse(with_value("models", "SELECTED"))
    assert c.is_selection_mode is True
    assert c.models == ()


def test_explicit_list_mode():
    c = cfg.parse(with_value("models", ["A-1234", "A-1235"]))
    assert c.is_selection_mode is False
    assert c.models == ("A-1234", "A-1235")


def test_empty_model_list_rejected():
    """空清單代表沒東西可做，多半是使用者刪過頭了，不該靜默跑完 0 個任務。"""
    with pytest.raises(cfg.ConfigError, match="models"):
        cfg.parse(with_value("models", []))


def test_unknown_models_keyword_rejected():
    with pytest.raises(cfg.ConfigError, match="models"):
        cfg.parse(with_value("models", "ALL"))


def test_model_list_with_non_string_rejected():
    with pytest.raises(cfg.ConfigError, match="models"):
        cfg.parse(with_value("models", ["A-1234", 123]))


# ── formats ──────────────────────────────────────────────────────────


def test_unknown_format_rejected():
    with pytest.raises(cfg.ConfigError, match="formats"):
        cfg.parse(with_value("formats", ["ZIP", "PDF"]))


def test_empty_formats_rejected():
    with pytest.raises(cfg.ConfigError, match="formats"):
        cfg.parse(with_value("formats", []))


def test_duplicate_formats_rejected():
    """重複會讓同一個 model 匯出兩次同格式，第二次必然撞檔名。"""
    with pytest.raises(cfg.ConfigError, match="formats"):
        cfg.parse(with_value("formats", ["ZIP", "ZIP"]))


def test_dxf_formats_excludes_zip():
    """ZIP 走 Explorer，其餘走 DCU；下游要靠這個分流，不能自己再算一次。"""
    assert cfg.parse(valid_dict()).dxf_formats == ("AAMA", "ASTM")
    assert cfg.parse(only_zip()).dxf_formats == ()


# ── 必填欄位 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        ("models",),
        ("formats",),
        ("paths",),
        ("expected_outputs",),
        ("detection",),
        ("zip",),
        ("dxf",),
        ("archival",),
        ("controls",),
        ("paths", "temp_dir"),
        ("paths", "output_root"),
        ("detection", "poll_interval_ms"),
        ("detection", "stable_samples"),
        ("detection", "quiet_period_sec"),
        ("detection", "timeout_sec"),
        ("zip", "complete_dialog"),
        ("zip", "complete_dialog", "title_like"),
        ("zip", "complete_dialog", "ok_button"),
        ("dxf", "completion"),
        ("dxf", "file_type_labels"),
        ("archival", "add_format_suffix"),
        ("archival", "output_dir_pattern"),
        ("controls", "explorer"),
        ("controls", "dcu"),
    ],
    ids=".".join,
)
def test_missing_required_field_is_named_in_error(path):
    """
    錯誤訊息必須指名是哪個欄位，否則使用者只能瞎找。

    這裡刻意同時斷言「缺少必填欄位」這句話：只比對欄位名的話，
    下游的型別檢查（"xxx 必須是字串"）會讓測試碰巧通過，
    必填檢查其實被拿掉了也驗不出來。
    """
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without(*path))
    assert path[-1] in str(exc.value)


# ── 未知欄位（拼錯保護）──────────────────────────────────────────────


def test_unknown_top_level_key_rejected():
    with pytest.raises(cfg.ConfigError, match="timout_sec"):
        cfg.parse(with_value("timout_sec", 300))


def test_unknown_detection_key_rejected():
    """把 timeout_sec 拼成 timout_sec 若被忽略，使用者會以為調過參數了。"""
    with pytest.raises(cfg.ConfigError, match="timout_sec"):
        cfg.parse(with_value("detection", "timout_sec", 600))


def test_removed_verify_exclusive_lock_is_rejected():
    """
    design §4.1 已移除 verify_exclusive_lock。留著舊欄位若被靜默接受，
    使用者會以為獨佔開檔檢查還在跑——其實那段程式碼已經不存在了。
    """
    with pytest.raises(cfg.ConfigError, match="verify_exclusive_lock"):
        cfg.parse(with_value("detection", "verify_exclusive_lock", False))


@pytest.mark.parametrize(
    "path",
    [
        ("zip", "extra"),
        ("zip", "complete_dialog", "cancel_button"),
        ("dxf", "notch_table"),
        ("controls", "pds"),
        ("controls", "explorer", "menu_edit"),
        ("controls", "dcu", "window", "timeout"),
    ],
    ids=".".join,
)
def test_unknown_nested_key_rejected(path):
    """新區段也要有拼錯保護，不能只有舊區段嚴格。"""
    with pytest.raises(cfg.ConfigError, match=path[-1]):
        cfg.parse(with_value(*path, "x"))


def test_underscore_keys_allowed_as_comments():
    """
    JSON 沒有註解語法，用 "_" 開頭的鍵當說明欄位。
    拼錯保護不受影響——拼錯的欄位不會剛好以底線開頭。
    """
    d = valid_dict()
    d["_說明"] = "這份設定的用途"
    d["detection"]["_說明"] = "輪詢參數"
    d["expected_outputs"]["_說明"] = "每種格式產出的副檔名"
    d["zip"]["_說明"] = "ZIP 完成對話框"
    d["zip"]["complete_dialog"]["_title_like"] = "標題"
    d["dxf"]["_說明"] = "DCU 設定"
    d["dxf"]["file_type_labels"]["_說明"] = "下拉文字"
    d["controls"]["_說明"] = "以官方文件名稱預填"
    d["controls"]["explorer"]["_說明"] = "Explorer 這一半"
    d["controls"]["dcu"]["window"]["_註"] = "標題正規式"
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


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.5])
def test_quiet_period_accepts_non_negative_numbers(value):
    """
    JSON 裡 1 與 1.0 都是合法寫法，使用者不該因為少打 .0 被擋。
    0 也合法：代表穩定後不多等——那是一個明確的選擇，不是錯誤。
    """
    c = cfg.parse(with_value("detection", "quiet_period_sec", value))
    assert isinstance(c.detection.quiet_period_sec, float)
    assert c.detection.quiet_period_sec == float(value)


@pytest.mark.parametrize("value", [-0.5, -1, "1.0", True, None])
def test_quiet_period_rejects_negative_or_non_numeric(value):
    """負的靜默期沒有意義；字串 "1.0" 會在算術時炸在現場而不是啟動時。"""
    with pytest.raises(cfg.ConfigError, match="quiet_period_sec"):
        cfg.parse(with_value("detection", "quiet_period_sec", value))


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


def _paths(temp, out):
    d = valid_dict()
    d["paths"]["temp_dir"] = temp
    d["paths"]["output_root"] = out
    return d


def test_temp_dir_must_differ_from_output_root():
    """
    暫存夾會被反覆清空，若與輸出根目錄相同，等於邊產出邊刪掉。
    """
    with pytest.raises(cfg.ConfigError, match="temp_dir"):
        cfg.parse(_paths(NOWHERE + "\\same", NOWHERE + "\\same"))


def test_temp_dir_inside_output_root_rejected():
    """
    暫存夾在輸出根目錄底下：歸檔會把產出搬進 output_root 的子資料夾，
    而暫存夾自己也是它的子資料夾——清空暫存夾時有機會連歸檔一起掃掉。
    這條在分支審查被抓出來：原本只擋「相等」。
    """
    with pytest.raises(cfg.ConfigError, match="temp_dir"):
        cfg.parse(_paths(NOWHERE + "\\out\\tmp", NOWHERE + "\\out"))


def test_output_root_inside_temp_dir_rejected():
    """反過來也一樣：輸出根目錄在暫存夾底下，清空暫存夾等於刪掉全部產出。"""
    with pytest.raises(cfg.ConfigError, match="output_root"):
        cfg.parse(_paths(NOWHERE + "\\tmp", NOWHERE + "\\tmp\\out"))


def test_nested_path_check_ignores_case():
    """Windows 檔案系統不分大小寫，C:\\Out 與 c:\\out 是同一個資料夾。"""
    with pytest.raises(cfg.ConfigError, match="temp_dir"):
        cfg.parse(_paths(NOWHERE.lower() + "\\OUT\\tmp", NOWHERE.upper() + "\\out"))


def test_nested_path_check_normalizes_dot_dot_and_slashes():
    """
    使用者手打的路徑可能混用 / 與 ..；正規化後仍在對方底下就得擋，
    否則只要多寫一段 ..\\out 就繞過檢查。
    """
    with pytest.raises(cfg.ConfigError, match="temp_dir"):
        cfg.parse(_paths(NOWHERE + "/out/../out/tmp", NOWHERE + "\\out"))


def test_sibling_with_common_prefix_is_allowed():
    """
    「AccuMark匯出_tmp」不在「AccuMark匯出」底下，只是字首相同。
    用字串 startswith 硬比會誤殺這種正常配置。
    """
    c = cfg.parse(_paths(NOWHERE + "\\AccuMark匯出_tmp", NOWHERE + "\\AccuMark匯出"))
    assert c.paths.temp_dir != c.paths.output_root


def test_nested_path_check_does_not_need_directories_to_exist():
    """
    首次執行時兩個目錄都還沒建。檢查若依賴 resolve 到真實檔案系統，
    要嘛在不存在時失效，要嘛在有符號連結時亂跳；兩個都不能接受。
    """
    assert not Path(NOWHERE).exists()
    with pytest.raises(cfg.ConfigError, match="temp_dir"):
        cfg.parse(_paths(NOWHERE + "\\a\\b\\c", NOWHERE + "\\a"))
    cfg.parse(_paths(NOWHERE + "\\a", NOWHERE + "\\b"))  # 不應拋出


# ── expected_outputs（TD-4：每種格式一個 model 的預期產出）────────────


def test_expected_outputs_parsed_as_tuples():
    c = cfg.parse(valid_dict())
    assert c.expected_outputs["AAMA"] == (".dxf", ".rul")
    assert c.expected_outputs["ZIP"] == (".zip",)


def test_expected_outputs_missing_format_is_named():
    """
    少了哪個格式，files 模式就算不出那個格式的預期檔案數——
    等於完成偵測對它失效。必須在啟動時指名。
    """
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without("expected_outputs", "AAMA"))
    assert "AAMA" in str(exc.value)


def test_expected_outputs_format_not_in_formats_rejected():
    """
    多出來的格式多半是使用者從 formats 拿掉了卻忘了這裡；
    靜默接受會讓兩個區段各說各話，之後很難看出哪邊才是真的。
    """
    d = only_zip()
    d["expected_outputs"]["ASTM"] = [".dxf"]
    with pytest.raises(cfg.ConfigError, match="ASTM"):
        cfg.parse(d)


@pytest.mark.parametrize(
    "value",
    [[], ".dxf", None, [".dxf", "rul"], ["dxf"], ["."], [""], [".dxf", 42]],
    ids=repr,
)
def test_expected_outputs_bad_value_rejected(value):
    """
    空清單代表「預期 0 個檔案」——任務會在還沒產出時就被判定完成。
    沒有前導點的副檔名比對不到任何檔案，效果是永遠逾時。
    """
    with pytest.raises(cfg.ConfigError, match="expected_outputs"):
        cfg.parse(with_value("expected_outputs", "AAMA", value))


def test_expected_outputs_must_be_object():
    with pytest.raises(cfg.ConfigError, match="expected_outputs"):
        cfg.parse(with_value("expected_outputs", [".zip"]))


# ── zip.complete_dialog（TD-4：OK 只按在它身上）──────────────────────


def test_zip_complete_dialog_parsed():
    c = cfg.parse(valid_dict())
    assert c.zip.complete_dialog.title_like == "*Process Complete*"
    assert c.zip.complete_dialog.ok_button == "OK"


@pytest.mark.parametrize("value", ["*", "**", "?", "*?*", "  *  ", ""], ids=repr)
def test_zip_title_like_pure_wildcard_rejected(value):
    """
    純萬用字元會匹配任何視窗——包含「是否覆蓋？」。流程在等完成對話框時
    第一個冒出來的視窗就會被當成它，然後 OK 按在錯的東西上。
    這正是 TD-5 要杜絕的那條路徑，所以在設定階段就擋。
    """
    with pytest.raises(cfg.ConfigError, match="title_like"):
        cfg.parse(with_value("zip", "complete_dialog", "title_like", value))


@pytest.mark.parametrize("value", ["", "   "], ids=repr)
def test_zip_ok_button_empty_rejected(value):
    """空的按鈕名稱會讓定位退化成「隨便找一顆鈕」。"""
    with pytest.raises(cfg.ConfigError, match="ok_button"):
        cfg.parse(with_value("zip", "complete_dialog", "ok_button", value))


@pytest.mark.parametrize("key", ["title_like", "ok_button"])
def test_zip_dialog_fields_must_be_strings(key):
    with pytest.raises(cfg.ConfigError, match=key):
        cfg.parse(with_value("zip", "complete_dialog", key, 1))


# ── dxf（TD-9：DCU 表單）─────────────────────────────────────────────


@pytest.mark.parametrize("value", ["files", "results_text"])
def test_dxf_completion_accepts_documented_modes(value):
    c = cfg.parse(with_value("dxf", "completion", value))
    assert c.dxf.completion == value


@pytest.mark.parametrize("value", ["dialog", "FILES", "", None, 1], ids=repr)
def test_dxf_completion_rejects_other_values(value):
    """
    完成模式只有兩種實作。打錯若被接受，等待迴圈會找不到對應分支——
    最好的情況是逾時，最壞的情況是永遠不等。訊息要列出可用值。
    """
    with pytest.raises(cfg.ConfigError, match="completion") as exc:
        cfg.parse(with_value("dxf", "completion", value))
    assert "files" in str(exc.value) and "results_text" in str(exc.value)


def test_dxf_file_type_labels_parsed():
    c = cfg.parse(valid_dict())
    assert c.dxf.file_type_labels["AAMA"] == "AAMA"
    assert c.dxf.file_type_labels["ASTM"] == "ASTM"


def test_dxf_file_type_labels_missing_format_is_named():
    """少了哪個格式，那個格式的 DCU 任務就不知道下拉選單要選什麼字。"""
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without("dxf", "file_type_labels", "ASTM"))
    assert "ASTM" in str(exc.value)


def test_dxf_file_type_labels_rejects_zip():
    """ZIP 不走 DCU，出現在這裡代表使用者搞混了兩條路徑。"""
    with pytest.raises(cfg.ConfigError, match="ZIP"):
        cfg.parse(with_value("dxf", "file_type_labels", "ZIP", "ZIP"))


def test_dxf_file_type_labels_rejects_format_not_in_formats():
    d = only_zip()
    d["dxf"]["file_type_labels"] = {"AAMA": "AAMA"}
    with pytest.raises(cfg.ConfigError, match="AAMA"):
        cfg.parse(d)


def test_only_zip_needs_no_dxf_labels():
    """只匯出 ZIP 時，DCU 相關對照表可以是空的，這是合法而非缺漏。"""
    c = cfg.parse(only_zip())
    assert dict(c.dxf.file_type_labels) == {}
    assert dict(c.expected_outputs) == {"ZIP": (".zip",)}


@pytest.mark.parametrize("value", ["", "  ", 1, None], ids=repr)
def test_dxf_file_type_label_must_be_nonempty_string(value):
    with pytest.raises(cfg.ConfigError, match="AAMA"):
        cfg.parse(with_value("dxf", "file_type_labels", "AAMA", value))


def test_dxf_file_type_labels_must_be_object():
    with pytest.raises(cfg.ConfigError, match="file_type_labels"):
        cfg.parse(with_value("dxf", "file_type_labels", ["AAMA", "ASTM"]))


# ── 對話框白名單 ─────────────────────────────────────────────────────


def test_whitelist_rule_parsed():
    c = cfg.parse(valid_dict())
    assert len(c.dialog_whitelist) == 1
    assert c.dialog_whitelist[0].action == "Cancel"


def test_whitelist_may_be_empty():
    """空白名單是合法的最嚴格狀態：任何對話框都會停機。"""
    c = cfg.parse(with_value("dialog_whitelist", []))
    assert c.dialog_whitelist == ()


def test_whitelist_missing_key_rejected():
    d = valid_dict()
    del d["dialog_whitelist"][0]["action"]
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(d)
    assert "action" in str(exc.value)


@pytest.mark.parametrize("value", ["*", "**", "?", "*?*", "  *  ", ""], ids=repr)
def test_whitelist_title_like_pure_wildcard_rejected(value):
    """
    白名單的 title_like 若只有萬用字元，等於「任何視窗都按 Cancel」——
    那把 TD-5「未知一律停機」整個拆掉：真正該停下來讓人看的對話框，
    會被安靜地按掉，然後任務記成白名單指定的狀態，日誌看起來一切正常。
    """
    d = valid_dict()
    d["dialog_whitelist"][0]["title_like"] = value
    with pytest.raises(cfg.ConfigError, match="dialog_whitelist\\[0\\].title_like"):
        cfg.parse(d)


def test_whitelist_unknown_action_rejected():
    d = valid_dict()
    d["dialog_whitelist"][0]["action"] = "ClickYes"
    with pytest.raises(cfg.ConfigError, match="action"):
        cfg.parse(d)


@pytest.mark.parametrize("status", list(Status), ids=lambda s: s.name)
def test_whitelist_result_status_accepts_every_known_status(status):
    """result_status 的合法值就是 reporting.Status 的名稱，一個都不該漏。"""
    d = valid_dict()
    d["dialog_whitelist"][0]["result_status"] = status.name
    c = cfg.parse(d)
    assert c.dialog_whitelist[0].result_status == status.name


@pytest.mark.parametrize(
    "value",
    ["FAILED_OVERWRITE", "failed_target_exists", "", "SUCCESS ", 1],
    ids=repr,
)
def test_whitelist_unknown_result_status_rejected_with_options(value):
    """
    白名單命中後，主流程會用 result_status 去查 Status；查不到會在現場
    炸出 KeyError——而且是在對話框已經被按掉之後。這條在分支審查被抓出來。
    訊息要列出可用值，使用者才知道該改成什麼。
    """
    d = valid_dict()
    d["dialog_whitelist"][0]["result_status"] = value
    with pytest.raises(cfg.ConfigError, match="result_status") as exc:
        cfg.parse(d)
    text = str(exc.value)
    assert "FAILED_TARGET_EXISTS" in text
    assert "FAILED_SELECTION" in text


# ── 控制項定位（TD-10：兩層結構，explorer / dcu）────────────────────


def test_controls_parsed_into_control_specs():
    c = cfg.parse(valid_dict())
    win = c.controls.explorer["window"]
    assert isinstance(win, cfg.ControlSpec)
    assert (win.strategy, win.value) == ("title_re", "AccuMark Explorer.*")
    assert c.controls.dcu["run_button"].value == "Export"


@pytest.mark.parametrize("name", cfg.EXPLORER_CONTROLS)
def test_missing_explorer_control_is_named_in_error(name):
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without("controls", "explorer", name))
    assert name in str(exc.value)


@pytest.mark.parametrize("name", cfg.DCU_CONTROLS)
def test_missing_dcu_control_is_named_in_error(name):
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without("controls", "dcu", name))
    assert name in str(exc.value)


def test_control_group_sets_match_design():
    """
    §4.1 列的就是這 8 + 6 個。主流程按名字取控制項，名單一漂移就是 KeyError。
    """
    assert cfg.EXPLORER_CONTROLS == (
        "window",
        "model_list",
        "menu_file",
        "menu_export_zip",
        "export_to_dialog",
        "export_to_path",
        "export_to_ok",
        "export_screen",
        "export_screen_ok",
    )
    assert cfg.DCU_CONTROLS == (
        "window",
        "file_type",
        "source_list",
        "destination_path",
        "run_button",
        "results",
    )


@pytest.mark.parametrize("key", ["strategy", "value"])
def test_control_entry_missing_key_is_named(key):
    with pytest.raises(cfg.ConfigError, match="缺少必填欄位") as exc:
        cfg.parse(without("controls", "dcu", "run_button", key))
    assert key in str(exc.value)


def test_control_entry_must_be_object():
    with pytest.raises(cfg.ConfigError, match="run_button"):
        cfg.parse(with_value("controls", "dcu", "run_button", "Export"))


def test_unknown_locator_strategy_rejected():
    with pytest.raises(cfg.ConfigError, match="strategy"):
        cfg.parse(with_value("controls", "explorer", "model_list", "strategy", "xpath"))


@pytest.mark.parametrize(
    "strategy,value",
    [("name", "File"), ("auto_id", "btnFile"), ("control_type", "MenuItem"), ("title_re", "File.*"), ("index", 0)],
)
def test_every_documented_strategy_accepted(strategy, value):
    """§4.1 列的五種策略都得能填；index 的 value 是數字，其餘是字串。"""
    c = cfg.parse(with_value("controls", "explorer", "menu_file", {"strategy": strategy, "value": value}))
    spec = c.controls.explorer["menu_file"]
    assert (spec.strategy, spec.value) == (strategy, value)


@pytest.mark.parametrize("group", ["explorer", "dcu"])
@pytest.mark.parametrize("strategy", ["name", "auto_id", "control_type", "index"])
def test_window_must_use_title_re(group, strategy):
    """
    window 是整組控制項的搜尋根。用 name 找頂層視窗會拿到第一個名字相符的
    任何東西（包含別的程式），底下每一項定位都會跟著錯——所以只准 title_re。
    """
    value = 0 if strategy == "index" else "AccuMark"
    with pytest.raises(cfg.ConfigError, match="title_re") as exc:
        cfg.parse(with_value("controls", group, "window", {"strategy": strategy, "value": value}))
    assert "window" in str(exc.value)


@pytest.mark.parametrize("value", [-1, "3", True, 1.0, None, ""], ids=repr)
def test_index_strategy_requires_non_negative_int(value):
    """
    index 是走訪順序的第幾個。字串 "3" 會被 pywinauto 當成名字去找；
    True 是 int 的子型別、會安靜地變成 1；負數會從尾端數——全都不是使用者的意思。
    """
    with pytest.raises(cfg.ConfigError, match="value"):
        cfg.parse(with_value("controls", "explorer", "model_list", {"strategy": "index", "value": value}))


@pytest.mark.parametrize("value", [0, 3])
def test_index_strategy_accepts_zero_and_positive(value):
    c = cfg.parse(with_value("controls", "explorer", "model_list", {"strategy": "index", "value": value}))
    assert c.controls.explorer["model_list"].value == value


@pytest.mark.parametrize("strategy", ["name", "auto_id", "control_type"])
@pytest.mark.parametrize("value", ["", "   ", 3, None], ids=repr)
def test_string_strategies_require_nonempty_string(strategy, value):
    """
    controls 已改為預填（TD-10），不再有「探測前留空」這個階段。
    空字串會讓定位退化成「該型別的任何東西」，數字則是把 index 填錯了位置。
    """
    with pytest.raises(cfg.ConfigError, match="value"):
        cfg.parse(with_value("controls", "dcu", "file_type", {"strategy": strategy, "value": value}))


@pytest.mark.parametrize("value", ["AccuMark(", "*.exe", "[unclosed"], ids=repr)
def test_title_re_must_be_a_valid_regex(value):
    """
    使用者很容易把 * 當萬用字元寫進 title_re；正規式編譯失敗若拖到
    目標機才爆，錯誤會出現在 pywinauto 的堆疊裡，離設定檔很遠。
    """
    with pytest.raises(cfg.ConfigError, match="title_re"):
        cfg.parse(with_value("controls", "explorer", "export_to_dialog", {"strategy": "title_re", "value": value}))


def test_control_spec_is_immutable():
    spec = cfg.parse(valid_dict()).controls.dcu["results"]
    with pytest.raises(Exception):
        spec.value = "changed"


def test_control_groups_are_read_only():
    """主流程若能偷改定位資訊，日誌上的設定就不再是實際跑的設定。"""
    c = cfg.parse(valid_dict())
    with pytest.raises(TypeError):
        c.controls.explorer["window"] = cfg.ControlSpec("title_re", "x")


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
