"""
期一：AccuMark UI 控制項探測。

【目前為佔位版本】實際實作在階段 B（B1–B3）。
先放這支是為了讓 1_ 與 2_ 兩個 .bat 有明確的指向——若使用者在
階段 B 交付之前誤點，會看到清楚的說明而不是「找不到檔案」。
"""

import argparse
import sys

from check_env import configure_stdout

STAGE = "B"


def main() -> int:
    configure_stdout()

    parser = argparse.ArgumentParser(
        description="AccuMark UI 控制項探測（尚未實作）"
    )
    parser.add_argument(
        "--mode",
        choices=["window", "dialog"],
        default="window",
        help="window＝探測主視窗；dialog＝探測目前開啟的模態對話框",
    )
    args = parser.parse_args()

    print("AccuMark 批次匯出 — UI 控制項探測")
    print("=" * 44)
    print()
    print("這支腳本還沒實作，目前是佔位版本。")
    print("預定在階段 %s 交付（B1 樹走訪 / B2 定位策略 / B3 報告輸出）。" % STAGE)
    print()
    print("目前可以做的是：雙擊 0_檢查環境.bat 確認 pywinauto 是否可用，")
    print("把畫面結果回報，這一步會決定後續要不要改用備案技術棧。")
    print()
    print("（本次收到的參數 --mode=%s，之後會用到）" % args.mode)
    return 2


if __name__ == "__main__":
    sys.exit(main())
