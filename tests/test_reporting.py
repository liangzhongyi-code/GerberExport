"""
A2 日誌與摘要測試（對應 spec: operability「結構化日誌」）。

時間一律由呼叫端傳入字串，模組本身不看時鐘——否則測試就得對付
「現在幾點」這種不可控的輸入，而這裡真正要驗的是統計與分類邏輯。
"""

from pathlib import Path

import pytest

from lib import reporting as rep
from lib.reporting import Status

T0 = "2026-08-30T14:30:12"
T1 = "2026-08-30T14:30:47"


def rec(model="A-1234", fmt="AAMA", status=Status.SUCCESS, outputs=(), detail=""):
    return rep.TaskRecord(
        model=model,
        fmt=fmt,
        status=status,
        started_at=T0,
        finished_at=T1,
        outputs=tuple(outputs),
        detail=detail,
    )


def many(status, n, fmt="AAMA"):
    return [rec(model=f"M-{i}", fmt=fmt, status=status) for i in range(n)]


def text_of(lines):
    return "\n".join(lines)


# ── 狀態分類 ─────────────────────────────────────────────────────────


def test_every_status_in_design_doc_exists():
    """design.md §4.3 列了九種狀態，少一種就代表某條路徑無處可歸。"""
    expected = {
        "SUCCESS",
        "SKIPPED_ALREADY_DONE",
        "SKIPPED_NOT_FOUND",
        "FAILED_SELECTION",
        "FAILED_UI",
        "FAILED_TIMEOUT",
        "FAILED_TARGET_EXISTS",
        "FAILED_MOVE",
        "HALTED_UNKNOWN_DIALOG",
    }
    assert {s.name for s in Status} == expected


@pytest.mark.parametrize(
    "status,is_problem",
    [
        (Status.SUCCESS, False),
        (Status.SKIPPED_ALREADY_DONE, False),
        (Status.SKIPPED_NOT_FOUND, True),
        (Status.FAILED_SELECTION, True),
        (Status.FAILED_TIMEOUT, True),
        (Status.FAILED_TARGET_EXISTS, True),
        (Status.FAILED_MOVE, True),
        (Status.HALTED_UNKNOWN_DIALOG, True),
    ],
)
def test_problem_classification(status, is_problem):
    """
    SKIPPED_NOT_FOUND 算問題：使用者指名要處理的 model 找不到，
    靜默跳過會讓他以為東西都出好了。

    FAILED_SELECTION 也算問題：那個 model 的 DXF 根本沒做，結束碼若是 0，
    使用者會把少一個檔案的資料夾當成完整的交出去。
    """
    assert status.is_problem is is_problem


@pytest.mark.parametrize(
    "status,aborts",
    [
        (Status.SUCCESS, False),
        (Status.SKIPPED_NOT_FOUND, False),
        (Status.FAILED_SELECTION, False),
        (Status.FAILED_TIMEOUT, False),
        (Status.FAILED_TARGET_EXISTS, False),
        (Status.FAILED_MOVE, True),
        (Status.HALTED_UNKNOWN_DIALOG, True),
    ],
)
def test_abort_classification(status, aborts):
    """
    只有兩種狀態該中止整批：搬檔失敗（磁碟／權限有問題，繼續也是白搭）
    與未知對話框（TD-5：不確定畫面上是什麼，就絕不繼續亂按）。

    FAILED_SELECTION 刻意不中止：它發生在觸發之前、什麼都沒動，
    下一個 model 重選一次很可能就正常；為了一次選取殘留停掉整批太浪費。
    """
    assert status.aborts_batch is aborts


@pytest.mark.parametrize(
    "status,is_skip",
    [
        (Status.SUCCESS, False),
        (Status.SKIPPED_ALREADY_DONE, True),
        (Status.SKIPPED_NOT_FOUND, True),
        (Status.FAILED_SELECTION, False),
        (Status.FAILED_TIMEOUT, False),
        (Status.FAILED_TARGET_EXISTS, False),
        (Status.FAILED_MOVE, False),
        (Status.HALTED_UNKNOWN_DIALOG, False),
    ],
)
def test_skip_classification(status, is_skip):
    """
    FAILED_SELECTION 雖然「沒有執行」，但不是跳過——跳過是流程的決定，
    選取殘留是流程的失敗。混在一起，續跑時會把它當成不必補做的項目。
    """
    assert status.is_skip is is_skip


@pytest.mark.parametrize("status", list(Status))
def test_every_status_has_a_chinese_description(status):
    """
    摘要用 DESCRIPTIONS 把狀態翻成人話；漏了哪一種，那一行會印出英文代碼，
    打版師看不懂就只能來問。新增狀態最容易忘的就是這張表。
    """
    assert status in rep.DESCRIPTIONS
    assert rep.DESCRIPTIONS[status].strip()


# ── 摘要統計 ─────────────────────────────────────────────────────────


def test_all_success():
    s = rep.summarize(many(Status.SUCCESS, 12))
    assert (s.total, s.succeeded, s.failed) == (12, 12, 0)
    assert s.exit_code == 0
    assert s.aborted is False


def test_all_success_summary_wording():
    lines = rep.format_summary(rep.summarize(many(Status.SUCCESS, 12)))
    assert "成功 12" in text_of(lines)
    assert "失敗 0" in text_of(lines)


def test_partial_failure_counts():
    records = many(Status.SUCCESS, 10) + [
        rec(model="M-A", status=Status.FAILED_TIMEOUT),
        rec(model="M-B", status=Status.FAILED_TARGET_EXISTS),
    ]
    s = rep.summarize(records)
    assert (s.total, s.succeeded, s.failed) == (12, 10, 2)
    assert s.exit_code != 0


def test_partial_failure_lists_each_reason():
    """規格要求逐條列出失敗任務與原因，只給數字沒辦法處理。"""
    records = many(Status.SUCCESS, 10) + [
        rec(model="M-A", fmt="ZIP", status=Status.FAILED_TIMEOUT, detail="逾時 300 秒"),
        rec(model="M-B", fmt="ASTM", status=Status.FAILED_MOVE, detail="磁碟空間不足"),
    ]
    body = text_of(rep.format_summary(rep.summarize(records)))
    assert "成功 10" in body and "失敗 2" in body
    for token in ("M-A", "ZIP", "逾時 300 秒", "M-B", "ASTM", "磁碟空間不足"):
        assert token in body, f"摘要未提到 {token}"


def test_failed_selection_counts_as_failure_without_aborting():
    """
    spec batch-export「DCU 選取殘留多項」：任務標記 FAILED_SELECTION、
    日誌記錄讀到的全部項目、腳本繼續下一個任務。

    三件事分開驗：它得進失敗數（不是跳過數）、不得把整批標成中止、
    摘要得把讀回的項目印出來——那是使用者判斷「殘留從哪來」的唯一線索。
    """
    records = many(Status.SUCCESS, 5) + [
        rec(
            model="M-B",
            fmt="ASTM",
            status=Status.FAILED_SELECTION,
            detail="讀回選取：M-A, M-B",
        )
    ]
    s = rep.summarize(records)
    assert (s.total, s.succeeded, s.skipped, s.failed) == (6, 5, 0, 1)
    assert s.aborted is False
    assert s.exit_code != 0

    body = text_of(rep.format_summary(s))
    assert "M-B" in body and "ASTM" in body
    assert "M-A, M-B" in body
    assert "中止" not in body


def test_skipped_already_done_is_not_a_failure():
    """續跑時前幾個被跳過是正常現象，不該讓整批看起來失敗。"""
    records = many(Status.SKIPPED_ALREADY_DONE, 6) + many(Status.SUCCESS, 6)
    s = rep.summarize(records)
    assert s.failed == 0
    assert s.skipped == 6
    assert s.exit_code == 0


def test_skipped_not_found_forces_nonzero_exit():
    records = many(Status.SUCCESS, 9) + many(Status.SKIPPED_NOT_FOUND, 3)
    s = rep.summarize(records)
    assert s.exit_code != 0


def test_abort_is_flagged():
    records = many(Status.SUCCESS, 3) + [rec(status=Status.HALTED_UNKNOWN_DIALOG)]
    s = rep.summarize(records)
    assert s.aborted is True
    assert s.exit_code != 0


def test_abort_summary_says_batch_stopped():
    records = many(Status.SUCCESS, 3) + [
        rec(status=Status.HALTED_UNKNOWN_DIALOG, detail="未知視窗：授權提醒")
    ]
    body = text_of(rep.format_summary(rep.summarize(records)))
    assert "中止" in body
    assert "授權提醒" in body


def test_empty_run_is_not_silent_success():
    """
    一個任務都沒跑通常代表出了問題（例如沒選取任何 model）。
    回 0 會讓使用者以為都好了。
    """
    s = rep.summarize([])
    assert s.total == 0
    assert s.exit_code != 0
    assert "沒有任何任務" in text_of(rep.format_summary(s))


# ── 單筆記錄格式 ─────────────────────────────────────────────────────


def test_record_line_contains_required_fields():
    """規格：逐筆記錄 model、格式、開始時間、結束時間、狀態、產出清單。"""
    r = rec(outputs=("C:\\out\\A-1234.dxf",))
    line = rep.format_record(r)
    for token in ("A-1234", "AAMA", T0, T1, "SUCCESS", "A-1234.dxf"):
        assert token in line, f"記錄行未包含 {token}"


def test_record_line_is_single_line():
    """一筆一行，方便用記事本或 grep 掃過去。"""
    r = rec(outputs=("C:\\out\\a.dxf", "C:\\out\\b.rul"), detail="含換行\n的細節")
    assert "\n" not in rep.format_record(r)


def test_record_with_multiple_outputs_lists_all():
    r = rec(outputs=("C:\\out\\a.dxf", "C:\\out\\a.rul"))
    line = rep.format_record(r)
    assert "a.dxf" in line and "a.rul" in line


# ── 寫檔 ─────────────────────────────────────────────────────────────


def test_log_written_as_utf8(tmp_path):
    """中文 model 名稱與錯誤訊息必須能被記事本正確開啟。"""
    path = tmp_path / "run.log"
    rep.write_log(path, [rec(model="外套-左前片", detail="測試中文")])
    raw = path.read_bytes()
    assert "外套-左前片".encode("utf-8") in raw
    assert raw.decode("utf-8")  # 不應拋出


def test_log_contains_records_and_summary(tmp_path):
    path = tmp_path / "run.log"
    records = many(Status.SUCCESS, 2) + [rec(model="M-X", status=Status.FAILED_TIMEOUT)]
    rep.write_log(path, records)
    body = path.read_text(encoding="utf-8")
    assert "M-X" in body
    assert "成功 2" in body and "失敗 1" in body


def test_log_creates_parent_directory(tmp_path):
    path = tmp_path / "_log" / "nested" / "run.log"
    rep.write_log(path, [rec()])
    assert path.is_file()


def test_write_log_returns_exit_code(tmp_path):
    """呼叫端直接拿它當結束碼，不用自己再算一次。"""
    ok = rep.write_log(tmp_path / "a.log", many(Status.SUCCESS, 3))
    bad = rep.write_log(tmp_path / "b.log", [rec(status=Status.FAILED_MOVE)])
    assert ok == 0
    assert bad != 0
