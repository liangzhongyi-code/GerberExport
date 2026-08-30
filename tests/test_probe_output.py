"""
B3 探測進入點測試（對應 spec: ui-probe「控制項樹匯出」「報告可攜回」）。

流程控制與錯誤處理以注入方式測試：探測本身（probe_fn）與列出候選視窗
（list_windows_fn）都是傳進來的，所以「AccuMark 沒開」「pywinauto 沒裝」
這些情境不用真的去製造，就能驗到腳本的反應。

最要緊的一條是「找不到目標時絕不產生報告檔」。一份看起來正常、其實什麼
都沒探到的報告，會讓整個期二依著錯誤的假設寫下去——那比直接失敗糟糕
得多。
"""

from datetime import datetime
from pathlib import Path

import pytest

import probe_ui
from lib import uia

WHEN = datetime(2026, 8, 30, 14, 30)


def fake_report(target="記事本"):
    root = uia.UiaNode(
        name="視窗",
        automation_id="root",
        control_type="Window",
        class_name="Notepad",
        is_enabled=True,
        depth=0,
        sibling_index=0,
        children=(),
    )
    return uia.build_report(root, target=target)


def run(**kw):
    """預設全部成功，各測試覆寫需要的部分。"""
    printed = []
    written = {}

    def default_write(path, text):
        written[Path(path)] = text

    params = dict(
        mode="window",
        out_dir=Path("out"),
        probe_fn=lambda: fake_report(),
        list_windows_fn=lambda: (),
        write_fn=default_write,
        now=lambda: WHEN,
        echo=printed.append,
        title_pattern="AccuMark.*",
    )
    params.update(kw)
    code = probe_ui.run(**params)
    return code, printed, written


def text_of(printed):
    return "\n".join(printed)


# ── 報告檔名與位置 ───────────────────────────────────────────────────


def test_report_filename_has_timestamp_and_mode():
    p = probe_ui.report_path(Path("out"), "window", WHEN)
    assert p.name == "probe_window_260830_143000.json"
    assert p.parent == Path("out")


def test_dialog_mode_uses_its_own_filename():
    """兩次探測不能互相覆蓋——使用者要把兩份都帶回來。"""
    a = probe_ui.report_path(Path("out"), "window", WHEN)
    b = probe_ui.report_path(Path("out"), "dialog", WHEN)
    assert a != b
    assert "dialog" in b.name


def test_two_runs_in_the_same_minute_do_not_collide():
    """
    時間戳要含秒。第一次標題打錯、改一改再跑，很可能就在同一分鐘內——
    只到分鐘的話第二次會靜靜覆蓋第一次，而使用者不會發現自己少帶了
    一份報告回來。
    """
    a = probe_ui.report_path(Path("out"), "window", datetime(2026, 8, 30, 14, 30, 5))
    b = probe_ui.report_path(Path("out"), "window", datetime(2026, 8, 30, 14, 30, 47))
    assert a != b


# ── 成功路徑 ─────────────────────────────────────────────────────────


def test_success_writes_report_and_returns_zero():
    code, printed, written = run()
    assert code == 0
    assert len(written) == 1


def test_success_prints_full_path():
    """使用者要照著這行路徑去找檔案帶回來，印相對路徑他會找不到。"""
    code, printed, written = run(out_dir=Path(r"C:\交付包\scripts\probe-output"))
    target = str(list(written)[0])
    assert target in text_of(printed)
    assert Path(target).is_absolute()


def test_report_is_valid_json():
    import json

    code, printed, written = run()
    data = json.loads(list(written.values())[0])
    assert "summary" in data and "nodes" in data


def test_report_json_keeps_chinese_readable():
    """報告要能直接用記事本打開看，逸出成 \\uXXXX 就沒人讀得懂。"""
    code, printed, written = run(probe_fn=lambda: fake_report(target="外套-左前片"))
    assert "外套-左前片" in list(written.values())[0]


def test_summary_is_echoed_so_user_can_report_back():
    """
    使用者不會打開 JSON 看。畫面上要直接講出關鍵結論。

    比對完整的「選取狀態可讀取」而不只是「選取」兩字：hint 文字裡也含
    那兩個字，只比對片段的話，結論那一行被拿掉了測試照樣是綠的。
    """
    code, printed, written = run()
    body = text_of(printed)
    assert "節點數" in body
    assert "選取狀態可讀取" in body, (
        "沒有直接講出 selection_readable 的結論——"
        "那決定 config.json 的 models 能不能用 SELECTED 模式"
    )
    assert "定位策略分佈" in body


# ── 探錯對象：實測抓到的真實缺陷 ─────────────────────────────────────
#
# 開發機上沒有 AccuMark，但 --title "AccuMark.*" 卻探測成功——它匹配到了
# Chrome 開著的一份標題以 AccuMark 開頭的 HTML 文件。報告上顯示「探測目標：
# AccuMark.*」，那是搜尋條件，不是實際抓到的視窗，使用者完全無從察覺。
#
# 在目標機上，桌面只要有任何標題含 AccuMark 的視窗（檔案總管開著資料夾、
# 一份說明文件），就會探錯對象並產出一份看起來正常的報告。


def test_matching_titles_finds_all_candidates():
    """純函式：同一個 pattern 匹配到幾個視窗。"""
    titles = ("AccuMark Explorer", "AccuMark 說明 - Chrome", "記事本")
    assert len(probe_ui.matching_titles(titles, "AccuMark.*")) == 2


def test_matching_titles_anchors_at_start():
    """與 pywinauto 的 title_re 一致：從開頭比對，不是任意位置。"""
    assert probe_ui.matching_titles(("我的 AccuMark 筆記",), "AccuMark.*") == ()


def test_matching_titles_survives_bad_pattern():
    """使用者可能打出不合法的正規表示式，不該讓整支腳本崩潰。"""
    assert probe_ui.matching_titles(("任何視窗",), "[unclosed") == ()


def test_summary_shows_the_window_actually_captured():
    """
    報告與畫面要講「實際抓到誰」，不是「我搜尋了什麼」。
    只印搜尋條件的話，探錯對象時使用者看不出任何異狀。
    """
    code, printed, written = run(
        probe_fn=lambda: fake_report(target="AccuMark 說明 - Google Chrome")
    )
    assert "AccuMark 說明 - Google Chrome" in text_of(printed)


def test_warns_when_more_than_one_window_matches():
    """
    多個視窗匹配同一個條件時，pywinauto 抓到哪一個並不確定。
    這種情況必須警告，否則探錯了也不會有人知道。
    """
    code, printed, written = run(
        list_windows_fn=lambda: ("AccuMark Explorer", "AccuMark 說明 - Chrome"),
        title_pattern="AccuMark.*",
    )
    body = text_of(printed)
    assert "不只一個" in body or "多個" in body
    assert "AccuMark 說明 - Chrome" in body


def test_no_warning_when_match_is_unambiguous():
    code, printed, written = run(
        list_windows_fn=lambda: ("AccuMark Explorer", "記事本"),
        title_pattern="AccuMark.*",
    )
    body = text_of(printed)
    assert "不只一個" not in body and "多個視窗" not in body


def test_ambiguity_warning_does_not_block_the_report():
    """警告歸警告，報告還是要產出——使用者可能就是要那一個。"""
    code, printed, written = run(
        list_windows_fn=lambda: ("AccuMark A", "AccuMark B"),
        title_pattern="AccuMark.*",
    )
    assert code == 0
    assert len(written) == 1


# ── 找不到目標：最要緊的路徑 ─────────────────────────────────────────


def boom_not_found():
    raise uia.WindowNotFoundError("找不到符合 {'title_re': 'AccuMark.*'} 的視窗")


def test_window_not_found_returns_nonzero():
    code, printed, written = run(probe_fn=boom_not_found)
    assert code != 0


def test_window_not_found_writes_no_report():
    """
    規格明訂：MUST NOT 產生空白或誤導性的報告檔。
    一份看起來正常卻什麼都沒探到的報告，會讓期二整個建在錯誤假設上。
    """
    code, printed, written = run(probe_fn=boom_not_found)
    assert written == {}, "找不到目標卻還是寫了報告檔"


def test_window_not_found_explains_why():
    code, printed, written = run(probe_fn=boom_not_found)
    assert "找不到" in text_of(printed)


def test_window_not_found_lists_candidates():
    """
    第一次探測時沒人知道 AccuMark 的視窗叫什麼。列出畫面上現有的視窗，
    使用者才能告訴我們正確的名稱——否則只能反覆猜。
    """
    code, printed, written = run(
        probe_fn=boom_not_found,
        list_windows_fn=lambda: ("AccuMark Explorer", "記事本", "Chrome"),
    )
    body = text_of(printed)
    for w in ("AccuMark Explorer", "記事本", "Chrome"):
        assert w in body


def test_candidate_listing_failure_does_not_mask_the_real_error():
    """列候選只是輔助，它自己壞掉不該蓋掉原本的錯誤訊息。"""

    def broken():
        raise RuntimeError("列舉視窗失敗")

    code, printed, written = run(probe_fn=boom_not_found, list_windows_fn=broken)
    assert code != 0
    assert "找不到" in text_of(printed)


def test_no_visible_windows_is_stated_plainly():
    code, printed, written = run(probe_fn=boom_not_found, list_windows_fn=lambda: ())
    assert code != 0
    assert written == {}


# ── pywinauto 沒裝 ───────────────────────────────────────────────────


def test_missing_pywinauto_points_at_the_env_check():
    """不要丟 traceback，把人導回 0_檢查環境.bat。"""

    def boom():
        raise uia.PywinautoMissingError("找不到 pywinauto")

    code, printed, written = run(probe_fn=boom)
    assert code != 0
    assert "0_檢查環境.bat" in text_of(printed)
    assert written == {}


# ── 未預期的錯誤 ─────────────────────────────────────────────────────


def test_unexpected_error_still_fails_cleanly():
    """任何沒想到的例外都不該把 traceback 噴在使用者臉上。"""

    def boom():
        raise ValueError("某個沒想到的狀況")

    code, printed, written = run(probe_fn=boom)
    assert code != 0
    assert written == {}
    assert "某個沒想到的狀況" in text_of(printed)


# ── 命令列參數 ───────────────────────────────────────────────────────


def test_mode_defaults_to_window():
    assert probe_ui.parse_args([]).mode == "window"


def test_dialog_mode_flag():
    assert probe_ui.parse_args(["--mode", "dialog"]).mode == "dialog"


def test_title_can_be_overridden():
    """
    第一次探測時我們不知道 AccuMark 的視窗標題，使用者可能要試幾次。
    讓他不用改程式碼就能試。
    """
    args = probe_ui.parse_args(["--title", "AccuMark.*"])
    assert args.title == "AccuMark.*"


def test_max_depth_can_be_limited():
    assert probe_ui.parse_args(["--max-depth", "3"]).max_depth == 3
