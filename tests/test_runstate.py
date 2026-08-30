"""
C3 續跑判定測試（對應 spec: operability「中斷後續跑」）。

should_skip 的存在性檢查以 exists_fn 注入，因此「狀態檔說成功但檔案被
刪掉了」這種情境不用真的去刪檔案就能測。

核心規則：**狀態檔說成功還不夠，產出檔案要真的還在。** 只信狀態檔的話，
使用者手動清掉輸出資料夾之後重跑，會拿到一個什麼都沒做卻宣稱成功的批次。
"""

import json

import pytest

from lib import runstate as rs
from lib.reporting import Status, TaskRecord

T0, T1 = "2026-08-30T14:30:12", "2026-08-30T14:30:47"


def rec(model, fmt, status=Status.SUCCESS, outputs=()):
    return TaskRecord(
        model=model,
        fmt=fmt,
        status=status,
        started_at=T0,
        finished_at=T1,
        outputs=tuple(outputs),
    )


def state_with(*pairs):
    """pairs: (model, fmt, outputs)"""
    return rs.merge(
        rs.EMPTY, [rec(m, f, outputs=o) for m, f, o in pairs]
    )


ALL_EXIST = lambda p: True
NONE_EXIST = lambda p: False


# ── 基本判定 ─────────────────────────────────────────────────────────


def test_unknown_task_is_not_skipped():
    assert rs.should_skip(rs.EMPTY, "A-1234", "AAMA", ALL_EXIST) is False


def test_completed_task_is_skipped():
    st = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf",)))
    assert rs.should_skip(st, "A-1234", "AAMA", ALL_EXIST) is True


def test_same_model_different_format_is_not_skipped():
    st = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf",)))
    assert rs.should_skip(st, "A-1234", "ASTM", ALL_EXIST) is False


def test_different_model_same_format_is_not_skipped():
    st = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf",)))
    assert rs.should_skip(st, "A-9999", "AAMA", ALL_EXIST) is False


# ── 產出必須真的還在 ─────────────────────────────────────────────────


def test_missing_output_means_rerun():
    """
    狀態檔說成功但檔案被刪掉了，就得重做。
    只信狀態檔的話，使用者清掉輸出資料夾後重跑會拿到一個什麼都
    沒做卻宣稱成功的批次。
    """
    st = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf",)))
    assert rs.should_skip(st, "A-1234", "AAMA", NONE_EXIST) is False


def test_partially_missing_outputs_means_rerun():
    """.dxf 還在但附帶的 .rul 不見了，這次匯出仍然不完整。"""
    st = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf", "C:\\out\\a.rul")))
    exists = lambda p: p.endswith(".dxf")
    assert rs.should_skip(st, "A-1234", "AAMA", exists) is False


def test_success_without_outputs_is_not_trusted():
    """成功卻沒有任何產出很可疑，寧可重做。"""
    st = state_with(("A-1234", "AAMA", ()))
    assert rs.should_skip(st, "A-1234", "AAMA", ALL_EXIST) is False


# ── 只有成功會被記住 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        Status.FAILED_TIMEOUT,
        Status.FAILED_MOVE,
        Status.FAILED_TARGET_EXISTS,
        Status.HALTED_UNKNOWN_DIALOG,
        Status.SKIPPED_NOT_FOUND,
    ],
)
def test_non_success_is_not_recorded(status):
    st = rs.merge(rs.EMPTY, [rec("A-1234", "AAMA", status, ("C:\\out\\a.dxf",))])
    assert rs.should_skip(st, "A-1234", "AAMA", ALL_EXIST) is False


def test_skipped_already_done_stays_recorded():
    """
    續跑時被跳過的任務，下一次還是該記得它已完成——
    否則跑第三次時它又會被重做。
    """
    previous = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf",)))
    now = rs.merge(previous, [rec("A-1234", "AAMA", Status.SKIPPED_ALREADY_DONE)])
    assert rs.should_skip(now, "A-1234", "AAMA", ALL_EXIST) is True


# ── 合併 ─────────────────────────────────────────────────────────────


def test_merge_accumulates():
    a = state_with(("M1", "ZIP", ("z",)))
    b = rs.merge(a, [rec("M1", "AAMA", outputs=("a",))])
    assert rs.should_skip(b, "M1", "ZIP", ALL_EXIST) is True
    assert rs.should_skip(b, "M1", "AAMA", ALL_EXIST) is True


def test_merge_overwrites_same_task_with_new_outputs():
    a = state_with(("M1", "ZIP", ("old",)))
    b = rs.merge(a, [rec("M1", "ZIP", outputs=("new",))])
    assert b.outputs_of("M1", "ZIP") == ("new",)


def test_merge_does_not_mutate_previous():
    a = state_with(("M1", "ZIP", ("z",)))
    rs.merge(a, [rec("M2", "ZIP", outputs=("x",))])
    assert rs.should_skip(a, "M2", "ZIP", ALL_EXIST) is False


# ── 讀寫 ─────────────────────────────────────────────────────────────


def test_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    st = state_with(("外套-左前片", "AAMA", ("C:\\out\\中文.dxf",)))
    rs.save(path, st, run_id="260830_1430")
    loaded = rs.load(path)
    assert rs.should_skip(loaded, "外套-左前片", "AAMA", ALL_EXIST) is True


def test_state_file_is_utf8(tmp_path):
    path = tmp_path / "state.json"
    rs.save(path, state_with(("外套", "ZIP", ("a",))), run_id="x")
    assert "外套".encode("utf-8") in path.read_bytes()


def test_missing_state_file_is_empty_state(tmp_path):
    assert rs.load(tmp_path / "nope.json") is rs.EMPTY


def test_corrupt_state_file_falls_back_to_empty(tmp_path):
    """
    狀態檔壞掉時重跑全部，而不是整支掛掉。
    最壞情況只是多做一次工，總比讓使用者卡在原地好。
    """
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert rs.load(path) is rs.EMPTY


@pytest.mark.parametrize(
    "body",
    [
        '{"tasks": "not a list"}',
        '{"tasks": null}',  # for item in None -> TypeError
        '{"tasks": 123}',  # for item in 123 -> TypeError
        '{"tasks": {"model": "M1"}}',
        "[1, 2, 3]",  # 頂層不是物件，data.get -> AttributeError
        '"just a string"',
        "null",
    ],
)
def test_state_file_with_wrong_shape_falls_back_to_empty(tmp_path, body):
    """
    使用者可能手動編輯過 state.json。任何形狀壞掉的內容都該退回空狀態，
    而不是讓整支腳本掛在讀檔階段。

    這裡刻意涵蓋 null 與數字：字串或 dict 迭代後會自然變空，看起來像是
    有防護，實際上少了型別檢查時 `for item in None` 會直接拋 TypeError。
    """
    path = tmp_path / "state.json"
    path.write_text(body, encoding="utf-8")
    assert rs.load(path) is rs.EMPTY


def test_save_creates_parent_dir(tmp_path):
    path = tmp_path / "_log" / "state.json"
    rs.save(path, rs.EMPTY, run_id="x")
    assert path.is_file()


def test_saved_json_is_human_readable(tmp_path):
    """使用者可能想自己打開看、或手動刪掉某一筆重做。"""
    path = tmp_path / "state.json"
    rs.save(path, state_with(("M1", "ZIP", ("a",))), run_id="260830_1430")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "260830_1430"
    assert isinstance(data["tasks"], list)
    assert data["tasks"][0]["model"] == "M1"


# ── --force ──────────────────────────────────────────────────────────


def test_force_ignores_everything(tmp_path):
    path = tmp_path / "state.json"
    rs.save(path, state_with(("M1", "ZIP", ("a",))), run_id="x")
    assert rs.load(path, force=True) is rs.EMPTY


def test_force_does_not_delete_the_file_before_the_run(tmp_path):
    """
    重跑期間如果又中斷，舊狀態還在總比什麼都沒有好。
    真正的覆寫發生在本次跑完存檔的時候。
    """
    path = tmp_path / "state.json"
    rs.save(path, state_with(("M1", "ZIP", ("a",))), run_id="x")
    rs.load(path, force=True)
    assert path.is_file()
