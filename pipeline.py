"""Run the pipeline under an OS-managed advisory lock."""

from __future__ import annotations

import fcntl
import subprocess
import sys
from datetime import datetime

from common import PROJECT_DIR


PIPELINE_SCRIPTS = ("db_setup.py", "scraper.py", "parser.py", "build_site.py")
LOCK_PATH = PROJECT_DIR / ".pipeline.lock"


def main() -> int:
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有另一個 Serenity Tracker 管線正在執行；本次排程略過。")
            return 0

        print(f"=== 執行時間：{datetime.now().astimezone().isoformat()} ===", flush=True)
        try:
            for script in PIPELINE_SCRIPTS:
                subprocess.run(
                    [sys.executable, str(PROJECT_DIR / script)],
                    cwd=PROJECT_DIR,
                    check=True,
                )
        except subprocess.CalledProcessError as error:
            print(
                f"=== 管線失敗：{error.cmd[-1]} 回傳 {error.returncode} ===",
                file=sys.stderr,
                flush=True,
            )
            return error.returncode or 1

        print(f"=== 執行成功：{datetime.now().astimezone().isoformat()} ===", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
