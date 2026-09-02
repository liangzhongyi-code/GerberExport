"""
C3 續跑判定測試（對應 spec: operability「中斷後續跑」、design.md §4.2）。

狀態檔的形狀依 §4.2：頂層 runId／outputDir／models／tasks，每筆 task 有
format／model／status／startedAt／finishedAt／outputs。

should_skip 的存在性檢查以 exists_fn 注入，因此「狀態檔說成功但檔案被
刪掉了」這種情境不用真的去刪檔案就能測。

核心規則：**狀態檔說成功還不夠，產出檔案要真的還在。** 只信狀態檔的話，
使用者手動清掉輸出資料夾之後重跑，會拿到一個什麼都沒做卻宣稱成功的批次。

`--force` 的方向以 spec Scenario「強制全部重跑」為準：所有任務皆執行、
狀態檔被重置。重置的做法依 §4.2：既有 state.json 改名為
state_<舊 runId>.json 保留，不刪。
"""

import json
from pathlib import Path

import pytest

from lib import runstate as rs
from lib.reporting import Status, TaskRecord

T0, T1 = "2026-09-02T14:30:12", "2026-09-02T14:30:31"
RUN_ID = "260902_1430"
OUT = "C:\\Users\\me\\Desktop\\AccuMark匯出_260902_1430"
MODELS = ("A-1234", "A-9999")


def rec(model, fmt, status=Status.SUCCESS, outputs=()):
    return TaskRecord(
        model=model,
        fmt=fmt,
        status=status,
        started_at=T0,
        finished_at=T1,
        outputs=tuple(outputs),
    )


def fresh():
    """一個還沒做任何任務的新批次。"""
    return rs.new_state(RUN_ID, OUT, MODELS)


def state_with(*pairs):
    """pairs: (model, fmt, outputs)，全部記成 SUCCESS。"""
    st = fresh()
    for m, f, o in pairs:
        st = rs.mark(st, rec(m, f, outputs=o))
    return st


def entries_for(state, model, fmt):
    return [t for t in state.tasks if (t.model, t.fmt) == (model, fmt)]


ALL_EXIST = lambda p: True
NONE_EXIST = lambda p: False


# ── 基本判定 ─────────────────────────────────────────────────────────


def test_unknown_task_is_not_skipped():
    assert rs.should_skip(fresh(), "A-1234", "AAMA", ALL_EXIST) is False


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
    Scenario「狀態檔記載成功但檔案已被刪除」：狀態檔說成功但檔案被刪掉了，
    就得重做。只信狀態檔的話，使用者清掉輸出資料夾後重跑會拿到一個什麼都
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


def test_exists_fn_receives_every_output_path():
    """存在性檢查得逐一問過每個產出，少問一個就是漏檢。"""
    seen = []
    st = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf", "C:\\out\\a.rul")))
    rs.should_skip(st, "A-1234", "AAMA", lambda p: seen.append(p) or True)
    assert seen == ["C:\\out\\a.dxf", "C:\\out\\a.rul"]


# ── 只有 SUCCESS 會被跳過；其他狀態照樣記錄但一律重跑 ────────────────


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
def test_non_success_is_recorded_but_rerun(status):
    """
    §4.2 的狀態檔記每一筆任務的結果（使用者打開能看到上次哪裡失敗），
    但跳過的條件只有 SUCCESS——失敗的任務下次一定重做。
    """
    st = rs.mark(fresh(), rec("A-1234", "AAMA", status, ("C:\\out\\a.dxf",)))
    assert st.latest("A-1234", "AAMA").status == status.value
    assert rs.should_skip(st, "A-1234", "AAMA", ALL_EXIST) is False


def test_skipped_already_done_keeps_the_success_record():
    """
    續跑時被跳過的任務，下一次還是該記得它已完成——
    若把 SUCCESS 換成 SKIPPED_ALREADY_DONE，跑第三次時它又會被重做。
    """
    previous = state_with(("A-1234", "AAMA", ("C:\\out\\a.dxf",)))
    now = rs.mark(previous, rec("A-1234", "AAMA", Status.SKIPPED_ALREADY_DONE))
    assert now.latest("A-1234", "AAMA").status == "SUCCESS"
    assert rs.should_skip(now, "A-1234", "AAMA", ALL_EXIST) is True


# ── mark：同一個 (model, format) 取代舊紀錄，不追加 ──────────────────


def test_mark_replaces_the_previous_record_for_the_same_task():
    """
    重做一次失敗的任務之後，狀態檔裡該只剩最新那一筆。追加的話，
    「最新紀錄」要靠順序猜，而且使用者打開檔案會看到同一個任務兩種結論。
    """
    a = state_with(("M1", "ZIP", ("old",)))
    b = rs.mark(a, rec("M1", "ZIP", Status.FAILED_TIMEOUT))
    assert len(entries_for(b, "M1", "ZIP")) == 1
    assert b.latest("M1", "ZIP").status == "FAILED_TIMEOUT"
    assert rs.should_skip(b, "M1", "ZIP", ALL_EXIST) is False


def test_mark_replaces_outputs_with_the_new_ones():
    a = state_with(("M1", "ZIP", ("old",)))
    b = rs.mark(a, rec("M1", "ZIP", outputs=("new",)))
    assert b.latest("M1", "ZIP").outputs == ("new",)


def test_mark_replacement_keeps_the_original_position():
    """使用者看檔案時任務順序不該因為重做而跳來跳去。"""
    st = state_with(("M1", "ZIP", ("z",)), ("M1", "AAMA", ("a",)))
    st = rs.mark(st, rec("M1", "ZIP", outputs=("z2",)))
    assert [(t.model, t.fmt) for t in st.tasks] == [("M1", "ZIP"), ("M1", "AAMA")]


def test_mark_accumulates_different_tasks():
    a = state_with(("M1", "ZIP", ("z",)))
    b = rs.mark(a, rec("M1", "AAMA", outputs=("a",)))
    assert rs.should_skip(b, "M1", "ZIP", ALL_EXIST) is True
    assert rs.should_skip(b, "M1", "AAMA", ALL_EXIST) is True


def test_mark_records_timestamps_and_status_string():
    st = rs.mark(fresh(), rec("M1", "ZIP", outputs=("z",)))
    t = st.latest("M1", "ZIP")
    assert (t.started_at, t.finished_at) == (T0, T1)
    assert t.status == "SUCCESS"


def test_mark_does_not_mutate_previous():
    a = state_with(("M1", "ZIP", ("z",)))
    rs.mark(a, rec("M2", "ZIP", outputs=("x",)))
    assert rs.should_skip(a, "M2", "ZIP", ALL_EXIST) is False
    assert len(a.tasks) == 1


def test_mark_keeps_run_id_output_dir_and_models():
    st = rs.mark(fresh(), rec("M1", "ZIP", outputs=("z",)))
    assert (st.run_id, st.output_dir, st.models) == (RUN_ID, OUT, MODELS)


# ── 純函式：落點路徑（§4.2）─────────────────────────────────────────


def test_runs_dir_is_under_scripts(tmp_path):
    assert rs.runs_dir(tmp_path) == tmp_path / "runs"


def test_runs_dir_accepts_str(tmp_path):
    assert rs.runs_dir(str(tmp_path)) == tmp_path / "runs"


def test_state_path_is_runs_state_json(tmp_path):
    assert rs.state_path(tmp_path) == tmp_path / "runs" / "state.json"


def test_log_path_carries_run_id(tmp_path):
    assert rs.log_path(tmp_path, RUN_ID) == tmp_path / "runs" / f"日誌_{RUN_ID}.txt"


def test_force_archive_path_carries_old_run_id(tmp_path):
    assert (
        rs.force_archive_path(tmp_path, "260901_0900")
        == tmp_path / "runs" / "state_260901_0900.json"
    )


# ── 純函式：新批次與續跑資料夾 ───────────────────────────────────────


def test_new_state_has_no_tasks():
    st = fresh()
    assert st.tasks == ()
    assert (st.run_id, st.output_dir, st.models) == (RUN_ID, OUT, MODELS)


def test_new_state_accepts_path_and_list():
    """呼叫端手上多半是 Path 與 list，存進去要變成能寫進 JSON 的形狀。"""
    st = rs.new_state(RUN_ID, Path(OUT), ["A", "B"])
    assert st.output_dir == OUT
    assert st.models == ("A", "B")


def test_resume_output_dir_returns_the_recorded_dir():
    """續跑沿用 outputDir，補跑的產出要落在同一批資料夾。"""
    assert rs.resume_output_dir(fresh()) == Path(OUT)


def test_resume_output_dir_is_none_without_state():
    assert rs.resume_output_dir(None) is None


# ── 讀寫（形狀依 §4.2）───────────────────────────────────────────────


def test_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    st = state_with(("外套-左前片", "AAMA", ("C:\\out\\中文.dxf",)))
    rs.save(path, st)
    loaded = rs.load(path)
    assert rs.should_skip(loaded, "外套-左前片", "AAMA", ALL_EXIST) is True
    assert loaded == st


def test_state_file_is_utf8(tmp_path):
    path = tmp_path / "state.json"
    rs.save(path, state_with(("外套", "ZIP", ("a",))))
    assert "外套".encode("utf-8") in path.read_bytes()


def test_saved_json_has_the_4_2_shape(tmp_path):
    """使用者可能想自己打開看、或手動刪掉某一筆重做，欄位名照 §4.2。"""
    path = tmp_path / "state.json"
    rs.save(path, state_with(("M1", "AAMA", ("C:\\out\\m1.dxf", "C:\\out\\m1.rul"))))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["runId"] == RUN_ID
    assert data["outputDir"] == OUT
    assert data["models"] == list(MODELS)
    task = data["tasks"][0]
    assert task["format"] == "AAMA"
    assert task["model"] == "M1"
    assert task["status"] == "SUCCESS"
    assert task["startedAt"] == T0
    assert task["finishedAt"] == T1
    assert task["outputs"] == ["C:\\out\\m1.dxf", "C:\\out\\m1.rul"]


def test_loads_the_design_4_2_example_verbatim(tmp_path):
    """design.md §4.2 的範例逐字讀進來要能認出兩筆任務（ZIP 那筆只有 kind）。"""
    body = {
        "runId": "260902_1430",
        "outputDir": "C:\\Users\\x\\Desktop\\AccuMark匯出_260902_1430",
        "models": ["m1", "m2"],
        "tasks": [
            {
                "kind": "ZIP",
                "model": "m1",
                "status": "SUCCESS",
                "startedAt": "2026-09-02T14:30:12",
                "finishedAt": "2026-09-02T14:30:31",
                "outputs": ["C:\\out\\m1\\m1.zip"],
            },
            {
                "kind": "DXF",
                "format": "AAMA",
                "model": "m1",
                "status": "SUCCESS",
                "startedAt": "2026-09-02T14:32:00",
                "finishedAt": "2026-09-02T14:32:09",
                "outputs": ["C:\\out\\m1\\m1.dxf", "C:\\out\\m1\\m1.rul"],
            },
        ],
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    st = rs.load(path)
    assert rs.should_skip(st, "m1", "ZIP", ALL_EXIST) is True
    assert rs.should_skip(st, "m1", "AAMA", ALL_EXIST) is True
    assert st.models == ("m1", "m2")


def test_missing_state_file_means_no_state(tmp_path):
    assert rs.load(tmp_path / "nope.json") is None


def test_corrupt_state_file_means_no_state(tmp_path):
    """
    狀態檔壞掉時重跑全部，而不是整支掛掉。
    最壞情況只是多做一次工，總比讓使用者卡在原地好。
    """
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert rs.load(path) is None


@pytest.mark.parametrize(
    "body",
    [
        '{"runId": "x", "outputDir": "C:\\\\o", "models": [], "tasks": "not a list"}',
        '{"runId": "x", "outputDir": "C:\\\\o", "models": [], "tasks": null}',
        '{"runId": "x", "outputDir": "C:\\\\o", "models": [], "tasks": 123}',
        '{"runId": "x", "outputDir": "C:\\\\o", "models": [], "tasks": {"model": "M1"}}',
        '{"outputDir": "C:\\\\o", "models": [], "tasks": []}',  # 沒有 runId
        '{"runId": "x", "models": [], "tasks": []}',  # 沒有 outputDir，續跑不知道要落哪
        '{"runId": 123, "outputDir": "C:\\\\o", "models": [], "tasks": []}',
        '{"run_id": "x", "tasks": []}',  # §4.2 之前的舊形狀
        "[1, 2, 3]",  # 頂層不是物件，data.get -> AttributeError
        '"just a string"',
        "null",
    ],
)
def test_state_file_with_wrong_shape_means_no_state(tmp_path, body):
    """
    使用者可能手動編輯過 state.json。任何形狀壞掉的內容都該當作沒有狀態，
    而不是讓整支腳本掛在讀檔階段。

    這裡刻意涵蓋 null 與數字：字串或 dict 迭代後會自然變空，看起來像是
    有防護，實際上少了型別檢查時 `for item in None` 會直接拋 TypeError。
    """
    path = tmp_path / "state.json"
    path.write_text(body, encoding="utf-8")
    assert rs.load(path) is None


def test_malformed_task_entries_are_skipped_but_the_rest_survive():
    data = rs.to_json(state_with(("M1", "ZIP", ("z",))))
    data["tasks"].insert(0, {"model": "M2"})  # 沒有 format
    data["tasks"].append("garbage")
    data["tasks"].append({"model": "M3", "format": "ZIP", "status": "SUCCESS", "outputs": "x"})
    st = rs.from_json(data)
    assert st is not None
    assert [(t.model, t.fmt) for t in st.tasks] == [("M1", "ZIP")]


def test_duplicate_entries_in_a_hand_edited_file_use_the_last_one():
    """手動編輯過的檔案可能同一任務出現兩次，「最新紀錄」以最後一筆為準。"""
    data = rs.to_json(state_with(("M1", "ZIP", ("z",))))
    data["tasks"].append(dict(data["tasks"][0], status="FAILED_TIMEOUT"))
    st = rs.from_json(data)
    assert rs.should_skip(st, "M1", "ZIP", ALL_EXIST) is False


def test_unknown_status_string_loads_and_is_rerun():
    """
    狀態檔的 status 是字串，不是 reporting.Status 列舉——日後 §4.3 再新增
    狀態（像 FAILED_SELECTION 那樣）時，舊版程式讀到新狀態不能炸，
    而且一律重跑。
    """
    data = rs.to_json(state_with(("M1", "AAMA", ("a",))))
    data["tasks"][0]["status"] = "FAILED_SOMETHING_NEW"
    st = rs.from_json(data)
    assert st.latest("M1", "AAMA").status == "FAILED_SOMETHING_NEW"
    assert rs.should_skip(st, "M1", "AAMA", ALL_EXIST) is False


def test_save_creates_parent_dir(tmp_path):
    path = tmp_path / "runs" / "state.json"
    rs.save(path, fresh())
    assert path.is_file()


def test_to_json_from_json_roundtrip_is_pure():
    st = state_with(("M1", "ZIP", ("z",)), ("M1", "AAMA", ("a", "r")))
    assert rs.from_json(rs.to_json(st)) == st


# ── --force：Scenario「強制全部重跑」──────────────────────────────────
#
# spec：GIVEN 使用者雙擊 4_強制全部重跑.bat（--force）
#       WHEN  腳本進行續跑判定
#       THEN  所有任務皆執行，狀態檔被重置
# §4.2：--force 把現有 state.json 改名為 state_<runId>.json 保留，開新批次、
#       新資料夾；一般執行才續跑（沿用 outputDir）。
#
# 舊測試斷言「--force 之後 state.json 原封不動、跑完才覆寫」，方向與 spec
# 相反：forced 批次若中途中斷，下一次一般執行會接回**舊**批次的狀態與資料夾，
# 使用者要的「全部重來」就悄悄失效了。


def test_plan_start_without_previous_state_starts_fresh():
    plan = rs.plan_start(None, False, RUN_ID, OUT, MODELS)
    assert plan.state == fresh()
    assert plan.resumed is False
    assert plan.archive_run_id is None


def test_plan_start_resumes_by_default():
    """一般執行才續跑：沿用舊批次的 runId、outputDir 與已完成清單。"""
    previous = rs.mark(
        rs.new_state("260901_0900", "C:\\old", ("M1",)), rec("M1", "ZIP", outputs=("z",))
    )
    plan = rs.plan_start(previous, False, RUN_ID, OUT, MODELS)
    assert plan.resumed is True
    assert plan.state is previous
    assert plan.archive_run_id is None
    assert rs.should_skip(plan.state, "M1", "ZIP", ALL_EXIST) is True


def test_plan_start_with_force_ignores_previous_state():
    """--force：所有任務皆執行。"""
    previous = rs.mark(
        rs.new_state("260901_0900", "C:\\old", ("M1",)), rec("M1", "ZIP", outputs=("z",))
    )
    plan = rs.plan_start(previous, True, RUN_ID, OUT, MODELS)
    assert plan.resumed is False
    assert plan.state == fresh()
    assert rs.should_skip(plan.state, "M1", "ZIP", ALL_EXIST) is False


def test_plan_start_with_force_archives_the_old_run():
    """--force：狀態檔被重置——改名保留，不刪，所以要知道舊 runId。"""
    previous = rs.new_state("260901_0900", "C:\\old", ("M1",))
    plan = rs.plan_start(previous, True, RUN_ID, OUT, MODELS)
    assert plan.archive_run_id == "260901_0900"


def test_plan_start_with_force_and_nothing_to_archive():
    plan = rs.plan_start(None, True, RUN_ID, OUT, MODELS)
    assert plan.archive_run_id is None
    assert plan.state == fresh()


def _write_previous(scripts_dir):
    previous = rs.mark(
        rs.new_state("260901_0900", "C:\\old", ("M1",)), rec("M1", "ZIP", outputs=("z",))
    )
    rs.save(rs.state_path(scripts_dir), previous)
    return previous


def test_start_with_force_reruns_everything(tmp_path):
    _write_previous(tmp_path)
    started = rs.start(tmp_path, RUN_ID, OUT, MODELS, force=True)
    assert started.resumed is False
    assert started.state.run_id == RUN_ID
    assert rs.should_skip(started.state, "M1", "ZIP", ALL_EXIST) is False


def test_start_with_force_renames_the_old_state_file(tmp_path):
    """
    重置的做法是改名保留，不是刪除：使用者要是按錯了 4_，舊批次的紀錄
    還找得回來。
    """
    previous = _write_previous(tmp_path)
    started = rs.start(tmp_path, RUN_ID, OUT, MODELS, force=True)
    archive = rs.force_archive_path(tmp_path, "260901_0900")
    assert started.archived_to == archive
    assert rs.load(archive) == previous


def test_start_with_force_resets_state_json_to_the_new_run(tmp_path):
    """
    重置之後 state.json 記的是新批次：forced 批次若中途中斷，下一次一般
    執行要接回**新**批次，而不是舊的。
    """
    _write_previous(tmp_path)
    rs.start(tmp_path, RUN_ID, OUT, MODELS, force=True)
    on_disk = rs.load(rs.state_path(tmp_path))
    assert on_disk is not None
    assert on_disk.run_id == RUN_ID
    assert on_disk.tasks == ()


def test_start_with_force_does_not_overwrite_an_existing_archive(tmp_path):
    """同一分鐘內按兩次 4_ 會撞到同名的封存檔，寧可多一個檔也不能蓋掉。"""
    archive = rs.force_archive_path(tmp_path, "260901_0900")
    archive.parent.mkdir(parents=True)
    archive.write_text("older archive", encoding="utf-8")
    _write_previous(tmp_path)
    started = rs.start(tmp_path, RUN_ID, OUT, MODELS, force=True)
    assert archive.read_text(encoding="utf-8") == "older archive"
    assert started.archived_to != archive
    assert started.archived_to.is_file()


def test_start_without_force_resumes(tmp_path):
    """一般執行才續跑：沿用舊 runId 與 outputDir，已完成的任務會被跳過。"""
    previous = _write_previous(tmp_path)
    started = rs.start(tmp_path, RUN_ID, OUT, MODELS, force=False)
    assert started.resumed is True
    assert started.state == previous
    assert started.archived_to is None
    assert rs.resume_output_dir(started.state) == Path("C:\\old")
    assert rs.should_skip(started.state, "M1", "ZIP", ALL_EXIST) is True


def test_start_without_force_leaves_the_state_file_alone(tmp_path):
    _write_previous(tmp_path)
    before = rs.state_path(tmp_path).read_bytes()
    rs.start(tmp_path, RUN_ID, OUT, MODELS, force=False)
    assert rs.state_path(tmp_path).read_bytes() == before
    assert not rs.force_archive_path(tmp_path, "260901_0900").exists()


def test_start_without_previous_state_creates_a_fresh_one(tmp_path):
    started = rs.start(tmp_path, RUN_ID, OUT, MODELS)
    assert started.resumed is False
    assert started.archived_to is None
    assert rs.load(rs.state_path(tmp_path)) == fresh()


def test_start_with_force_and_no_previous_state_is_fine(tmp_path):
    started = rs.start(tmp_path, RUN_ID, OUT, MODELS, force=True)
    assert started.state == fresh()
    assert started.archived_to is None


def test_start_treats_a_corrupt_state_file_as_absent(tmp_path):
    path = rs.state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ broken", encoding="utf-8")
    started = rs.start(tmp_path, RUN_ID, OUT, MODELS)
    assert started.resumed is False
    assert rs.load(path) == fresh()
