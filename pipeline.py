"""Run the pipeline under an OS-managed advisory lock."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from datetime import datetime

from common import PROJECT_DIR
from storage import (
    DB_PATH,
    connect_db,
    finish_pipeline_run,
    get_pipeline_run,
    init_db,
    mark_abandoned_pipeline_runs,
    start_pipeline_run,
    update_pipeline_stage,
    utc_now,
)


PIPELINE_SCRIPTS = (
    "db_setup.py",
    "scraper.py",
    "parser.py",
    "summarize.py",
    "alias_review.py",
    "build_site.py",
)
STAGE_NAMES = {
    "db_setup.py": "database",
    "scraper.py": "scraper",
    "parser.py": "parser",
    "summarize.py": "summaries",
    "alias_review.py": "aliases",
    "build_site.py": "site",
}
SCRIPT_ARGUMENTS = {"alias_review.py": ("scan",)}
LOCK_PATH = PROJECT_DIR / ".pipeline.lock"


def main() -> int:
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有另一個 Serenity Tracker 管線正在執行；本次排程略過。")
            return 0

        init_db(DB_PATH)
        connection = connect_db(DB_PATH)
        recovered_runs = mark_abandoned_pipeline_runs(connection)
        if recovered_runs:
            print(f"已標記 {recovered_runs} 次未完成的舊管線為中斷。", flush=True)
        run_id = start_pipeline_run(connection)
        stage = "database"
        final_status = "success"
        environment = {**os.environ, "SERENITY_PIPELINE_RUN_ID": str(run_id)}
        print(f"=== 執行時間：{datetime.now().astimezone().isoformat()} ===", flush=True)
        try:
            for script in PIPELINE_SCRIPTS:
                stage = STAGE_NAMES[script]
                update_pipeline_stage(connection, run_id, stage)
                if script == "build_site.py":
                    run = get_pipeline_run(connection, run_id)
                    status = "partial" if run and (
                        run["long_posts_failed"]
                        or run["parse_failed"]
                        or run["summaries_failed"]
                    ) else "success"
                    final_status = status
                    finish_pipeline_run(
                        connection,
                        run_id,
                        status=status,
                        site_generated_at=utc_now(),
                    )
                subprocess.run(
                    [
                        sys.executable,
                        str(PROJECT_DIR / script),
                        *SCRIPT_ARGUMENTS.get(script, ()),
                    ],
                    cwd=PROJECT_DIR,
                    check=True,
                    env=environment,
                )
        except subprocess.CalledProcessError as error:
            run = get_pipeline_run(connection, run_id)
            status = "partial" if run and (
                run["scraped_count"]
                or run["parsed_count"]
                or run["parse_failed"]
                or run["summaries_updated"]
            ) else "failed"
            finish_pipeline_run(
                connection,
                run_id,
                status=status,
                failed_stage=run["failed_stage"] if run and run["failed_stage"] else stage,
                failure_kind=(
                    run["failure_kind"]
                    if run and run["failure_kind"]
                    else "stage_failure"
                ),
                failure_code=run["failure_code"] if run else None,
                error_message=(
                    run["error_message"]
                    if run and run["error_message"]
                    else f"{stage} 回傳 {error.returncode}"
                ),
            )
            print(
                f"=== 管線失敗：{stage} 回傳 {error.returncode} ===",
                file=sys.stderr,
                flush=True,
            )
            return error.returncode or 1
        except Exception as error:
            finish_pipeline_run(
                connection,
                run_id,
                status="failed",
                failed_stage=stage,
                failure_kind="runtime",
                error_message=str(error),
            )
            print(f"=== 管線失敗：{stage}：{error} ===", file=sys.stderr, flush=True)
            return 1
        finally:
            connection.close()

        outcome = "執行完成（部分項目失敗）" if final_status == "partial" else "執行成功"
        print(f"=== {outcome}：{datetime.now().astimezone().isoformat()} ===", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
