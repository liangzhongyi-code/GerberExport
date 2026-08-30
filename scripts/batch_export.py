"""
期二：批次匯出主流程。

【目前為佔位版本】實際實作在階段 D（D2–D4），且需要階段 B 的
探測報告當輸入——在拿到 AccuMark 真實的控制項結構之前，這支
腳本的每一行都只能靠猜，所以刻意不先寫。

先放這支是為了讓 3_ 與 4_ 兩個 .bat 有明確的指向。
"""

import argparse
import sys

from check_env import configure_stdout

STAGE = "D"


def main() -> int:
    configure_stdout()

    parser = argparse.ArgumentParser(description="AccuMark 批次匯出（尚未實作）")
    parser.add_argument("--only", metavar="MODEL", help="只處理指定的一個 model")
    parser.add_argument(
        "--format",
        choices=["ZIP", "AAMA", "ASTM"],
        help="只執行指定的一種格式",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略續跑狀態，全部重跑",
    )
    args = parser.parse_args()

    print("AccuMark 批次匯出")
    print("=" * 44)
    print()
    print("這支腳本還沒實作，目前是佔位版本。")
    print("預定在階段 %s 交付，且需要先有階段 B 的探測報告。" % STAGE)
    print()
    print("流程順序：")
    print("  1. 0_檢查環境.bat      確認 pywinauto 可用      ← 現在在這")
    print("  2. 1_執行探測.bat      dump AccuMark UI 結構")
    print("  3. 2a／2b／2c           三種匯出對話框各探一次")
    print("  4. 回報表_請填寫.txt   操作流程與產出檔案（UI 樹裡沒有）")
    print("  5. 3_執行批次匯出.bat  （本腳本）")
    print()
    print(
        "（本次收到的參數 --only=%s --format=%s --force=%s，之後會用到）"
        % (args.only, args.format, args.force)
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
