"""
B2：定位策略評估與選取狀態偵測（對應 spec: ui-probe「定位策略評估」「選取狀態可讀性探測」）。

這裡測的是 uia.py 的**純函式部分**——吃樹狀資料、吐報告，完全不碰 pywinauto、
不碰 UI。開發機沒有 AccuMark，走訪本身只能到目標機驗收；但「這個控制項該用什麼
方式定位」「這份清單讀不讀得到選取項」這兩個判斷是純運算，出錯的代價又特別高
（定位錯 → 主腳本點到別的東西；選取狀態誤判 → 使用者以為框選有效卻跑出零個
model），所以整條判斷路徑必須在這裡就測乾淨。
"""

import json
from pathlib import Path

import pytest

from lib import config, uia

LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"


# ── 建構小工具 ───────────────────────────────────────────────────────


def node(**kwargs):
    """省掉每次都要填七個欄位。未指定的一律取空值，正好是最難定位的情況。"""
    return uia.UiaNode(**kwargs)


def tree(root_kwargs=None, children=()):
    """建一棵兩層樹，children 為 (kwargs, ...)。"""
    kids = tuple(
        uia.UiaNode(depth=1, sibling_index=i, **kw) for i, kw in enumerate(children)
    )
    return uia.UiaNode(
        depth=0, sibling_index=0, children=kids, **(root_kwargs or {})
    )


def locator_of(*children, at=0):
    """對 children 中第 at 個節點做定位評估，同層即為這些節點。"""
    root = tree({"control_type": "Window"}, children)
    return uia.evaluate_locator(root.children[at], root.children)


# ── 優先序：automation_id 最優先 ─────────────────────────────────────


def test_automation_id_wins_when_unique():
    loc = locator_of({"automation_id": "btnExport", "name": "匯出", "control_type": "Button"})
    assert loc.strategy == uia.STRATEGY_AUTO_ID
    assert loc.value == "btnExport"
    assert loc.stable is True


def test_automation_id_beats_name_even_when_both_unique():
    """優先序反轉的第一道守門：兩者都可用時，必須挑 automation_id。"""
    loc = locator_of(
        {"automation_id": "btnOk", "name": "確定", "control_type": "Button"},
        {"automation_id": "btnCancel", "name": "取消", "control_type": "Button"},
    )
    assert loc.strategy == uia.STRATEGY_AUTO_ID
    assert loc.value == "btnOk"


def test_automation_id_beats_unique_control_type():
    loc = locator_of(
        {"automation_id": "lstModels", "control_type": "List"},
        {"control_type": "Button", "name": "關閉"},
    )
    assert loc.strategy == uia.STRATEGY_AUTO_ID


# ── automation_id 的唯一性判定 ───────────────────────────────────────


def test_duplicate_automation_id_falls_back_to_name():
    """同層兩個一樣的 automation_id 定位不到唯一控制項，必須退到下一順位。"""
    loc = locator_of(
        {"automation_id": "item", "name": "第一列", "control_type": "ListItem"},
        {"automation_id": "item", "name": "第二列", "control_type": "ListItem"},
    )
    assert loc.strategy == uia.STRATEGY_NAME
    assert loc.value == "第一列"


def test_duplicate_automation_id_and_name_falls_through_to_unstable():
    loc = locator_of(
        {"automation_id": "item", "name": "列", "control_type": "ListItem"},
        {"automation_id": "item", "name": "列", "control_type": "ListItem"},
    )
    assert loc.strategy == uia.STRATEGY_UNSTABLE
    assert loc.stable is False


def test_empty_automation_id_is_not_usable():
    loc = locator_of({"automation_id": "", "name": "確定", "control_type": "Button"})
    assert loc.strategy == uia.STRATEGY_NAME


def test_whitespace_automation_id_is_not_usable():
    """全空白的 id 在 UIA 裡查不到東西，等同於沒有。"""
    loc = locator_of({"automation_id": "   ", "name": "確定", "control_type": "Button"})
    assert loc.strategy == uia.STRATEGY_NAME


def test_padded_automation_id_keeps_the_raw_value():
    """
    UIA 的屬性比對是逐字的。報告若擅自把前後空白修掉，使用者抄進設定檔的值
    就定位不到真正的控制項，而錯誤只會是「找不到控制項」。
    """
    loc = locator_of({"automation_id": "  btnOk  ", "control_type": "Button"})
    assert loc.value == "  btnOk  "


def test_padded_value_is_flagged_in_the_reason():
    """空白在報告裡看不出來，必須明講，否則使用者會很自然地把它修掉。"""
    loc = locator_of({"automation_id": "  btnOk  ", "control_type": "Button"})
    assert "空白" in loc.reason


def test_clean_value_has_no_padding_warning():
    loc = locator_of({"automation_id": "btnOk", "control_type": "Button"})
    assert "空白" not in loc.reason


def test_padded_name_keeps_the_raw_value():
    """記事本的狀態列就長這樣：Name 是 "  第 1 列，第 1 行"。"""
    loc = locator_of({"name": "  第 1 列  ", "control_type": "Text"})
    assert loc.value == "  第 1 列  "
    assert "空白" in loc.reason


def test_padding_only_difference_is_not_treated_as_unique():
    """"確定" 與 " 確定 " 在畫面上長得一樣，當成兩個不同的識別太樂觀了。"""
    loc = locator_of(
        {"name": "確定", "control_type": "Button"},
        {"name": " 確定 ", "control_type": "Button"},
    )
    assert loc.strategy == uia.STRATEGY_UNSTABLE


def test_uniqueness_scope_is_the_parent_container_only():
    """
    唯一性只看同一個父容器。不同容器下的同名 id 互不干擾——
    若拿整棵樹判斷，幾乎所有 ListItem 都會被誤判成不可用。
    """
    left = uia.UiaNode(
        control_type="Pane",
        automation_id="left",
        depth=1,
        sibling_index=0,
        children=(uia.UiaNode(automation_id="ok", control_type="Button", depth=2),),
    )
    right = uia.UiaNode(
        control_type="Pane",
        automation_id="right",
        depth=1,
        sibling_index=1,
        children=(uia.UiaNode(automation_id="ok", control_type="Button", depth=2),),
    )
    root = uia.UiaNode(control_type="Window", children=(left, right))

    reports = uia.annotate(root)
    buttons = [r for r in reports if r.node.control_type == "Button"]
    assert len(buttons) == 2
    assert all(r.locator.strategy == uia.STRATEGY_AUTO_ID for r in buttons)


def test_node_missing_from_siblings_is_still_evaluated():
    """呼叫端漏傳自己時不該當成「同層沒有我」而誤判唯一性。"""
    target = node(automation_id="btn", control_type="Button", sibling_index=3)
    loc = uia.evaluate_locator(target, [])
    assert loc.strategy == uia.STRATEGY_AUTO_ID


# ── name 策略 ────────────────────────────────────────────────────────


def test_unique_name_used_when_no_automation_id():
    loc = locator_of(
        {"name": "另存新檔", "control_type": "MenuItem"},
        {"name": "開啟舊檔", "control_type": "MenuItem"},
    )
    assert loc.strategy == uia.STRATEGY_NAME
    assert loc.value == "另存新檔"


def test_duplicate_name_falls_back_to_control_type():
    """同層兩個同名但型別不同 → name 不可用，型別反而是唯一的。"""
    loc = locator_of(
        {"name": "路徑", "control_type": "Edit"},
        {"name": "路徑", "control_type": "Text"},
    )
    assert loc.strategy == uia.STRATEGY_INDEX
    assert loc.value.startswith("Edit")


def test_whitespace_name_is_not_usable():
    loc = locator_of(
        {"name": "  ", "control_type": "Edit"},
        {"name": "  ", "control_type": "Button"},
    )
    assert loc.strategy == uia.STRATEGY_INDEX


# ── control_type + 同層索引 ──────────────────────────────────────────


def test_unique_control_type_yields_index_strategy():
    loc = locator_of(
        {"control_type": "Edit"},
        {"control_type": "Button"},
        {"control_type": "Button"},
    )
    assert loc.strategy == uia.STRATEGY_INDEX
    assert loc.value == "Edit#0"
    assert loc.stable is True


def test_index_value_carries_control_type_and_position():
    loc = locator_of(
        {"control_type": "Button", "name": "確定"},
        {"control_type": "Edit"},
        at=1,
    )
    assert loc.value == "Edit#0"


# ── UNSTABLE ─────────────────────────────────────────────────────────


def test_multiple_same_type_without_identity_is_unstable():
    """
    對應 Scenario「控制項無任何穩定識別」：id 與 name 皆空、同層又有多個同型。
    只剩位置可用，而位置會隨版本與資料筆數變動。
    """
    loc = locator_of(
        {"control_type": "ListItem"},
        {"control_type": "ListItem"},
        {"control_type": "ListItem"},
    )
    assert loc.strategy == uia.STRATEGY_UNSTABLE
    assert loc.stable is False
    assert loc.value == ""


def test_unstable_reason_is_not_empty():
    """報告要能告訴使用者為什麼定位不了，否則他無從判斷替代方案。"""
    loc = locator_of({"control_type": "Custom"}, {"control_type": "Custom"})
    assert loc.reason.strip()


def test_node_without_control_type_is_unstable():
    """連型別都讀不到就沒有任何可用的定位依據。"""
    loc = locator_of({}, {})
    assert loc.strategy == uia.STRATEGY_UNSTABLE


def test_lone_nameless_node_is_not_unstable():
    """同層只有它一個同型控制項時，型別本身就是唯一識別。"""
    loc = locator_of({"control_type": "Document"})
    assert loc.strategy == uia.STRATEGY_INDEX
    assert loc.stable is True


# ── 樹的扁平化 ───────────────────────────────────────────────────────


def test_flatten_is_depth_first_preorder():
    root = uia.UiaNode(
        name="root",
        children=(
            uia.UiaNode(
                name="a",
                depth=1,
                sibling_index=0,
                children=(uia.UiaNode(name="a1", depth=2),),
            ),
            uia.UiaNode(name="b", depth=1, sibling_index=1),
        ),
    )
    assert [n.name for n in uia.flatten(root)] == ["root", "a", "a1", "b"]


def test_flatten_of_leaf_returns_single_node():
    """自繪畫布會是這種形狀：一個節點、沒有子節點，不能因此出錯。"""
    assert len(uia.flatten(node(control_type="Custom"))) == 1


def test_annotate_records_path_from_root():
    root = tree({"control_type": "Window"}, ({"control_type": "Edit"},))
    reports = uia.annotate(root)
    assert reports[0].path == "Window[0]"
    assert reports[1].path == "Window[0] > Edit[0]"


def test_annotate_keeps_depth_and_sibling_index():
    root = tree(
        {"control_type": "Window"},
        ({"control_type": "Button"}, {"control_type": "Edit"}),
    )
    reports = uia.annotate(root)
    assert [(r.node.depth, r.node.sibling_index) for r in reports] == [
        (0, 0),
        (1, 0),
        (1, 1),
    ]


def test_annotate_reports_child_count():
    root = tree({"control_type": "Window"}, ({"control_type": "Edit"},))
    assert uia.annotate(root)[0].child_count == 1
    assert uia.annotate(root)[1].child_count == 0


# ── 摘要統計 ─────────────────────────────────────────────────────────


def test_summary_counts_unstable_nodes():
    """對應 Scenario：報告摘要區列出所有 UNSTABLE 項目的總數。"""
    root = tree(
        {"control_type": "Window", "automation_id": "main"},
        (
            {"control_type": "ListItem"},
            {"control_type": "ListItem"},
            {"control_type": "Button", "automation_id": "ok"},
        ),
    )
    report = uia.build_report(root)
    assert report.summary.unstable_count == 2
    assert report.summary.total_nodes == 4


def test_summary_lists_unstable_paths():
    root = tree(
        {"control_type": "Window", "automation_id": "main"},
        ({"control_type": "Custom"}, {"control_type": "Custom"}),
    )
    paths = uia.build_report(root).summary.unstable_paths
    assert paths == ("Window[0] > Custom[0]", "Window[0] > Custom[1]")


def test_summary_counts_every_strategy():
    root = tree(
        {"control_type": "Window", "automation_id": "main"},
        (
            {"control_type": "Button", "name": "確定"},
            {"control_type": "Edit"},
            {"control_type": "Custom"},
            {"control_type": "Custom"},
        ),
    )
    counts = uia.build_report(root).summary.strategy_counts
    assert counts[uia.STRATEGY_AUTO_ID] == 1
    assert counts[uia.STRATEGY_NAME] == 1
    assert counts[uia.STRATEGY_INDEX] == 1
    assert counts[uia.STRATEGY_UNSTABLE] == 2


def test_summary_always_has_all_strategy_keys():
    """缺鍵會讓報告閱讀端要處理 KeyError，一律補 0。"""
    counts = uia.build_report(node(control_type="Window")).summary.strategy_counts
    assert set(counts) == {
        uia.STRATEGY_AUTO_ID,
        uia.STRATEGY_NAME,
        uia.STRATEGY_INDEX,
        uia.STRATEGY_UNSTABLE,
    }


def test_summary_reports_max_depth():
    root = uia.UiaNode(
        control_type="Window",
        children=(
            uia.UiaNode(
                control_type="Pane",
                depth=1,
                children=(uia.UiaNode(control_type="Edit", depth=2),),
            ),
        ),
    )
    assert uia.build_report(root).summary.max_depth == 2


def test_summary_counts_truncated_nodes():
    """被深度上限截斷的節點要能被看見，否則使用者會以為樹就是這麼淺。"""
    root = tree(
        {"control_type": "Window"},
        ({"control_type": "Pane", "truncated": True}, {"control_type": "Edit"}),
    )
    assert uia.build_report(root).summary.truncated_count == 1


def test_unstable_path_listing_is_capped():
    """摘要要能被人讀完；總數才是決策依據，清單只是樣本。"""
    kids = tuple({"control_type": "Custom"} for _ in range(uia.MAX_LISTED_UNSTABLE + 5))
    report = uia.build_report(tree({"control_type": "Window"}, kids))
    assert report.summary.unstable_count == uia.MAX_LISTED_UNSTABLE + 5
    assert len(report.summary.unstable_paths) == uia.MAX_LISTED_UNSTABLE


# ── 選取狀態可讀性 ───────────────────────────────────────────────────


def test_selection_readable_when_items_are_actually_read():
    """對應 Scenario「清單支援讀取選取項」：要列出讀到的名稱供使用者比對。"""
    probe = uia.SelectionProbe(supported=True, items=("A-1234", "A-1235"))
    result = uia.evaluate_selection(probe)
    assert result.readable is True
    assert result.items == ("A-1234", "A-1235")


def test_selection_not_readable_when_pattern_absent():
    """對應 Scenario「清單不支援讀取選取項」。"""
    result = uia.evaluate_selection(uia.SelectionProbe(supported=False))
    assert result.readable is False


def test_unsupported_selection_hint_points_to_explicit_list():
    """讀不到時要直接告訴使用者退路是什麼，否則他只會看到一個 false。"""
    result = uia.evaluate_selection(uia.SelectionProbe(supported=False))
    assert "models" in result.hint
    assert "清單" in result.hint


def test_selection_probe_error_is_not_readable():
    """讀取時丟例外一律當成讀不到——安全方向的預設。"""
    result = uia.evaluate_selection(
        uia.SelectionProbe(supported=True, items=("A-1",), error="COM 呼叫失敗")
    )
    assert result.readable is False
    assert "COM 呼叫失敗" in result.hint


def test_supported_but_empty_selection_is_not_readable():
    """
    曝光 pattern 不等於讀得到。清單可能宣稱支援卻永遠回傳空集合，
    把「宣稱支援」當成證據，使用者會用 SELECTED 模式跑出零個 model
    而完全不知道為什麼。只有真的讀到項目才算證明。
    """
    result = uia.evaluate_selection(uia.SelectionProbe(supported=True, items=()))
    assert result.readable is False


def test_empty_selection_hint_asks_user_to_select_first():
    result = uia.evaluate_selection(uia.SelectionProbe(supported=True, items=()))
    assert "框選" in result.hint


def test_no_probe_at_all_is_not_readable():
    """沒指定清單控制項時不該樂觀地說可以用。"""
    result = uia.evaluate_selection(None)
    assert result.readable is False
    assert result.items == ()


def test_missing_pattern_and_read_failure_give_different_hints():
    """
    「沒有 Selection pattern」是個確定的答案（改用明確清單），
    「讀取時失敗」則值得再試一次。兩者混為一談會讓使用者做錯決定。
    """
    absent = uia.evaluate_selection(uia.SelectionProbe(supported=False))
    failed = uia.evaluate_selection(
        uia.SelectionProbe(supported=False, error="COM 呼叫失敗")
    )
    assert absent.hint != failed.hint


def test_exception_without_message_is_described_by_type_alone():
    """NoPatternInterfaceError 的訊息是空的；照原樣格式化會印出一個尾巴冒號。"""
    assert uia._describe_exception(ValueError()) == "ValueError"


def test_exception_message_is_kept_when_present():
    assert uia._describe_exception(ValueError("壞了")) == "ValueError: 壞了"


def test_selection_hint_shows_item_count_when_readable():
    result = uia.evaluate_selection(
        uia.SelectionProbe(supported=True, items=("A", "B", "C"))
    )
    assert "3" in result.hint


def test_report_surfaces_selection_readable_in_summary():
    root = node(control_type="Window", automation_id="main")
    report = uia.build_report(
        root, selection=uia.SelectionProbe(supported=True, items=("A-1",))
    )
    assert report.summary.selection_readable is True


def test_report_without_selection_defaults_to_false():
    report = uia.build_report(node(control_type="Window", automation_id="main"))
    assert report.summary.selection_readable is False


# ── 報告序列化（B3 會直接拿去寫檔）────────────────────────────────────


def test_report_to_dict_is_json_serializable():
    root = tree(
        {"control_type": "Window", "automation_id": "main", "name": "記事本"},
        ({"control_type": "Edit", "automation_id": "15"},),
    )
    data = uia.report_to_dict(
        uia.build_report(root, target="notepad", selection=uia.SelectionProbe())
    )
    # 報告必須是純文字可攜回的（spec: 報告可攜回）
    assert json.loads(json.dumps(data, ensure_ascii=False))["target"] == "notepad"


def test_report_dict_node_carries_every_required_field():
    """spec 明訂每個控制項至少含這幾個欄位。"""
    root = node(control_type="Window", automation_id="main")
    entry = uia.report_to_dict(uia.build_report(root))["nodes"][0]
    for field in (
        "name",
        "automation_id",
        "control_type",
        "class_name",
        "is_enabled",
        "depth",
        "sibling_index",
        "child_count",
        "path",
        "locator",
    ):
        assert field in entry, f"報告節點缺少欄位 {field}"


def test_report_dict_locator_carries_strategy_and_value():
    root = node(control_type="Window", automation_id="main")
    loc = uia.report_to_dict(uia.build_report(root))["nodes"][0]["locator"]
    assert loc["strategy"] == uia.STRATEGY_AUTO_ID
    assert loc["value"] == "main"


def test_report_dict_summary_has_selection_readable():
    data = uia.report_to_dict(uia.build_report(node(control_type="Window")))
    assert data["summary"]["selection_readable"] is False


def test_report_dict_is_enabled_is_a_real_bool():
    """JSON 端要拿到 true/false，不是 0/1 或字串。"""
    root = node(control_type="Window", is_enabled=True)
    assert uia.report_to_dict(uia.build_report(root))["nodes"][0]["is_enabled"] is True


# ── 從報告讀回節點（D1 在開發機重新評估用）────────────────────────────


def test_node_from_mapping_round_trips_through_json():
    root = tree(
        {"control_type": "Window", "automation_id": "main", "is_enabled": True},
        ({"control_type": "Edit", "name": "文字編輯區"},),
    )
    restored = uia.node_from_mapping(json.loads(json.dumps(uia.node_to_mapping(root))))
    assert restored == root


def test_node_from_mapping_tolerates_missing_fields():
    """報告日後多了欄位或少了欄位都不該讓重新評估整個掛掉。"""
    restored = uia.node_from_mapping({"control_type": "Button", "未知欄位": 1})
    assert restored.control_type == "Button"
    assert restored.children == ()


# ── 與 config 的約定 ─────────────────────────────────────────────────


def test_strategy_names_match_config_vocabulary():
    """
    報告裡的策略字串會被直接抄進 config.json 的 controls.strategy，
    兩邊用詞一旦漂移，使用者照著報告填的設定會被 config 驗證擋下來。
    """
    for strategy in (uia.STRATEGY_AUTO_ID, uia.STRATEGY_NAME, uia.STRATEGY_INDEX):
        assert strategy in config.VALID_STRATEGIES


def test_unstable_marker_is_not_a_valid_config_strategy():
    """UNSTABLE 不是可用的定位方式，不該能被填進設定檔。"""
    assert uia.STRATEGY_UNSTABLE not in config.VALID_STRATEGIES


# ── 分層守則（TD-3）──────────────────────────────────────────────────


def test_uia_does_not_import_pywinauto_at_module_level():
    """
    純函式層必須在沒有 pywinauto 的機器上也能 import，
    否則整份 test_locator.py 會連跑都跑不起來。
    """
    for line in (LIB / "uia.py").read_text(encoding="utf-8").splitlines():
        assert not line.startswith(("import pywinauto", "from pywinauto")), line


@pytest.mark.parametrize(
    "name",
    [
        "evaluate_locator",
        "evaluate_selection",
        "flatten",
        "annotate",
        "build_report",
        "report_to_dict",
        "node_from_mapping",
        "node_to_mapping",
    ],
)
def test_pure_api_is_exposed(name):
    assert callable(getattr(uia, name))
