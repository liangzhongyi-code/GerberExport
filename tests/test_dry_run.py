"""
`--dry-run`（TD-10）：只定位、不操作，回報缺哪幾個。

它的價值全在那份缺項清單上——使用者跑一次就知道要改哪幾個設定，而不是
把 700 個節點的探測報告帶回來讓人逐一比對。所以測試重點有三個：

  1. 一個失敗不能中斷其餘（否則得跑十次才問得完十個缺項）
  2. 視窗沒找到時，底下的項目是「未檢查」而不是「找不到」
  3. 缺幾個與全缺的下一步完全不同，訊息要分得出來

「絕不操作介面」不靠測試盯：check_controls 只收「找視窗」與「找控制項」
兩個函式，結構上就沒有東西可以按。
"""

from types import SimpleNamespace

import pytest

from lib import dryrun


def spec(strategy, value):
    return SimpleNamespace(strategy=strategy, value=value)


EXPLORER = {
    "window": spec("title_re", "AccuMark Explorer.*"),
    "model_list": spec("control_type", "List"),
    "menu_file": spec("name", "File"),
}
DCU = {
    "window": spec("title_re", "Data Conversion.*"),
    "file_type": spec("name", "File Type"),
}
CONTROLS = SimpleNamespace(explorer=EXPLORER, dcu=DCU)


def checker(*, missing_names=(), missing_windows=()):
    """
    假的定位器。missing_names 裡的控制項與 missing_windows 裡的視窗
    會拋錯，其餘都找得到。
    """
    calls = []

    def find_window_fn(s):
        calls.append(("window", s.value))
        if s.value in missing_windows:
            raise RuntimeError(f"找不到符合 {s.value} 的視窗")
        return SimpleNamespace(kind="window", value=s.value)

    def resolve_fn(window, s):
        calls.append(("resolve", s.value))
        if s.value in missing_names:
            raise RuntimeError(f"「{window.value}」底下找不到 {s.value}")
        return SimpleNamespace(kind="control")

    return find_window_fn, resolve_fn, calls


def run(**kw):
    find_window_fn, resolve_fn, calls = checker(**kw)
    results = dryrun.check_controls(
        CONTROLS, ("explorer", "dcu"), find_window_fn=find_window_fn, resolve_fn=resolve_fn
    )
    return results, calls


# ── 全部找到 ─────────────────────────────────────────────────────────


def test_everything_found_reports_success():
    results, _ = run()
    assert dryrun.missing(results) == ()
    assert dryrun.exit_code(results) == 0
    assert "可以開始跑批次" in "\n".join(dryrun.format_results(results))


def test_every_control_is_checked():
    results, calls = run()
    assert len(results) == len(EXPLORER) + len(DCU)
    assert ("resolve", "File") in calls


# ── 部分缺項 ─────────────────────────────────────────────────────────


def test_one_failure_does_not_stop_the_rest():
    """
    使用者要的是完整清單。第一個失敗就中斷的話，他得跑十次才問得完十個
    缺項——而每一次都要來回傳檔。
    """
    results, calls = run(missing_names=("List",))
    assert ("resolve", "File") in calls, "第一個失敗之後就不檢查了"
    assert [r.name for r in dryrun.missing(results)] == ["model_list"]


def test_missing_control_reports_how_it_was_searched():
    """缺項清單要能直接照著改設定檔，所以策略與值都要在。"""
    results, _ = run(missing_names=("File",))
    body = "\n".join(dryrun.format_results(results))
    assert "menu_file" in body
    assert "name" in body and "File" in body


def test_partial_failure_is_non_zero():
    results, _ = run(missing_names=("File",))
    assert dryrun.exit_code(results) == 1


def test_a_few_missing_suggests_language_difference():
    """視窗都在、只缺幾個控制項 → 多半是語系或版本差異，改設定就好。"""
    results, _ = run(missing_names=("File",))
    assert "語系" in "\n".join(dryrun.format_results(results))


# ── 視窗沒找到 ───────────────────────────────────────────────────────


def test_missing_window_still_checks_the_other_group():
    """DCU 沒開不該讓 Explorer 那一半也沒得檢查。"""
    results, calls = run(missing_windows=("Data Conversion.*",))
    assert ("resolve", "File") in calls
    explorer_found = [r for r in results if r.group == "explorer" and r.found]
    assert len(explorer_found) == len(EXPLORER)


def test_controls_under_a_missing_window_are_marked_unchecked():
    """
    「沒檢查」與「找不到」是不同的結論。混為一談，使用者會以為要改一堆
    設定，其實只要把那個程式打開。
    """
    results, _ = run(missing_windows=("Data Conversion.*",))
    file_type = next(r for r in results if r.name == "file_type")
    assert file_type.found is False
    assert "沒有檢查" in file_type.detail


def test_missing_window_does_not_try_to_resolve_under_it():
    results, calls = run(missing_windows=("Data Conversion.*",))
    assert ("resolve", "File Type") not in calls


def test_unchecked_items_are_not_listed_as_missing():
    """
    實測抓到的：DCU 沒開時，摘要把底下 5 個「沒檢查」的項目一起列進
    「定位不到」，使用者看到「15 個裡有 15 個找不到」——那句話會讓他以為
    每一個設定都要改，實際上只要把程式打開。

    未檢查與找不到是不同的結論，摘要要分開講。
    """
    results, _ = run(missing_windows=("Data Conversion.*",))
    assert [r.name for r in dryrun.missing(results)] == ["window"]
    assert len(dryrun.unchecked(results)) == len(DCU) - 1


def test_summary_counts_unchecked_separately():
    results, _ = run(missing_windows=("Data Conversion.*",))
    body = "\n".join(dryrun.format_results(results))
    assert "未檢查" in body
    # 「找不到」的數量只能算真的檢查過的那一個
    assert "有 1 個" in body or "1 個定位不到" in body


def test_unchecked_alone_still_counts_as_failure():
    """
    沒驗完就不能說沒問題。

    這裡直接構造結果而不走 check_controls：真實流程裡「有未檢查」必然
    伴隨「視窗找不到」，於是 exit_code 靠 missing 就會回 1——那樣這條
    測試會因為錯誤的理由通過，而 `or unchecked(...)` 拿掉也不會變紅。
    突變檢查抓到了這一點。
    """
    only_unchecked = (
        dryrun.CheckResult("dcu", "window", "title_re", "X", True),
        dryrun.CheckResult("dcu", "file_type", "name", "Y", False, "未檢查", checked=False),
    )
    assert dryrun.missing(only_unchecked) == ()
    assert dryrun.exit_code(only_unchecked) == 1


def test_missing_window_advice_points_at_opening_the_program():
    results, _ = run(missing_windows=("Data Conversion.*",))
    body = "\n".join(dryrun.format_results(results))
    assert "最小化" in body or "開著" in body


# ── 全缺：唯一會讓方案翻船的訊號 ─────────────────────────────────────


def test_everything_missing_names_the_self_drawn_risk():
    """
    一個都定位不到，代表 AccuMark 的介面可能是自繪的、UIA 看不見它——
    那是整個方案唯一的致命未知。這句話一定要出現，否則使用者會以為
    只是設定沒填好，繼續在錯的方向上花時間。
    """
    results, _ = run(
        missing_windows=("AccuMark Explorer.*", "Data Conversion.*"),
    )
    body = "\n".join(dryrun.format_results(results))
    assert "自繪" in body
    assert dryrun.exit_code(results) == 1


def test_everything_missing_also_offers_the_boring_explanation():
    """先講「程式沒開」——那比「方案翻船」常見得多，別嚇到使用者。"""
    results, _ = run(missing_windows=("AccuMark Explorer.*", "Data Conversion.*"))
    body = "\n".join(dryrun.format_results(results))
    assert "沒開" in body
    assert body.index("沒開") < body.index("自繪")


def test_everything_missing_mentions_the_language_possibility():
    """
    AccuMark 有中文版介面。中文版連視窗標題都可能不是 `AccuMark Explorer`，
    於是兩個視窗都找不到、底下全部未檢查——外觀與「介面自繪」完全一樣。

    但兩者的下一步天差地遠：語系只要改設定檔的十幾行字，自繪則是整個方案
    要換做法。漏掉語系這個可能，使用者會以為方案沒救而放棄，實際上五分鐘
    就能解決。
    """
    results, _ = run(missing_windows=("AccuMark Explorer.*", "Data Conversion.*"))
    body = "\n".join(dryrun.format_results(results))
    assert "語系" in body or "中文" in body
    # 語系比自繪常見，要排在前面
    idx_lang = body.index("語系") if "語系" in body else body.index("中文")
    assert idx_lang < body.index("自繪")


def test_everything_missing_tells_how_to_find_the_real_titles():
    """
    語系不同時，使用者需要知道「正確的視窗標題是什麼」——而那正是
    1_執行探測.bat 會列出來的東西。不指路的話，他只能回報「全都找不到」，
    然後我們再來回問一次。
    """
    results, _ = run(missing_windows=("AccuMark Explorer.*", "Data Conversion.*"))
    body = "\n".join(dryrun.format_results(results))
    assert "1_執行探測" in body


# ── 結構保證 ─────────────────────────────────────────────────────────


# ── 選取狀態：決定要不要每次手動維護清單 ─────────────────────────────
#
# 「在 Explorer 框選幾個 model，雙擊就跑」這件事能不能成立，取決於
# AccuMark 的清單控制項有沒有把「目前選了哪些」對外曝光。那是 UIA 的
# SelectionPattern，只有在真機上問過才知道。
#
# 原本 dry-run 只檢查 model_list「找不找得到」——找得到不代表讀得到選取。
# 少了這一問，對方會在 2e 全綠之後跑批次，才在啟動檢查那裡撞到，然後
# 得再跑一趟。dry-run 的價值就是一次問完。


class FakeProbe:
    def __init__(self, supported, items=(), error=None):
        self.supported = supported
        self.items = tuple(items)
        self.error = error


def test_selection_readable_with_items():
    check = dryrun.check_selection(object(), lambda c: FakeProbe(True, ("A-1234", "A-1235")))
    assert check.supported is True
    assert check.items == ("A-1234", "A-1235")
    body = "\n".join(dryrun.format_selection(check))
    assert "A-1234" in body
    assert "不用" in body or "免" in body


def test_selection_supported_but_nothing_chosen():
    """
    三種結論要有三種說法：有選、支援但沒選、不支援。

    斷言刻意避開「框選」二字——「有選」那一段也含它，用 or 串起來的話，
    把兩個分支合併掉測試照樣是綠的（突變檢查抓到過一次）。改成釘住那句
    怪訊息不能出現：沒選就是沒選，不是「選了 0 項」。
    """
    check = dryrun.check_selection(object(), lambda c: FakeProbe(True, ()))
    assert check.supported is True
    body = "\n".join(dryrun.format_selection(check))
    assert "0 項" not in body, "「沒有選取」不該說成「選了 0 項」"
    assert "沒有" in body
    assert "明確清單" not in body, "支援卻叫人改設定檔，那是把可用的功能講成壞掉"


def test_the_three_selection_verdicts_read_differently():
    """三種結論的下一步不同，文字就不能長得一樣。"""
    chosen = "\n".join(dryrun.format_selection(dryrun.SelectionCheck(True, ("A-1234",))))
    empty = "\n".join(dryrun.format_selection(dryrun.SelectionCheck(True, ())))
    unsupported = "\n".join(dryrun.format_selection(dryrun.SelectionCheck(False)))
    assert len({chosen, empty, unsupported}) == 3


def test_selection_not_supported_points_at_the_explicit_list():
    """讀不到就得退回明確清單——那是使用者要知道的替代方案，不是死路。"""
    check = dryrun.check_selection(object(), lambda c: FakeProbe(False))
    assert check.supported is False
    body = "\n".join(dryrun.format_selection(check))
    assert "明確清單" in body
    assert "models" in body


def test_selection_probe_error_is_reported_not_swallowed():
    check = dryrun.check_selection(object(), lambda c: FakeProbe(False, error="COM 錯誤 0x80004005"))
    assert "0x80004005" in "\n".join(dryrun.format_selection(check))


def test_selection_check_survives_a_throwing_probe():
    """這是輔助資訊，它自己壞掉不該讓整個 dry-run 失敗。"""

    def boom(ctrl):
        raise RuntimeError("讀取時炸了")

    check = dryrun.check_selection(object(), boom)
    assert check.supported is False
    assert "讀取時炸了" in "\n".join(dryrun.format_selection(check))


def test_selection_result_does_not_change_the_exit_code():
    """
    讀不到選取狀態不是「設定壞了」，是「要換一種用法」。把它算進失敗會讓
    使用者以為還有東西要修，而其實只要改 models 欄位就能開始跑。
    """
    results, _ = run()
    assert dryrun.exit_code(results) == 0


def test_check_controls_needs_nothing_but_two_lookups():
    """
    「絕不操作介面」不是靠自律，是靠介面：check_controls 收不到任何
    可以按下去的東西。這條測試把那個契約釘住——有人日後想多傳一個 ops
    進來，會先撞到這裡。
    """
    import inspect

    params = set(inspect.signature(dryrun.check_controls).parameters)
    assert params == {"controls", "groups", "find_window_fn", "resolve_fn"}
