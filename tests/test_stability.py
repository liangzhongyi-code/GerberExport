"""
C1 完成偵測測試（對應 spec: batch-export「完成偵測不依賴固定等待」/ TD-4）。

這是全專案最容易造成**靜默資料損毀**的地方：判定太早就會搬走一個寫到
一半的檔案，而且當下不會有任何錯誤訊息——使用者要等到工廠打不開檔案
才會發現。

取樣、睡眠、時鐘三者全部以參數注入，測試餵入人工序列，不依賴真實計時。
因此「大型 model 匯出 45 秒」這種情境可以在幾毫秒內驗完。
"""

import pytest

from lib import stability as st


def snap(**files):
    """一次取樣：檔名 -> 位元組大小。"""
    return dict(files)


# ── is_stable：純判定 ────────────────────────────────────────────────


def test_not_stable_before_enough_samples():
    assert st.is_stable([snap(a=100), snap(a=100)], required=3) is False


def test_stable_when_last_n_identical():
    samples = [snap(a=50), snap(a=100), snap(a=100), snap(a=100)]
    assert st.is_stable(samples, required=3) is True


def test_still_growing_is_not_stable():
    samples = [snap(a=100), snap(a=200), snap(a=300)]
    assert st.is_stable(samples, required=3) is False


def test_only_the_last_n_matter():
    """前面怎麼跳動都無所謂，重點是最後連續 N 次。"""
    samples = [snap(a=1), snap(a=999), snap(a=7), snap(a=7), snap(a=7)]
    assert st.is_stable(samples, required=3) is True


def test_window_covers_all_required_samples():
    """
    最後兩次相同、但倒數第三次不同時，required=3 就不該算穩定。

    這條專門盯住「視窗取得比 required 短」這種寫錯——寫成 samples[-2:]
    的話，設定 stable_samples=3 會被悄悄降級成 2，穩定判定變寬鬆，
    而所有其他測試都還是綠的。
    """
    samples = [snap(a=100), snap(a=200), snap(a=200)]
    assert st.is_stable(samples, required=3) is False


def test_longer_window_is_stricter():
    samples = [snap(a=1), snap(a=5), snap(a=5), snap(a=5)]
    assert st.is_stable(samples, required=3) is True
    assert st.is_stable(samples, required=4) is False


def test_empty_snapshot_is_never_stable():
    """暫存夾一直是空的代表匯出還沒開始，不是「穩定地沒有檔案」。"""
    assert st.is_stable([snap(), snap(), snap()], required=3) is False


def test_zero_byte_file_is_never_stable():
    """
    AccuMark 可能先建立零位元組佔位檔再寫入。
    把它當成「穩定」會讓腳本搬走一個空檔，而且完全不會報錯。
    """
    assert st.is_stable([snap(a=0), snap(a=0), snap(a=0)], required=3) is False


def test_zero_byte_among_others_blocks_stability():
    """多檔案時只要有一個是 0 就不算完成——附帶檔可能還沒開始寫。"""
    samples = [snap(a=100, b=0)] * 3
    assert st.is_stable(samples, required=3) is False


def test_all_files_must_be_stable():
    """a 不動但 b 還在長，整體就不算完成。"""
    samples = [snap(a=100, b=10), snap(a=100, b=20), snap(a=100, b=30)]
    assert st.is_stable(samples, required=3) is False


def test_multiple_files_all_stable():
    samples = [snap(a=100, b=50)] * 3
    assert st.is_stable(samples, required=3) is True


def test_new_file_appearing_resets_stability():
    """中途冒出附帶檔，代表這次匯出還沒結束。"""
    samples = [snap(a=100), snap(a=100), snap(a=100, b=20)]
    assert st.is_stable(samples, required=3) is False


def test_file_disappearing_is_not_stable():
    samples = [snap(a=100, b=50), snap(a=100, b=50), snap(a=100)]
    assert st.is_stable(samples, required=3) is False


@pytest.mark.parametrize("required", [0, 1])
def test_required_below_two_is_rejected(required):
    """
    只取樣一次等於完全沒有穩定判定。這個下限在設定層已經擋過一次，
    這裡再擋一次，避免有人直接呼叫函式繞過去。
    """
    with pytest.raises(ValueError):
        st.is_stable([snap(a=1)] * 3, required=required)


# ── wait_for_stable：注入式等待迴圈 ──────────────────────────────────


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


def run(samples, *, stable_samples=3, poll_ms=500, timeout=300):
    """把一串預先排好的取樣結果餵進等待迴圈。"""
    clock = FakeClock()
    seq = list(samples)

    def sample_fn():
        return seq.pop(0) if seq else (samples[-1] if samples else {})

    result = st.wait_for_stable(
        sample_fn=sample_fn,
        sleep_fn=clock.sleep,
        clock_fn=clock.time,
        poll_interval_ms=poll_ms,
        stable_samples=stable_samples,
        timeout_sec=timeout,
    )
    return result, clock


def test_detects_completion():
    result, _ = run([snap(), snap(a=100), snap(a=300), snap(a=300), snap(a=300)])
    assert result.stable is True
    assert result.files == ("a",)


def test_large_model_taking_long_is_not_a_failure():
    """
    裁片多的 model 匯出 45 秒。固定等待會誤判失敗，
    輪詢則是耐心等到大小穩定為止。
    """
    growing = [snap(a=i * 1000) for i in range(1, 90)]  # 約 44 秒的成長
    result, clock = run(growing + [snap(a=90000)] * 3, timeout=300)
    assert result.stable is True
    assert clock.now > 40, "應該真的等了 40 秒以上"


def test_timeout_when_nothing_appears():
    result, clock = run([snap()] * 10, timeout=2)
    assert result.stable is False
    assert result.reason == "timeout"
    assert clock.now >= 2


def test_timeout_while_still_growing():
    """一直在長也可能是 AccuMark 卡住了，逾時就是逾時。"""
    forever_growing = [snap(a=i) for i in range(1, 200)]
    result, _ = run(forever_growing, timeout=3)
    assert result.stable is False


def test_no_sleep_longer_than_poll_interval():
    _, clock = run([snap(a=1)] * 6, poll_ms=250)
    assert all(s == pytest.approx(0.25) for s in clock.sleeps)


def test_multiple_outputs_are_all_reported():
    """AAMA 匯出可能同時吐出 .dxf 與規則檔，兩個都要納入歸檔。"""
    result, _ = run([snap(a=10, b=20)] * 4)
    assert result.stable is True
    assert set(result.files) == {"a", "b"}


def test_files_are_sorted_for_deterministic_output():
    """順序固定，日誌與測試才不會隨機跳動。"""
    result, _ = run([snap(z=1, a=2, m=3)] * 4)
    assert result.files == ("a", "m", "z")


def test_result_reports_elapsed_time():
    result, _ = run([snap(a=1)] * 5, poll_ms=500)
    assert result.elapsed_sec > 0


def test_timeout_result_has_no_files():
    result, _ = run([snap()] * 10, timeout=1)
    assert result.files == ()


def test_timeout_reports_no_files_even_when_some_exist():
    """
    逾時代表這次匯出沒有可信的產出，即使暫存夾裡看得到東西。

    回報半成品會讓歸檔層把一個寫到一半的檔案搬進輸出資料夾，
    而且整個過程不會有任何錯誤訊息——正是 TD-4 要防的靜默損毀。
    """
    forever_growing = [snap(a=i * 100) for i in range(1, 200)]
    result, _ = run(forever_growing, timeout=3)
    assert result.stable is False
    assert result.reason == "timeout"
    assert result.files == (), "逾時卻回報了檔案，歸檔層會搬走半成品"


def test_samples_are_taken_at_least_once_before_sleeping():
    """匯出瞬間完成時不該白等一輪。"""
    calls = []
    clock = FakeClock()

    def sample_fn():
        calls.append(clock.now)
        return snap(a=100)

    st.wait_for_stable(
        sample_fn=sample_fn,
        sleep_fn=clock.sleep,
        clock_fn=clock.time,
        poll_interval_ms=500,
        stable_samples=2,
        timeout_sec=30,
    )
    assert calls[0] == 0.0
