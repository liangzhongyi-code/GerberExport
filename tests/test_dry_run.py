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


# ── 結構保證 ─────────────────────────────────────────────────────────


def test_check_controls_needs_nothing_but_two_lookups():
    """
    「絕不操作介面」不是靠自律，是靠介面：check_controls 收不到任何
    可以按下去的東西。這條測試把那個契約釘住——有人日後想多傳一個 ops
    進來，會先撞到這裡。
    """
    import inspect

    params = set(inspect.signature(dryrun.check_controls).parameters)
    assert params == {"controls", "groups", "find_window_fn", "resolve_fn"}
