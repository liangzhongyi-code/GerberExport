"""
期二編排層（D4）：任務怎麼排、一個任務怎麼從觸發走到歸檔。

這一層是整個專案唯一「把所有模組串起來」的地方，而它在開發機上沒有
AccuMark 可以跑。作法是把不純的東西全部注入：UI 操作交給替身、時鐘與
睡眠是假的，**但暫存夾與輸出資料夾是真的**——歸檔那一段要是也用替身，
就等於沒測到「檔案真的被搬到正確的資料夾」，而那正是使用者要的東西。

替身記錄每一次呼叫，所以「選取讀回不符時沒有按下執行鈕」這種
「不該做什麼」的斷言才驗得出來。TD-9 的整個保證就靠這個。
"""

from datetime import datetime
from pathlib import Path

import pytest

from lib import archival, orchestrator as orch
from lib.dialog_guard import DialogInfo
from lib.reporting import Status

WHEN = datetime(2026, 9, 2, 14, 30)


# ── 替身 ─────────────────────────────────────────────────────────────


class FakeOps:
    """
    假的 UI 操作層。

    produces 決定「觸發之後暫存夾會出現什麼」：{(model, fmt): [檔名, ...]}，
    預設依 model 命名。檔案是**真的**寫進暫存夾的，所以完成偵測與歸檔走的
    是真實路徑。
    """

    def __init__(
        self,
        temp_dir: Path,
        *,
        explorer_models=("M1", "M2"),
        dcu_models=None,
        produces=None,
        selection_override=None,
        completion_dialog="Process Complete",
        fail_on=None,
    ):
        self.temp_dir = Path(temp_dir)
        self.explorer_models = tuple(explorer_models)
        self.dcu_models = tuple(dcu_models if dcu_models is not None else explorer_models)
        self.produces = dict(produces or {})
        # selection_override：模擬「選了卻讀回別的東西」（上次選取殘留）
        self.selection_override = dict(selection_override or {})
        self.completion_dialog = completion_dialog
        self.fail_on = dict(fail_on or {})

        self.calls = []
        self.dialog = None
        self._selected = ()
        self._pending = None

    # -- 記錄與失敗注入 --
    def _call(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail_on:
            raise self.fail_on[name]

    def names(self):
        return [c[0] for c in self.calls]

    def _emit(self, model, fmt):
        """把這次任務「該產出」的檔案真的寫進暫存夾。"""
        for fname in self.produces.get((model, fmt), (f"{model}.dxf",)):
            (self.temp_dir / fname).write_text("x" * 10, encoding="utf-8")

    # -- 查詢 --
    def available_models(self, fmt):
        self._call("available_models", fmt)
        return self.explorer_models if fmt == "ZIP" else self.dcu_models

    def selected_models(self):
        self._call("selected_models")
        return self.explorer_models

    # -- ZIP --
    def explorer_select(self, model):
        self._call("explorer_select", model)
        self._selected = self.selection_override.get(model, (model,))

    def explorer_selection(self):
        self._call("explorer_selection")
        return self._selected

    def export_zip(self, model, dest):
        self._call("export_zip", model, str(dest))
        self._emit(model, "ZIP")
        self.dialog = DialogInfo(title=self.completion_dialog)

    # -- DXF --
    def dcu_set_format(self, fmt):
        self._call("dcu_set_format", fmt)

    def dcu_select(self, model):
        self._call("dcu_select", model)
        self._selected = self.selection_override.get(model, (model,))

    def dcu_selection(self):
        self._call("dcu_selection")
        return self._selected

    def dcu_set_destination(self, dest):
        self._call("dcu_set_destination", str(dest))

    def dcu_run(self, model, fmt):
        self._call("dcu_run", model, fmt)
        self._emit(model, fmt)

    # -- 守衛 --
    def foreground_dialog(self):
        return self.dialog

    def dismiss_completion(self):
        self._call("dismiss_completion")
        self.dialog = None


class Clock:
    """假時鐘：sleep 就是把時間往前撥。"""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


def make_ctx(tmp_path, ops, **kw):
    temp_dir = Path(tmp_path) / "temp"
    temp_dir.mkdir(exist_ok=True)
    out_dir = Path(tmp_path) / "out"
    clock = Clock()
    params = dict(
        temp_dir=temp_dir,
        output_dir=out_dir,
        ops=ops,
        expected_outputs={"ZIP": (".zip",), "AAMA": (".dxf", ".rul"), "ASTM": (".dxf",)},
        completion_title_like="*Process Complete*",
        dialog_rules=(),
        poll_interval_ms=100,
        stable_samples=2,
        quiet_period_sec=0.0,
        timeout_sec=60,
        add_format_suffix=False,
        clock_fn=clock.now,
        sleep_fn=clock.sleep,
        now_fn=lambda: WHEN,
    )
    params.update(kw)
    return orch.RunContext(**params), clock


def ops_for(tmp_path, **kw):
    temp_dir = Path(tmp_path) / "temp"
    temp_dir.mkdir(exist_ok=True)
    return FakeOps(temp_dir, **kw)


def run_one(tmp_path, task, *, ops=None, ctx_kw=None, ops_kw=None):
    ops = ops or ops_for(tmp_path, **(ops_kw or {}))
    ctx, _ = make_ctx(tmp_path, ops, **(ctx_kw or {}))
    return orch.run_task(task, ctx), ops, ctx


ZIP_TASK = orch.Task("M1", "ZIP") if hasattr(orch, "Task") else None


# ── 任務規劃 ─────────────────────────────────────────────────────────


def test_every_model_gets_every_format():
    """N model × 3 格式。TD-9：DXF 也是逐 model，不是一次全選。"""
    tasks = orch.plan_tasks(("M1", "M2"), ("ZIP", "AAMA", "ASTM"))
    assert len(tasks) == 6
    assert {(t.model, t.fmt) for t in tasks} == {
        (m, f) for m in ("M1", "M2") for f in ("ZIP", "AAMA", "ASTM")
    }


def test_tasks_are_grouped_by_format():
    """
    同格式的任務排在一起，DCU 的 File Type 才只切一次。

    TD-9 說 UI 操作次數是主要風險來源——每多切一次下拉就多一次讀回驗證
    可能失敗的機會。四個 model 逐一切換要切 8 次，照格式分組只要 2 次。
    """
    tasks = orch.plan_tasks(("M1", "M2", "M3"), ("ZIP", "AAMA", "ASTM"))
    assert [t.fmt for t in tasks] == ["ZIP"] * 3 + ["AAMA"] * 3 + ["ASTM"] * 3


def test_model_order_is_preserved_within_a_format():
    tasks = orch.plan_tasks(("B", "A"), ("AAMA",))
    assert [t.model for t in tasks] == ["B", "A"]


def test_only_filter_keeps_one_model():
    tasks = orch.plan_tasks(("M1", "M2"), ("ZIP", "AAMA"), only="M2")
    assert {t.model for t in tasks} == {"M2"}


def test_format_filter_keeps_one_format():
    tasks = orch.plan_tasks(("M1", "M2"), ("ZIP", "AAMA"), only_format="AAMA")
    assert {t.fmt for t in tasks} == {"AAMA"}


def test_only_filter_is_case_insensitive():
    """使用者從 Explorer 抄名字，大小寫未必一致；Windows 本來就不分。"""
    tasks = orch.plan_tasks(("Model-A",), ("ZIP",), only="model-a")
    assert len(tasks) == 1


def test_unknown_only_yields_no_tasks():
    """指名的 model 不在清單裡 → 空清單；由主流程回報，不是靜默跑全部。"""
    assert orch.plan_tasks(("M1",), ("ZIP",), only="不存在") == ()


def test_task_label_is_used_for_residue_folders():
    task = orch.Task("M1", "AAMA")
    assert task.label == archival.task_label("AAMA", "M1")


# ── ZIP：正常路徑 ────────────────────────────────────────────────────


def test_zip_success_moves_files_into_model_folder(tmp_path):
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "ZIP"),
        ops_kw={"produces": {("M1", "ZIP"): ("M1.zip",)}},
    )
    assert outcome.status is Status.SUCCESS
    assert (ctx.output_dir / "M1" / "M1.zip").is_file()
    assert outcome.outputs == (str(ctx.output_dir / "M1" / "M1.zip"),)


def test_zip_leaves_the_temp_dir_empty(tmp_path):
    """核心不變式：下一個任務開始前暫存夾必須是空的。"""
    outcome, ops, ctx = run_one(
        tmp_path, orch.Task("M1", "ZIP"), ops_kw={"produces": {("M1", "ZIP"): ("M1.zip",)}}
    )
    assert list(ctx.temp_dir.iterdir()) == []


def test_zip_presses_ok_only_after_the_files_are_stable(tmp_path):
    """
    完成對話框出現不代表檔案寫完（TD-4）。按 OK 之前必須先確認穩定——
    順序反過來的話，AccuMark 可能在對話框關掉時才 flush。
    """
    outcome, ops, ctx = run_one(
        tmp_path, orch.Task("M1", "ZIP"), ops_kw={"produces": {("M1", "ZIP"): ("M1.zip",)}}
    )
    names = ops.names()
    assert "dismiss_completion" in names
    assert names.index("export_zip") < names.index("dismiss_completion")


def test_zip_verifies_the_selection_before_exporting(tmp_path):
    """
    ZIP 多選會把四個 model 混成一包（使用者實務確認）。規格只對 DCU 明訂
    讀回驗證，但同一個失敗模式在 Explorer 一樣存在，而後果一樣是「檔名
    正確、內容錯誤」——看不出來的那一種。
    """
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "ZIP"),
        ops_kw={"selection_override": {"M1": ("M1", "M2")}},
    )
    assert outcome.status is Status.FAILED_SELECTION
    assert "export_zip" not in ops.names(), "選取不符卻還是匯出了"


# ── DXF：TD-9 的讀回驗證 ─────────────────────────────────────────────


def test_dxf_success_moves_all_outputs(tmp_path):
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "AAMA"),
        ops_kw={"produces": {("M1", "AAMA"): ("M1.dxf", "M1.rul")}},
    )
    assert outcome.status is Status.SUCCESS
    assert (ctx.output_dir / "M1" / "M1.dxf").is_file()
    assert (ctx.output_dir / "M1" / "M1.rul").is_file()


def test_dxf_sets_format_before_selecting(tmp_path):
    """先切 File Type 再選 model：順序反了，選取可能被切換動作清掉。"""
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "AAMA"),
        ops_kw={"produces": {("M1", "AAMA"): ("M1.dxf", "M1.rul")}},
    )
    names = ops.names()
    assert names.index("dcu_set_format") < names.index("dcu_select")


def test_dxf_refuses_to_run_when_two_items_are_selected(tmp_path):
    """
    這是 TD-9 的核心保證。DCU 記住上次的選取、Select 沒清乾淨，兩個 model
    都亮著——執行下去會產出一個把裁片併在一起的 DXF，而檔名還是對的。
    """
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "AAMA"),
        ops_kw={"selection_override": {"M1": ("M1", "M2")}},
    )
    assert outcome.status is Status.FAILED_SELECTION
    assert "dcu_run" not in ops.names(), "讀回不符卻還是按了執行鈕"


def test_dxf_refuses_when_the_selected_one_is_a_different_model(tmp_path):
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "AAMA"),
        ops_kw={"selection_override": {"M1": ("M2",)}},
    )
    assert outcome.status is Status.FAILED_SELECTION
    assert "dcu_run" not in ops.names()


def test_dxf_refuses_when_nothing_is_selected(tmp_path):
    outcome, ops, ctx = run_one(
        tmp_path, orch.Task("M1", "AAMA"), ops_kw={"selection_override": {"M1": ()}}
    )
    assert outcome.status is Status.FAILED_SELECTION
    assert "dcu_run" not in ops.names()


def test_selection_mismatch_lists_what_was_actually_selected(tmp_path):
    """使用者要能從日誌看出「它選到了什麼」，否則只能猜。"""
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "AAMA"),
        ops_kw={"selection_override": {"M1": ("M1", "M2")}},
    )
    assert "M2" in outcome.detail


def test_selection_mismatch_does_not_abort_the_batch(tmp_path):
    """
    §4.3：FAILED_SELECTION 發生在觸發之前，什麼都沒動。下一個 model
    重選一次多半就正常了，為一次殘留停掉整批太浪費。
    """
    outcome, _, _ = run_one(
        tmp_path, orch.Task("M1", "AAMA"), ops_kw={"selection_override": {"M1": ()}}
    )
    assert not outcome.status.aborts_batch


# ── 找不到 model ─────────────────────────────────────────────────────


def test_model_missing_from_the_list_is_skipped(tmp_path):
    outcome, ops, ctx = run_one(
        tmp_path, orch.Task("M9", "ZIP"), ops_kw={"explorer_models": ("M1", "M2")}
    )
    assert outcome.status is Status.SKIPPED_NOT_FOUND
    assert "explorer_select" not in ops.names()


def test_model_missing_only_from_dcu_is_skipped(tmp_path):
    """Explorer 有、DCU 沒有：ZIP 做得成，DXF 做不成。分別判斷。"""
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M2", "AAMA"),
        ops_kw={"explorer_models": ("M1", "M2"), "dcu_models": ("M1",)},
    )
    assert outcome.status is Status.SKIPPED_NOT_FOUND


# ── 完成偵測 ─────────────────────────────────────────────────────────


def test_timeout_moves_residue_instead_of_deleting_it(tmp_path):
    """
    逾時的殘留可能是寫到一半的檔，也可能是完整但訊號沒來的檔——分不出來，
    所以一律保留給人看。刪掉的話使用者連「它到底有沒有寫出東西」都不知道。
    """
    ops = ops_for(tmp_path, produces={("M1", "AAMA"): ("M1.dxf",)})
    # 預期兩個檔（.dxf + .rul）但只產出一個 → 訊號永遠不會齊
    outcome, _, ctx = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert outcome.status is Status.FAILED_TIMEOUT
    residue = archival.residue_dir(
        ctx.output_dir, archival.TIMEOUT_RESIDUE_DIRNAME, orch.Task("M1", "AAMA").label
    )
    assert (residue / "M1.dxf").is_file()
    assert list(ctx.temp_dir.iterdir()) == [], "殘留沒有搬走，下一個任務的不變式就破了"


def test_timeout_does_not_abort_the_batch(tmp_path):
    ops = ops_for(tmp_path, produces={("M1", "AAMA"): ("M1.dxf",)})
    outcome, _, _ = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert not outcome.status.aborts_batch


def test_extra_file_goes_to_the_unclassified_folder(tmp_path):
    """
    TD-9 的防線：任務逐 model，暫存夾裡的東西理應全屬它。對不上的要被
    看見——靜默歸到某個 model 底下，使用者會拿到一個不該在那裡的檔案。
    """
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "AAMA"),
        ops_kw={"produces": {("M1", "AAMA"): ("M1.dxf", "M1.rul", "別人的.dxf")}},
    )
    assert outcome.status is Status.SUCCESS
    assert (ctx.output_dir / "M1" / "M1.dxf").is_file()
    unclassified = archival.residue_dir(
        ctx.output_dir, archival.UNCLASSIFIED_DIRNAME, orch.Task("M1", "AAMA").label
    )
    assert (unclassified / "別人的.dxf").is_file()
    assert "未歸類" in outcome.detail


def test_ownership_check_is_case_insensitive(tmp_path):
    """Windows 檔案系統不分大小寫，歸屬判斷也不該分。"""
    outcome, ops, ctx = run_one(
        tmp_path,
        orch.Task("M1", "ASTM"),
        ops_kw={"produces": {("M1", "ASTM"): ("m1.DXF",)}},
    )
    assert outcome.status is Status.SUCCESS
    assert (ctx.output_dir / "M1" / "m1.DXF").is_file()


# ── 守衛 ─────────────────────────────────────────────────────────────


def test_unknown_dialog_halts_the_batch(tmp_path):
    ops = ops_for(tmp_path)
    ops.dialog = DialogInfo(title="磁碟區沒有回應")
    outcome, _, ctx = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert outcome.status is Status.HALTED_UNKNOWN_DIALOG
    assert outcome.status.aborts_batch


def test_unknown_dialog_is_described_for_the_whitelist(tmp_path):
    """停機時要留下使用者擴充白名單所需的全部資訊（TD-5）。"""
    ops = ops_for(tmp_path)
    ops.dialog = DialogInfo(title="磁碟區沒有回應", buttons=("重試", "取消"))
    outcome, _, _ = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert "磁碟區沒有回應" in outcome.detail
    assert "重試" in outcome.detail


def test_dialog_with_no_title_does_not_crash(tmp_path):
    """pywinauto 對某些視窗回 None；停機路徑一掛，就只剩 traceback。"""
    ops = ops_for(tmp_path)
    ops.dialog = DialogInfo(title=None)
    outcome, _, _ = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert outcome.status is Status.HALTED_UNKNOWN_DIALOG


def test_whitelisted_dialog_uses_its_configured_status(tmp_path):
    from lib.config import DialogRule

    ops = ops_for(tmp_path)
    ops.dialog = DialogInfo(title="檔案已存在")
    rule = DialogRule(title_like="*已存在*", action="Cancel", result_status="FAILED_TARGET_EXISTS")
    outcome, _, _ = run_one(
        tmp_path, orch.Task("M1", "AAMA"), ops=ops, ctx_kw={"dialog_rules": (rule,)}
    )
    assert outcome.status is Status.FAILED_TARGET_EXISTS
    assert not outcome.status.aborts_batch


def test_completion_dialog_is_not_treated_as_unknown(tmp_path):
    """
    ZIP 的完成對話框長得像「一個沒被白名單收錄的視窗」。守衛若不認得它，
    每一次成功的匯出都會變成停機。
    """
    outcome, ops, ctx = run_one(
        tmp_path, orch.Task("M1", "ZIP"), ops_kw={"produces": {("M1", "ZIP"): ("M1.zip",)}}
    )
    assert outcome.status is Status.SUCCESS


# ── UI 失敗 ──────────────────────────────────────────────────────────


def test_ui_failure_is_reported_without_aborting(tmp_path):
    """
    定位不到控制項多半是設定或語系問題。中止整批的話使用者一次只看到一個
    錯誤；不中止則一次看到全部，「哪些格式壞了、哪些好」的資訊更有用。
    """
    from lib.uia import ControlNotFoundError

    ops = ops_for(tmp_path, fail_on={"dcu_set_format": ControlNotFoundError("name", "File Type", None)})
    outcome, _, _ = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert outcome.status is Status.FAILED_UI
    assert not outcome.status.aborts_batch


def test_ui_failure_message_reaches_the_log(tmp_path):
    from lib.uia import UiaError

    ops = ops_for(tmp_path, fail_on={"dcu_set_destination": UiaError("欄位是唯讀的")})
    outcome, _, _ = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert "唯讀" in outcome.detail


def test_ui_failure_before_trigger_leaves_nothing_behind(tmp_path):
    from lib.uia import UiaError

    ops = ops_for(tmp_path, fail_on={"dcu_select": UiaError("清單不見了")})
    outcome, _, ctx = run_one(tmp_path, orch.Task("M1", "AAMA"), ops=ops)
    assert "dcu_run" not in ops.names()
    assert list(ctx.temp_dir.iterdir()) == []


# ── 暫存夾不變式 ─────────────────────────────────────────────────────


def test_batch_runs_every_task_and_records_each(tmp_path):
    ops = ops_for(
        tmp_path,
        explorer_models=("M1", "M2"),
        produces={
            ("M1", "ZIP"): ("M1.zip",),
            ("M2", "ZIP"): ("M2.zip",),
            ("M1", "ASTM"): ("M1.dxf",),
            ("M2", "ASTM"): ("M2.dxf",),
        },
    )
    ctx, _ = make_ctx(tmp_path, ops)
    tasks = orch.plan_tasks(("M1", "M2"), ("ZIP", "ASTM"))
    records = orch.run_batch(tasks, ctx, now_fn=lambda: WHEN)
    assert len(records) == 4
    assert all(r.status is Status.SUCCESS for r in records)
    assert (ctx.output_dir / "M2" / "M2.dxf").is_file()


def test_batch_stops_after_an_aborting_status(tmp_path):
    """
    FAILED_MOVE 與 HALTED_UNKNOWN_DIALOG 之後繼續跑只會製造更多同樣的
    失敗，而且畫面上還擋著東西。停下來讓人處理。
    """
    ops = ops_for(tmp_path, explorer_models=("M1", "M2"))
    ops.dialog = DialogInfo(title="不明的視窗")
    ctx, _ = make_ctx(tmp_path, ops)
    tasks = orch.plan_tasks(("M1", "M2"), ("ASTM",))
    records = orch.run_batch(tasks, ctx, now_fn=lambda: WHEN)
    assert len(records) == 1
    assert records[0].status is Status.HALTED_UNKNOWN_DIALOG


def test_batch_continues_after_a_non_aborting_failure(tmp_path):
    ops = ops_for(
        tmp_path,
        explorer_models=("M1", "M2"),
        selection_override={"M1": ()},
        produces={("M2", "ASTM"): ("M2.dxf",)},
    )
    ctx, _ = make_ctx(tmp_path, ops)
    records = orch.run_batch(
        orch.plan_tasks(("M1", "M2"), ("ASTM",)), ctx, now_fn=lambda: WHEN
    )
    assert [r.status for r in records] == [Status.FAILED_SELECTION, Status.SUCCESS]


def test_batch_skips_what_is_already_done(tmp_path):
    ops = ops_for(tmp_path, explorer_models=("M1", "M2"), produces={("M2", "ASTM"): ("M2.dxf",)})
    ctx, _ = make_ctx(tmp_path, ops)
    records = orch.run_batch(
        orch.plan_tasks(("M1", "M2"), ("ASTM",)),
        ctx,
        now_fn=lambda: WHEN,
        should_skip_fn=lambda t: t.model == "M1",
    )
    assert records[0].status is Status.SKIPPED_ALREADY_DONE
    assert "dcu_run" not in [c[0] for c in ops.calls if c[0] == "dcu_run" and c[1] == "M1"]


def test_batch_reports_each_result_as_it_happens(tmp_path):
    """
    每一筆都要立刻交給呼叫端存進 state.json。整批跑完才存的話，中途當機
    就等於整批白做——而使用者下一次重跑會從頭開始。
    """
    ops = ops_for(tmp_path, explorer_models=("M1",), produces={("M1", "ASTM"): ("M1.dxf",)})
    ctx, _ = make_ctx(tmp_path, ops)
    seen = []
    orch.run_batch(
        orch.plan_tasks(("M1",), ("ASTM",)), ctx, now_fn=lambda: WHEN, on_result=seen.append
    )
    assert len(seen) == 1
    assert seen[0].model == "M1"


def test_batch_turns_a_broken_precondition_into_a_record(tmp_path):
    """
    暫存夾髒掉是流程前提被破壞，但不該讓整支腳本拋 traceback：使用者要
    看到的是一句「暫存夾不是空的」加上正常的摘要。
    """
    ops = ops_for(tmp_path)
    ctx, _ = make_ctx(tmp_path, ops)
    (ctx.temp_dir / "殘留.dxf").write_text("x", encoding="utf-8")
    records = orch.run_batch(orch.plan_tasks(("M1",), ("ASTM",)), ctx, now_fn=lambda: WHEN)
    assert len(records) == 1
    assert records[0].status.aborts_batch
    assert "暫存" in records[0].detail


def test_refuses_to_start_when_the_temp_dir_is_not_empty(tmp_path):
    """
    觸發前暫存夾必為空——這條不變式是「暫存夾裡的東西必屬當前任務」的
    唯一依據。破了它，上一次的殘留會被當成這次的產出歸到錯的 model。
    """
    ops = ops_for(tmp_path)
    ctx, _ = make_ctx(tmp_path, ops)
    (ctx.temp_dir / "上次留下的.dxf").write_text("x", encoding="utf-8")
    with pytest.raises(orch.OrchestrationError, match="暫存"):
        orch.run_task(orch.Task("M1", "AAMA"), ctx)
