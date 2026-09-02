"""
把一個任務從「按下去」帶到「檔案躺在正確的資料夾裡」（design.md §2.3）。

這是唯一一層把所有模組串起來的地方，而它在開發機上沒有 AccuMark 可以跑。
因此不純的東西全部注入：UI 操作交給 ops、時鐘與睡眠是參數。剩下的邏輯
——任務怎麼排、什麼情況算哪一種失敗、產出該搬去哪——就都能在這台機器上
測完。

**讀回驗證留在這一層，不在 uia。** TD-9 的保證是「一次只有一個 model
進去」，而 uia.select_single 刻意不做讀回：驗證若藏在操作函式裡，這一層
漏掉那一步時，整合測試照樣是綠的。放在這裡，替身就能回報「我選了兩項」，
測試才驗得出流程真的擋下來、真的沒按執行鈕。

ops 要提供的介面（真實實作見 lib/ops.py，測試用替身）：

    available_models(fmt)          該視窗的清單裡有哪些 model
    explorer_select(model)         Explorer 選一個
    explorer_selection()           Explorer 目前選了哪些      ← 讀回驗證
    export_zip(model, dest)        File → Export Zip 整段精靈
    dcu_set_format(fmt)            切 File Type
    dcu_select(model)              DCU 選一個
    dcu_selection()                DCU 目前選了哪些           ← 讀回驗證
    dcu_set_destination(dest)      填 Destination Path
    dcu_run(model, fmt)            按執行鈕
    foreground_dialog()            前景對話框，沒有就 None
    dismiss_completion()           按完成對話框的 OK
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from . import archival, completion, dialog_guard
from .reporting import Status
from .uia import UiaError

ZIP_FORMAT = "ZIP"


class OrchestrationError(RuntimeError):
    """流程的前提被破壞，繼續下去會產生錯誤的結果。整批停下。"""


# ── 任務 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Task:
    model: str
    fmt: str

    @property
    def is_zip(self) -> bool:
        return self.fmt == ZIP_FORMAT

    @property
    def label(self) -> str:
        """殘留資料夾用的名字，例如 `AAMA_M1`。"""
        return archival.task_label(self.fmt, self.model)

    def __str__(self) -> str:
        return f"{self.model} / {self.fmt}"


@dataclass(frozen=True)
class TaskOutcome:
    status: Status
    outputs: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class RunContext:
    """一次執行的全部參數。ops 之外都是資料，方便整包印進日誌。"""

    temp_dir: Path
    output_dir: Path
    ops: Any
    expected_outputs: Mapping[str, Tuple[str, ...]]
    completion_title_like: str
    dialog_rules: Tuple[Any, ...]
    poll_interval_ms: int
    stable_samples: int
    quiet_period_sec: float
    timeout_sec: float
    add_format_suffix: bool
    clock_fn: Callable[[], float]
    sleep_fn: Callable[[float], None]
    now_fn: Callable[[], Any]


def _fold(name: str) -> str:
    return name.strip().casefold()


def plan_tasks(
    models: Sequence[str],
    formats: Sequence[str],
    only: Optional[str] = None,
    only_format: Optional[str] = None,
) -> Tuple[Task, ...]:
    """
    純函式：要做哪些任務，依什麼順序。

    **依格式分組**，同一種格式的所有 model 排在一起。DCU 的 File Type
    因此只切換一次；四個 model 逐一輪流要切八次，而每一次切換都是一次
    可能失敗的 UI 操作。TD-9 說 UI 操作次數是這個專案的主要風險來源，
    不是效能問題——所以順序照著風險排，不照直覺排。
    """
    wanted = _fold(only) if only else None
    wanted_fmt = _fold(only_format) if only_format else None
    tasks = []
    for fmt in formats:
        if wanted_fmt is not None and _fold(fmt) != wanted_fmt:
            continue
        for model in models:
            if wanted is not None and _fold(model) != wanted:
                continue
            tasks.append(Task(model=model, fmt=fmt))
    return tuple(tasks)


# ── 守衛：把三種判定接到 completion 的兩個鉤子上 ──────────────────────


class _Guard:
    """
    每輪輪詢看一次前景視窗，把 dialog_guard 的三種判定翻譯成
    completion 要的 abort_fn／signal_fn。

    **poll() 同時是 abort_fn**：completion 第一階段的順序是先 abort_fn
    再 signal_fn，而 stability 在第二階段每次取樣前也會呼叫 abort_fn。
    也就是說整個等待期間 poll() 一定會被呼叫到，signal() 只要讀它留下的
    狀態就好——這樣一輪只查一次前景視窗，而不是查兩次。
    """

    def __init__(self, ops, rules, expected_completion: Optional[str]):
        self._ops = ops
        self._rules = rules
        self._expected = expected_completion
        self.verdict = None
        self.completion_seen = False

    def poll(self) -> bool:
        verdict = dialog_guard.check_foreground(
            self._ops.foreground_dialog, self._rules, self._expected
        )
        if verdict is None:
            return False
        if verdict.completion:
            self.completion_seen = True
            return False
        # 白名單命中或未知——兩者都要停下這個任務的等待。
        self.verdict = verdict
        return True

    def signal(self) -> bool:
        return self.completion_seen


def _guard_outcome(verdict) -> TaskOutcome:
    """守衛喊停時，這個任務算哪一種結果。"""
    if verdict.known and verdict.result_status:
        try:
            status = Status(verdict.result_status)
        except ValueError:
            # 設定驗證應該早就擋掉了；真的漏進來就當未知，不要猜。
            status = Status.HALTED_UNKNOWN_DIALOG
    else:
        status = Status.HALTED_UNKNOWN_DIALOG
    return TaskOutcome(status=status, detail=verdict.description)


# ── 暫存夾 ───────────────────────────────────────────────────────────


def _sample(temp_dir: Path) -> dict:
    """{檔名: 大小}。completion 與 stability 都吃這個形狀。"""
    d = Path(temp_dir)
    if not d.is_dir():
        return {}
    return {p.name: p.stat().st_size for p in d.iterdir() if p.is_file()}


def _existing_names(directory: Path) -> set:
    d = Path(directory)
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


def _move(files: Sequence[str], dest_dir: Path, task: Task, ctx: RunContext) -> Tuple[str, ...]:
    """規劃並搬移一組檔案。保留原檔名、絕不覆蓋（TD-8）。"""
    moves = archival.plan(
        files=files,
        fmt=task.fmt,
        dest_dir=dest_dir,
        existing=_existing_names(dest_dir),
        add_format_suffix=ctx.add_format_suffix,
    )
    return archival.execute(moves, ctx.temp_dir)


# ── 觸發 ─────────────────────────────────────────────────────────────


def _verify_selection(actual: Sequence[str], model: str) -> Optional[str]:
    """
    選取必須恰好是這一個 model。不符就回一句話說明實際選到什麼。

    這是 TD-9 的機械保證。DCU 記住上次的選取、Select 沒清乾淨，兩個
    model 都亮著——執行下去會產出一個把裁片併在一起的 DXF，而檔名還是
    對的。看不出來的錯誤要在發生前擋掉，不是事後檢查。
    """
    names = tuple(actual)
    if len(names) == 1 and _fold(names[0]) == _fold(model):
        return None
    if not names:
        return "清單裡沒有任何項目被選取"
    return f"選取的不是恰好這一個 model：實際選到 {list(names)}"


def _trigger(task: Task, ctx: RunContext) -> Optional[TaskOutcome]:
    """
    做完「按下去」為止。回傳 None 表示已觸發、可以開始等；
    回傳 TaskOutcome 表示還沒觸發就結束了（沒有任何檔案被動到）。
    """
    ops = ctx.ops
    if task.is_zip:
        ops.explorer_select(task.model)
        problem = _verify_selection(ops.explorer_selection(), task.model)
        if problem:
            return TaskOutcome(status=Status.FAILED_SELECTION, detail=problem)
        ops.export_zip(task.model, ctx.temp_dir)
        return None

    ops.dcu_set_format(task.fmt)
    ops.dcu_select(task.model)
    problem = _verify_selection(ops.dcu_selection(), task.model)
    if problem:
        return TaskOutcome(status=Status.FAILED_SELECTION, detail=problem)
    ops.dcu_set_destination(ctx.temp_dir)
    ops.dcu_run(task.model, task.fmt)
    return None


# ── 一個任務 ─────────────────────────────────────────────────────────


def run_task(task: Task, ctx: RunContext) -> TaskOutcome:
    """
    跑一個任務：確認前提 → 觸發 → 等完成 → 歸檔。

    這個函式不拋例外給呼叫端（OrchestrationError 除外）——每一種失敗都
    翻譯成一個狀態，讓主流程的迴圈只需要看 status.aborts_batch。
    """
    if _sample(ctx.temp_dir):
        raise OrchestrationError(
            f"暫存夾 {ctx.temp_dir} 不是空的，無法確定裡面的東西屬於哪一次匯出。"
            "請先確認那些檔案是否還需要，再重跑"
        )

    if not any(_fold(m) == _fold(task.model) for m in ctx.ops.available_models(task.fmt)):
        where = "AccuMark Explorer" if task.is_zip else "Data Conversion Utility"
        return TaskOutcome(
            status=Status.SKIPPED_NOT_FOUND,
            detail=f"{where} 的清單裡找不到 {task.model}",
        )

    try:
        early = _trigger(task, ctx)
    except UiaError as exc:
        return TaskOutcome(status=Status.FAILED_UI, detail=str(exc))
    if early is not None:
        return early

    guard = _Guard(
        ctx.ops,
        ctx.dialog_rules,
        ctx.completion_title_like if task.is_zip else None,
    )
    expected_count = len(ctx.expected_outputs.get(task.fmt, ()))
    sample_fn = lambda: _sample(ctx.temp_dir)  # noqa: E731

    signal_fn = (
        guard.signal
        if task.is_zip
        else completion.count_signal(sample_fn, expected_count)
    )

    result = completion.wait_for_completion(
        signal_fn=signal_fn,
        sample_fn=sample_fn,
        sleep_fn=ctx.sleep_fn,
        clock_fn=ctx.clock_fn,
        abort_fn=guard.poll,
        expected_count=expected_count or None,
        poll_interval_ms=ctx.poll_interval_ms,
        stable_samples=ctx.stable_samples,
        quiet_period_sec=ctx.quiet_period_sec,
        timeout_sec=ctx.timeout_sec,
    )

    if result.status == completion.ABORTED:
        # 守衛喊停。暫存夾裡可能有半成品，但畫面上有東西擋著，
        # 現在動檔案只會讓現場更難判讀——留給使用者看。
        return _guard_outcome(guard.verdict) if guard.verdict else TaskOutcome(
            status=Status.HALTED_UNKNOWN_DIALOG, detail="等待期間被中止"
        )

    if result.status == completion.TIMEOUT:
        return _timeout_outcome(task, ctx, result)

    if task.is_zip:
        # 訊號到了、檔案也穩定了，才按 OK。反過來的話，AccuMark 可能
        # 在對話框關掉時才把最後一段寫出去。
        try:
            ctx.ops.dismiss_completion()
        except UiaError as exc:
            return TaskOutcome(status=Status.FAILED_UI, detail=str(exc))

    return _archive(task, ctx, result.files)


def run_batch(
    tasks: Sequence[Task],
    ctx: RunContext,
    *,
    now_fn: Callable[[], Any],
    should_skip_fn: Optional[Callable[[Task], bool]] = None,
    on_result: Optional[Callable[[Any], None]] = None,
) -> Tuple[Any, ...]:
    """
    跑完一批任務，回傳逐筆 TaskRecord。

    on_result 在**每一筆**之後立刻被呼叫，讓呼叫端把狀態寫進 state.json。
    整批跑完才存的話，中途當機等於整批白做——而使用者重跑會從第一個開始。

    status.aborts_batch 的任務之後就停：繼續跑只會製造更多同樣的失敗，
    而且畫面上可能還擋著東西。
    """
    from .reporting import TaskRecord  # 延後匯入，避免與 reporting 形成環

    records = []
    for task in tasks:
        started = _stamp(now_fn)
        if should_skip_fn is not None and should_skip_fn(task):
            outcome = TaskOutcome(
                status=Status.SKIPPED_ALREADY_DONE, detail="上次已完成且產出還在"
            )
        else:
            try:
                outcome = run_task(task, ctx)
            except OrchestrationError as exc:
                # 前提被破壞。這是整批的問題，不是這一個任務的問題，
                # 但仍要變成一筆紀錄——使用者要看到摘要，不是 traceback。
                outcome = TaskOutcome(status=Status.FAILED_MOVE, detail=str(exc))

        record = TaskRecord(
            model=task.model,
            fmt=task.fmt,
            status=outcome.status,
            started_at=started,
            finished_at=_stamp(now_fn),
            outputs=outcome.outputs,
            detail=outcome.detail,
        )
        records.append(record)
        if on_result is not None:
            on_result(record)
        if outcome.status.aborts_batch:
            break
    return tuple(records)


def _stamp(now_fn) -> str:
    value = now_fn()
    return value.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(value, "strftime") else str(value)


def _timeout_outcome(task: Task, ctx: RunContext, result) -> TaskOutcome:
    """
    逾時：殘留搬到 `_逾時殘留\\<任務>\\`，**不刪**。

    那可能是寫到一半的檔，也可能是完整但訊號沒來的檔——這裡分不出來。
    刪掉的話使用者連「它到底有沒有寫出東西」都不知道；留在暫存夾則會破壞
    下一個任務的不變式。
    """
    detail = result.reason
    if result.files:
        residue = archival.residue_dir(
            ctx.output_dir, archival.TIMEOUT_RESIDUE_DIRNAME, task.label
        )
        try:
            _move(result.files, residue, task, ctx)
            detail += f"；殘留已移至 {residue}"
        except archival.ArchivalError as exc:
            return TaskOutcome(status=Status.FAILED_MOVE, detail=f"{detail}；搬移殘留失敗：{exc}")
    return TaskOutcome(status=Status.FAILED_TIMEOUT, detail=detail)


def _archive(task: Task, ctx: RunContext, files: Sequence[str]) -> TaskOutcome:
    """
    歸檔。主檔名對不上當前 model 的檔案走 `_未歸類\\`（TD-9 的防線）。

    先搬不屬於這個 model 的：萬一它失敗，屬於這個 model 的還留在暫存夾，
    整個任務就是乾淨的失敗，而不是「一半在輸出資料夾、一半在暫存夾」。
    """
    owned, foreign = archival.check_ownership(files, task.model)
    notes = []

    try:
        if foreign:
            residue = archival.residue_dir(
                ctx.output_dir, archival.UNCLASSIFIED_DIRNAME, task.label
            )
            _move(foreign, residue, task, ctx)
            notes.append(
                f"有 {len(foreign)} 個檔案的名稱對不上這個 model，已移至未歸類：{list(foreign)}"
            )
        outputs = _move(owned, archival.model_dir(ctx.output_dir, task.model), task, ctx)
    except archival.ArchivalError as exc:
        return TaskOutcome(status=Status.FAILED_MOVE, detail=str(exc))

    renamed = [m for m in files if m not in owned and m not in foreign]
    if renamed:  # 理論上不會發生，留一句話比靜默好
        notes.append(f"未處理的檔案：{renamed}")

    return TaskOutcome(status=Status.SUCCESS, outputs=outputs, detail="；".join(notes))
