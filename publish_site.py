"""Safely rebuild and publish only the generated GitHub Pages artifact."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common import PROJECT_DIR


LOCK_PATH = PROJECT_DIR / ".publish.lock"
ALLOWED_ARTIFACT = "index.html"


def _run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=PROJECT_DIR,
        check=True,
        text=True,
        capture_output=capture,
    )


def _git_output(*arguments: str) -> str:
    return _run(["git", *arguments], capture=True).stdout.rstrip("\r\n")


def _require_clean_worktree() -> None:
    status = _git_output("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError(
            "工作目錄不是乾淨狀態，為避免誤提交程式碼，已停止發布：\n"
            f"{status}"
        )


def _validate_pipeline_changes() -> bool:
    lines = _git_output(
        "status", "--porcelain=v1", "--untracked-files=all"
    ).splitlines()
    if not lines:
        return False
    unexpected = [line for line in lines if line != f" M {ALLOWED_ARTIFACT}"]
    if unexpected:
        raise RuntimeError(
            "管線執行後出現 index.html 以外的異動，已停止發布；"
            "請先人工檢查：\n" + "\n".join(unexpected)
        )
    return True


def publish() -> None:
    remote = os.environ.get("PUBLISH_REMOTE", "origin")
    expected_branch = os.environ.get("PUBLISH_BRANCH", "main")
    branch = _git_output("branch", "--show-current")
    if branch != expected_branch:
        raise RuntimeError(
            f"目前分支是 {branch or '(detached HEAD)'}；"
            f"發布只允許從 {expected_branch} 執行。"
        )

    _require_clean_worktree()
    print(f"同步 {remote}/{expected_branch} ...", flush=True)
    _run(["git", "pull", "--rebase", remote, expected_branch])
    _require_clean_worktree()

    unpublished = int(
        _git_output("rev-list", "--count", f"{remote}/{expected_branch}..HEAD")
    )
    if unpublished:
        raise RuntimeError(
            f"目前有 {unpublished} 個尚未推送的 commit。"
            "請先人工確認並推送，再執行發布腳本。"
        )

    print("執行資料管線與靜態站重建 ...", flush=True)
    _run([str(PROJECT_DIR / "run_pipeline.sh")])
    if not _validate_pipeline_changes():
        print("index.html 沒有變化，不需要發布。")
        return

    _run(["git", "add", "--", ALLOWED_ARTIFACT])
    staged = _git_output("diff", "--cached", "--name-only").splitlines()
    if staged != [ALLOWED_ARTIFACT]:
        raise RuntimeError(
            "暫存區不是只有 index.html，已停止 commit：\n" + "\n".join(staged)
        )

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    _run(["git", "commit", "-m", f"publish tracker {stamp}"])
    _run(["git", "push", remote, expected_branch])
    print(f"發布完成：只提交並推送了 {ALLOWED_ARTIFACT}。")


def main() -> int:
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有另一個發布程序正在執行；本次略過。")
            return 0
        try:
            publish()
        except (RuntimeError, subprocess.CalledProcessError, ValueError) as error:
            print(f"發布失敗：{error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
