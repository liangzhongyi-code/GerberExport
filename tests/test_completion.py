"""
D2 完成偵測測試（對應 spec: batch-export「完成偵測以 UI 訊號為主、檔案穩定為輔」
/ TD-4 修訂版）。

C1 的 stability 只回答「檔案寫完了沒」，但審查實測抓到的兩個提前判定缺陷
（`.dxf` 穩定後 `.rul` 才出現、寫入暫停跨越取樣窗）根源都一樣：**太早開始算**。
沒有任何有限的觀察期能對抗任意長的停頓，所以 TD-4 改成兩道關卡——先等
AccuMark 自己說「做完了」（ZIP 的完成對話框、DXF 的預期檔案數），訊號到了
才開始看檔案。

這個檔案守的就是那條線：**訊號到達前，stability.wait_for_stable 一次都不能被
呼叫。** 用 monkeypatch 把它包起來記錄每次呼叫時的假時鐘，任何一次早於訊號
就是紅燈。

訊號、取樣、睡眠、時鐘全部注入，並以「時間軸」描述暫存夾的變化，讓
「訊號在第 250 秒才到」這種情境能在幾毫秒內確定性地驗完。
"""

import dataclasses

import pytest

from lib import completion as cp
from lib import stability as st


# ── 測試替身 ──────────────────────────────────────────────────────────


class FakeClock:
    """假時鐘：sleep 直接推進時間，測試不用真的等。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Timeline:
    """
    以「第 t 秒起暫存夾長這樣」描述檔案變化。

    比 test_stability 的「一串預先排好的取樣」更合適：這裡 sample_fn 會被
    第一階段的逾時殘留、count_signal、第二階段的穩定判定三處呼叫，
    用序列 pop 會讓「第幾次取樣」跟「第幾秒」對不上；改用時間軸就沒有這個問題。
    """

    def __init__(self, clock, events):
        self.clock = clock
        self.events = sorted(events, key=lambda e: e[0])

    def sample(self):
        current = {}
        for at, snapshot in self.events:
            if self.clock.now >= at:
                current = snapshot
        return dict(current)


class SignalAt:
    """從第 t 秒起訊號為 True（ZIP 的完成對話框一出現就會一直在）。"""

    def __init__(self, clock, at):
        self.clock = clock
        self.at = at
        self.polled_at = []

    def __call__(self):
        self.polled_at.append(self.clock.now)
        return self.at is not None and self.clock.now >= self.at


class StabilitySpy:
    """
    包住 stability.wait_for_stable，記錄每次被呼叫時的假時鐘與收到的參數。

    這是整個檔案最重要的探針：「訊號前有沒有開始算穩定」不能從結果反推——
    提前開始算、剛好又沒踩到停頓的話，結果一樣是 complete，只有呼叫時間點
    能證明流程走對了。
    """

    def __init__(self, clock, monkeypatch):
        self.clock = clock
        self.called_at = []
        self.kwargs = []
        original = st.wait_for_stable

        def spy(**kwargs):
            self.called_at.append(self.clock.now)
            self.kwargs.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(st, "wait_for_stable", spy)


def run(
    events,
    *,
    signal_at=None,
    signal_fn=None,
    expected_count=None,
    abort_fn=None,
    stable_samples=3,
    poll_ms=500,
    quiet_period_sec=0.0,
    timeout=300,
    monkeypatch=None,
):
    """
    把一條時間軸餵進 wait_for_completion。

    signal_at：訊號從第幾秒起為 True（None 代表永遠不來）。
    signal_fn：若要用 count_signal 這類自訂訊號，傳入接收 sample_fn 的工廠。
    回傳 (result, clock, spy)；spy 只在有給 monkeypatch 時才存在。
    """
    clock = FakeClock()
    timeline = Timeline(clock, events)
    spy = StabilitySpy(clock, monkeypatch) if monkeypatch is not None else None
    signal = signal_fn(timeline.sample) if signal_fn else SignalAt(clock, signal_at)

    result = cp.wait_for_completion(
        signal_fn=signal,
        sample_fn=timeline.sample,
        sleep_fn=clock.sleep,
        clock_fn=clock.time,
        abort_fn=abort_fn,
        expected_count=expected_count,
        poll_interval_ms=poll_ms,
        stable_samples=stable_samples,
        quiet_period_sec=quiet_period_sec,
        timeout_sec=timeout,
    )
    return result, clock, spy


def abort_from(clock, at):
    """從第 t 秒起守衛喊停（模擬未知對話框在途中彈出）。"""
    return lambda: clock.now >= at


# ── 參數驗證：明顯不合理的值要在開跑前就擋掉 ──────────────────────────


def call_with(**overrides):
    clock = FakeClock()
    params = dict(
        signal_fn=lambda: True,
        sample_fn=lambda: {"a.zip": 100},
        sleep_fn=clock.sleep,
        clock_fn=clock.time,
        abort_fn=None,
        expected_count=None,
        poll_interval_ms=500,
        stable_samples=3,
        quiet_period_sec=0.0,
        timeout_sec=300,
    )
    params.update(overrides)
    return cp.wait_for_completion(**params)


@pytest.mark.parametrize("poll_ms", [0, -500])
def test_poll_interval_must_be_positive(poll_ms):
    """0 或負的間隔會變成沒有睡眠的緊迴圈，把 CPU 吃滿還讓逾時失去意義。"""
    with pytest.raises(ValueError):
        call_with(poll_interval_ms=poll_ms)


@pytest.mark.parametrize("timeout", [0, -1])
def test_timeout_must_be_positive(timeout):
    with pytest.raises(ValueError):
        call_with(timeout_sec=timeout)


@pytest.mark.parametrize("stable_samples", [0, 1])
def test_stable_samples_below_two_is_rejected_before_waiting(stable_samples):
    """
    stability 自己也會擋這個值，但它要到第二階段才被呼叫——訊號還沒來之前
    可能已經白等了五分鐘，才發現參數根本不合法。這裡在開跑前就擋。
    """
    polled = []
    with pytest.raises(ValueError):
        call_with(
            stable_samples=stable_samples,
            signal_fn=lambda: polled.append(1) or False,
            timeout_sec=5,
        )
    assert polled == [], "參數不合法卻已經開始輪詢訊號"


@pytest.mark.parametrize("expected", [0, -1])
def test_expected_count_must_be_at_least_one(expected):
    """預期 0 個檔案等於「什麼都不用等」，那是設定填錯，不是合法需求。"""
    with pytest.raises(ValueError):
        call_with(expected_count=expected)


def test_quiet_period_must_not_be_negative():
    with pytest.raises(ValueError):
        call_with(quiet_period_sec=-1.0)


# ── 第一階段：訊號到達前絕不開始算穩定 ────────────────────────────────
#
# 這是 TD-4 修訂的全部意義。審查實測的兩個提前判定缺陷都出在「太早開始算」，
# 訊號解決的正是「該從什麼時候開始算」這個問題。


def test_zip_dialog_then_stability_then_complete(monkeypatch):
    """
    Spec「ZIP 完成對話框出現」：對話框出現 → 才開始穩定確認 → 穩定後 complete。

    暫存夾裡的檔案從第 0 秒就已經穩定了，但對話框第 2 秒才出現；
    穩定判定必須在第 2 秒之後才開始，不能因為檔案「看起來好了」就提前。
    """
    result, _, spy = run(
        [(0.0, {"M001.zip": 4096})],
        signal_at=2.0,
        monkeypatch=monkeypatch,
    )
    assert result.status == "complete"
    assert result.signal_seen is True
    assert result.files == ("M001.zip",)
    assert spy.called_at, "訊號到了卻沒有做穩定確認"
    assert all(t >= 2.0 for t in spy.called_at), (
        f"訊號前就開始算穩定了：{spy.called_at}"
    )


def test_stability_is_never_computed_while_the_signal_is_missing(monkeypatch):
    """訊號始終沒來，wait_for_stable 的呼叫次數必須是 0——不是「很少」。"""
    result, _, spy = run(
        [(0.0, {"M001.zip": 4096})],
        signal_at=None,
        timeout=10,
        monkeypatch=monkeypatch,
    )
    assert result.status == "timeout"
    assert spy.called_at == [], f"訊號沒來卻算了穩定：{spy.called_at}"


def test_stable_files_before_the_signal_do_not_count():
    """
    行為面的守衛：檔案第 0 秒就穩定，訊號第 10 秒才到，完成時間必須 ≥ 10 秒。

    這條不靠探針，專門抓「先算穩定、再看訊號」這種順序寫反的實作——
    它的結果一樣是 complete，只有耗時會露餡。
    """
    result, clock, _ = run([(0.0, {"M001.zip": 4096})], signal_at=10.0)
    assert result.status == "complete"
    assert clock.now >= 10.0, "訊號還沒到就判定完成了"


def test_signal_is_polled_at_the_configured_interval():
    """輪詢間隔一律從設定來，不能有自己寫死的秒數。"""
    _, clock, _ = run([(0.0, {"a.zip": 10})], signal_at=3.0, poll_ms=250)
    assert all(s == pytest.approx(0.25) for s in clock.sleeps)


def test_signal_is_checked_before_the_first_sleep():
    """匯出瞬間完成時（小 model），不該白等一輪才發現訊號早就在了。"""
    clock = FakeClock()
    signal = SignalAt(clock, 0.0)
    cp.wait_for_completion(
        signal_fn=signal,
        sample_fn=lambda: {"a.zip": 10},
        sleep_fn=clock.sleep,
        clock_fn=clock.time,
        abort_fn=None,
        expected_count=None,
        poll_interval_ms=500,
        stable_samples=2,
        quiet_period_sec=0.0,
        timeout_sec=30,
    )
    assert signal.polled_at[0] == 0.0


# ── 第二階段：訊號到了，但檔案還在寫 ──────────────────────────────────
#
# TD-4 選項 B 被否決的理由：「訊號出現時檔案未必已 flush」。訊號只決定
# 從什麼時候開始看，看到穩定為止仍然是必要條件。


def test_signal_while_still_writing_waits_for_stability():
    """
    Spec「完成對話框出現但暫存夾仍在寫入」：第 1 秒對話框出現，檔案卻長到第 4 秒。
    不判定完成、繼續等到穩定。
    """
    growing = [(float(t), {"M001.zip": 1000 * (t + 1)}) for t in range(0, 5)]
    result, clock, _ = run(growing, signal_at=1.0)
    assert result.status == "complete"
    assert result.files == ("M001.zip",)
    # 最後一次變動在第 4 秒，之後還要連續 3 次相同（2 個間隔）
    assert clock.now >= 4.0 + 2 * 0.5


def test_signal_before_any_file_exists_waits_for_the_file():
    """對話框先跳、檔案晚 2 秒才落地——空的暫存夾不是「穩定地沒東西」。"""
    result, clock, _ = run(
        [(0.0, {}), (3.0, {"M001.zip": 4096})],
        signal_at=1.0,
    )
    assert result.status == "complete"
    assert result.files == ("M001.zip",)
    assert clock.now >= 3.0


def test_zero_byte_placeholder_after_signal_is_not_complete():
    """
    確認穩定判定真的交給 stability，而不是自己另寫一套漏掉零位元組規則。
    AccuMark 可能先建佔位檔再寫入；搬走空檔跟搬走半個檔一樣是靜默損毀。
    """
    result, clock, _ = run(
        [(0.0, {"M001.zip": 0}), (3.0, {"M001.zip": 4096})],
        signal_at=0.0,
    )
    assert result.status == "complete"
    assert clock.now >= 3.0, "零位元組佔位檔被當成寫完了"


def test_late_sibling_file_is_caught_by_the_quiet_period():
    """
    quiet_period_sec 必須原封不動傳給 stability。

    審查實測的缺陷情境：`.dxf` 穩定三次之後 `.rul` 才開始寫。若這裡忘了
    把觀察期傳下去，C1 修好的洞會在 D2 重新打開，而所有其他測試照樣是綠的。
    """
    events = [
        (0.0, {"M001.dxf": 100}),
        (0.5, {"M001.dxf": 512000}),
        (2.5, {"M001.dxf": 512000, "M001.rul": 50}),  # 穩定視窗關閉後才冒出
        (3.0, {"M001.dxf": 512000, "M001.rul": 800}),
    ]
    result, _, _ = run(events, signal_at=0.0, quiet_period_sec=2.0)
    assert result.status == "complete"
    assert set(result.files) == {"M001.dxf", "M001.rul"}, (
        "附帶檔被漏掉了——它會留在暫存夾，而任務記為 SUCCESS"
    )


# ── DXF files 模式：以預期檔案數當訊號 ────────────────────────────────
#
# DCU 的 Results 窗格能不能用 UIA 讀到要 dry-run 回來才知道，
# 所以預設用「暫存夾檔案數達 len(expected_outputs[fmt])」當訊號。


def test_count_signal_is_false_below_expected():
    signal = cp.count_signal(lambda: {"M001.dxf": 100}, expected_count=2)
    assert signal() is False


def test_count_signal_is_true_at_expected():
    signal = cp.count_signal(lambda: {"M001.dxf": 100, "M001.rul": 50}, expected_count=2)
    assert signal() is True


def test_count_signal_is_true_above_expected():
    """多出來的檔案是歸檔層主檔名防線（_未歸類）的事，訊號這裡只管「齊了沒」。"""
    three = {"a.dxf": 1, "a.rul": 1, "extra.txt": 1}
    assert cp.count_signal(lambda: three, expected_count=2)() is True


def test_count_signal_reads_fresh_each_time():
    """訊號是活的：每次呼叫都要重新取樣，不能把第一次的結果記起來。"""
    state = {"files": {}}
    signal = cp.count_signal(lambda: state["files"], expected_count=1)
    assert signal() is False
    state["files"] = {"a.dxf": 10}
    assert signal() is True


@pytest.mark.parametrize("expected", [0, -1])
def test_count_signal_rejects_non_positive_expected(expected):
    with pytest.raises(ValueError):
        cp.count_signal(lambda: {}, expected_count=expected)


def test_dxf_files_mode_waits_for_the_second_file(monkeypatch):
    """
    Spec「DXF 以預期檔案數判定」：expected 2，先只有 `.dxf` → 不算；
    `.rul` 出現且兩者皆穩定 → complete。穩定判定只能在第 2 個檔案出現後開始。
    """
    events = [
        (0.0, {}),
        (1.0, {"M001.dxf": 100}),
        (2.0, {"M001.dxf": 512000}),
        (4.0, {"M001.dxf": 512000, "M001.rul": 800}),
    ]
    result, clock, spy = run(
        events,
        signal_fn=lambda sample: cp.count_signal(sample, expected_count=2),
        expected_count=2,
        monkeypatch=monkeypatch,
    )
    assert result.status == "complete"
    assert set(result.files) == {"M001.dxf", "M001.rul"}
    assert clock.now >= 4.0
    assert all(t >= 4.0 for t in spy.called_at), (
        f"只有 1 個檔案時就開始算穩定了：{spy.called_at}"
    )


def test_dxf_files_mode_times_out_with_only_one_file():
    """Spec 同上：只出現 1 個時 MUST 繼續等待直到逾時，殘留的那個要回報。"""
    result, _, _ = run(
        [(1.0, {"M001.dxf": 512000})],
        signal_fn=lambda sample: cp.count_signal(sample, expected_count=2),
        expected_count=2,
        timeout=20,
    )
    assert result.status == "timeout"
    assert result.signal_seen is False
    assert result.files == ("M001.dxf",)


# ── 第三階段：訊號來了、穩定了、但檔案數不夠 ──────────────────────────
#
# ZIP 用對話框當訊號時，expected_count 是獨立的第二道檢查；
# results_text 模式的 DXF 也一樣。訊號說做完了、檔案也不動了，
# 但數量少於預期——這不是完成，是「還沒寫到下一個」。


def test_signal_and_stable_but_short_count_keeps_waiting():
    """
    第 1 秒訊號到，`.dxf` 立刻穩定，但 `.rul` 第 6 秒才出現。
    expected 2 → 第一次穩定（只有 1 個）不算完成，要等到第二個也穩定。
    """
    events = [
        (0.0, {"M001.dxf": 512000}),
        (6.0, {"M001.dxf": 512000, "M001.rul": 50}),
        (7.0, {"M001.dxf": 512000, "M001.rul": 800}),
    ]
    result, clock, _ = run(events, signal_at=1.0, expected_count=2)
    assert result.status == "complete"
    assert set(result.files) == {"M001.dxf", "M001.rul"}
    assert clock.now >= 7.0


def test_signal_and_stable_but_short_count_times_out():
    """一直沒補齊 → timeout，且 signal_seen 要是 True，日誌才分得出「訊號沒來」跟「來了但沒齊」。"""
    result, _, _ = run(
        [(0.0, {"M001.dxf": 512000})],
        signal_at=1.0,
        expected_count=2,
        timeout=30,
    )
    assert result.status == "timeout"
    assert result.signal_seen is True
    assert result.files == ("M001.dxf",)


def test_file_count_equal_to_expected_is_enough():
    """
    恰好等於預期就是完成。寫成 `<=` 的話，每個任務都會在檔案齊了之後
    繼續空等到逾時——12 個任務乘 300 秒，而且每一個都記 FAILED_TIMEOUT。
    """
    result, clock, _ = run(
        [(0.0, {"M001.dxf": 512000, "M001.rul": 800})],
        signal_at=0.0,
        expected_count=2,
        timeout=30,
    )
    assert result.status == "complete"
    assert clock.now < 30


def test_more_files_than_expected_is_still_complete():
    """expected_outputs 填少了不該讓任務卡死；多出來的全部回報，交給歸檔層判斷。"""
    result, _, _ = run(
        [(0.0, {"M001.dxf": 512000, "M001.rul": 800})],
        signal_at=0.0,
        expected_count=1,
    )
    assert result.status == "complete"
    assert set(result.files) == {"M001.dxf", "M001.rul"}


def test_expected_count_none_accepts_any_stable_set():
    """ZIP 不填 expected_count 時，只看訊號與穩定。"""
    result, _, _ = run([(0.0, {"M001.zip": 4096})], signal_at=0.0, expected_count=None)
    assert result.status == "complete"
    assert result.files == ("M001.zip",)


# ── 逾時：一段預算，不是兩段 ──────────────────────────────────────────
#
# timeout_sec 是整個任務的預算。若第二階段拿到的是完整的 timeout_sec，
# 訊號在第 299 秒到就能再等 300 秒——設定檔寫 5 分鐘，實際 10 分鐘。


def test_timeout_without_signal_reports_residue_files():
    """
    Spec「逾時訊號未到」：status timeout、signal_seen False，
    殘留檔名要回報——呼叫端要把它們搬去 _逾時殘留\\，不是刪。
    """
    result, clock, _ = run(
        [(0.0, {}), (1.0, {"M001.zip": 2048})],
        signal_at=None,
        timeout=5,
    )
    assert result.status == "timeout"
    assert result.signal_seen is False
    assert result.files == ("M001.zip",)
    assert clock.now >= 5


def test_timeout_without_signal_and_empty_temp_dir():
    result, _, _ = run([(0.0, {})], signal_at=None, timeout=5)
    assert result.status == "timeout"
    assert result.files == ()


def test_total_timeout_is_a_single_budget():
    """
    訊號第 250 秒才到、timeout 300 → 第二階段最多只能等 50 秒。
    檔案永遠在長，整體必須在第 300 秒附近就放棄，而不是第 550 秒。
    """
    forever_growing = [(float(t), {"M001.zip": 100 * (t + 1)}) for t in range(0, 700)]
    result, clock, _ = run(forever_growing, signal_at=250.0, timeout=300)
    assert result.status == "timeout"
    assert result.signal_seen is True
    assert clock.now <= 300.0 + 0.5, f"逾時被算成兩段：總共等了 {clock.now} 秒"
    assert result.elapsed_sec == pytest.approx(clock.now)


def test_stability_receives_the_remaining_budget(monkeypatch):
    """
    直接看傳給 stability 的 timeout_sec：訊號第 250 秒到，剩餘應是 50，不是 300。
    """
    forever_growing = [(float(t), {"M001.zip": 100 * (t + 1)}) for t in range(0, 700)]
    _, _, spy = run(
        forever_growing,
        signal_at=250.0,
        timeout=300,
        monkeypatch=monkeypatch,
    )
    assert spy.kwargs, "沒有呼叫 stability"
    assert spy.kwargs[0]["timeout_sec"] == pytest.approx(50.0)


def test_timeout_after_signal_reports_residue_files():
    """訊號到了但檔案一直沒穩定：逾時一樣要帶殘留檔名。"""
    forever_growing = [(float(t), {"M001.zip": 100 * (t + 1)}) for t in range(0, 100)]
    result, _, _ = run(forever_growing, signal_at=2.0, timeout=10)
    assert result.status == "timeout"
    assert result.signal_seen is True
    assert result.files == ("M001.zip",)


def test_signal_arriving_at_the_deadline_still_times_out(monkeypatch):
    """
    訊號剛好在最後一刻到，剩餘預算是 0——沒有時間確認穩定就不能算完成。
    這是預算「一段」的邊界：第二階段不能因為預算歸零就偷拿完整的 timeout，
    也不該拿著 0 或負的預算去呼叫 stability——那是把邊界情況丟給別人處理。
    """
    result, clock, spy = run(
        [(0.0, {"M001.zip": 4096})],
        signal_at=30.0,
        timeout=30,
        monkeypatch=monkeypatch,
    )
    assert result.status == "timeout"
    assert result.signal_seen is True
    assert clock.now <= 30.5
    assert all(k["timeout_sec"] > 0 for k in spy.kwargs), (
        f"拿著非正數的預算呼叫了 stability：{[k['timeout_sec'] for k in spy.kwargs]}"
    )


def test_timeout_reasons_tell_no_signal_apart_from_short_count():
    """
    TD-4 的承諾：「訊號沒來」與「訊號來了但檔案沒齊」要分開記錄。
    signal_seen 是機器讀的，reason 是人讀的，兩者都要分得出來。
    """
    no_signal, _, _ = run([(0.0, {"M001.dxf": 1})], signal_at=None, timeout=5)
    short, _, _ = run(
        [(0.0, {"M001.dxf": 512000})],
        signal_at=0.0,
        expected_count=2,
        timeout=5,
    )
    assert no_signal.reason and short.reason
    assert no_signal.reason != short.reason


# ── 中止：守衛喊停時任何階段都要立刻放手 ──────────────────────────────
#
# TD-5：偵測到白名單外的對話框，MUST NOT 送出任何輸入並中止當前任務。
# 中止與逾時一樣不回報檔案——兩者都代表這次沒有可信的產出。


def test_abort_during_signal_wait_stops_immediately():
    result, clock, _ = run(
        [(0.0, {"M001.zip": 4096})],
        signal_at=None,
        abort_fn=lambda: True,
        timeout=300,
    )
    assert result.status == "aborted"
    assert result.signal_seen is False
    assert result.files == ()
    assert clock.now < 1.0, "中止後還在輪詢訊號"


def test_abort_during_stability_wait_stops_immediately():
    """訊號已到、正在等穩定時守衛喊停，一樣要立刻放手。"""
    forever_growing = [(float(t), {"M001.zip": 100 * (t + 1)}) for t in range(0, 100)]
    clock = FakeClock()
    timeline = Timeline(clock, forever_growing)
    result = cp.wait_for_completion(
        signal_fn=SignalAt(clock, 0.0),
        sample_fn=timeline.sample,
        sleep_fn=clock.sleep,
        clock_fn=clock.time,
        abort_fn=abort_from(clock, 2.0),
        expected_count=None,
        poll_interval_ms=500,
        stable_samples=3,
        quiet_period_sec=0.0,
        timeout_sec=300,
    )
    assert result.status == "aborted"
    assert result.signal_seen is True
    assert result.files == ()
    assert clock.now < 3.0, "中止後還在等穩定"


def test_abort_during_recount_wait_stops_immediately():
    """第三階段（穩定了但檔案沒齊、回頭再等）也要吃得到中止。"""
    clock = FakeClock()
    timeline = Timeline(clock, [(0.0, {"M001.dxf": 512000})])
    result = cp.wait_for_completion(
        signal_fn=SignalAt(clock, 0.0),
        sample_fn=timeline.sample,
        sleep_fn=clock.sleep,
        clock_fn=clock.time,
        abort_fn=abort_from(clock, 4.0),
        expected_count=2,
        poll_interval_ms=500,
        stable_samples=3,
        quiet_period_sec=0.0,
        timeout_sec=300,
    )
    assert result.status == "aborted"
    assert result.files == ()
    assert clock.now < 5.0


def test_abort_wins_over_a_simultaneous_signal():
    """守衛與完成對話框同時出現時，守衛優先——未知對話框可能就是在匯出瞬間彈的。"""
    result, _, _ = run(
        [(0.0, {"M001.zip": 4096})],
        signal_at=0.0,
        abort_fn=lambda: True,
    )
    assert result.status == "aborted"
    assert result.files == ()


def test_no_abort_fn_means_never_abort():
    result, _, _ = run([(0.0, {"M001.zip": 4096})], signal_at=0.0, abort_fn=None)
    assert result.status == "complete"


# ── 產出格式 ──────────────────────────────────────────────────────────


def test_multiple_outputs_are_all_reported():
    """Spec「一次匯出產生多個檔案」：AAMA 的 .dxf 與 .rul 都要納入歸檔，MUST NOT 只取其中一個。"""
    result, _, _ = run(
        [(0.0, {"M001.dxf": 512000, "M001.rul": 800})],
        signal_at=1.0,
    )
    assert result.status == "complete"
    assert set(result.files) == {"M001.dxf", "M001.rul"}


def test_files_are_sorted_for_deterministic_output():
    """順序固定，日誌與測試才不會隨機跳動。"""
    result, _, _ = run([(0.0, {"z": 1, "a": 2, "m": 3})], signal_at=0.0)
    assert result.files == ("a", "m", "z")


def test_large_model_taking_long_is_not_a_failure():
    """
    Spec「大型 model 匯出耗時較久」：訊號第 45 秒才到，檔案一路長到那時。
    預設逾時 300 秒內耐心等，不誤判失敗。
    """
    growing = [(float(t), {"M001.zip": 1000 * (t + 1)}) for t in range(0, 46)]
    result, clock, _ = run(growing, signal_at=45.0, timeout=300)
    assert result.status == "complete"
    assert clock.now > 45.0


def test_elapsed_covers_both_phases():
    """elapsed_sec 是從開始等訊號起算的總時間，不是只有穩定那一段。"""
    result, clock, _ = run([(0.0, {"M001.zip": 4096})], signal_at=10.0)
    assert result.elapsed_sec == pytest.approx(clock.now)
    assert result.elapsed_sec >= 10.0


def test_result_is_immutable():
    """結果會被寫進日誌與續跑狀態，途中不能被誰順手改掉。"""
    result, _, _ = run([(0.0, {"M001.zip": 4096})], signal_at=0.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "timeout"


def test_complete_reason_is_human_readable():
    result, _, _ = run([(0.0, {"M001.zip": 4096})], signal_at=0.0)
    assert isinstance(result.reason, str) and result.reason.strip()
