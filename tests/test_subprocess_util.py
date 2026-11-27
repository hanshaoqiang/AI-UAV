"""test_subprocess_util.py — 外部工具超时兜底（进程树 kill）单元测试。

验证 run_with_timeout 的两个核心修复：
1. 超时后杀整个进程树（含孙进程），不残留孤儿进程；
2. 文件重定向（非管道）使超时后能及时返回，不阻塞在孙进程继承的管道句柄上。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.core.subprocess_util import (
    ExternalToolTimeout,
    kill_process_tree,
    run_with_timeout,
)


def _pid_exists(pid: int) -> bool:
    """跨平台判断进程是否存活。"""
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        # 进程不存在或已退出
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_external_tool_timeout_is_runtime_error():
    """ExternalToolTimeout 应是 RuntimeError 子类，便于上层捕获与分类。"""
    assert issubclass(ExternalToolTimeout, RuntimeError)


def test_run_with_timeout_normal_return():
    """正常完成：返回 (rc, stdout, stderr)。"""
    rc, out, err = run_with_timeout(
        [sys.executable, "-c", "print('hello-stdout'); import sys; sys.stderr.write('hello-stderr')"],
        cwd=".", timeout_s=20)
    assert rc == 0
    assert "hello-stdout" in out
    assert "hello-stderr" in err


def test_run_with_timeout_stdin_file(tmp_path: Path):
    """stdin 文件重定向（XFOIL 场景：Fortran 运行时经管道读入会丢行）。"""
    script = "import sys; print('got:' + sys.stdin.read().strip())"
    stdin_file = tmp_path / "commands.txt"
    stdin_file.write_text("hello-stdin", encoding="ascii")
    with open(stdin_file, "r", encoding="ascii") as fh:
        rc, out, err = run_with_timeout(
            [sys.executable, "-c", script], cwd=str(tmp_path),
            timeout_s=20, stdin_file=fh)
    assert rc == 0
    assert "got:hello-stdin" in out


def test_run_with_timeout_kills_process_tree(tmp_path: Path):
    """超时后杀整个进程树：父进程 + 孙进程都不残留，且及时返回。

    模拟 VSPAero 事故：父进程(=vspscript) 启动孙进程(=vspaero) 后自己挂起；
    超时后两者都应被终止，且 run_with_timeout 不阻塞在管道句柄上。
    """
    gc_pid_file = tmp_path / "grandchild.pid"
    # 父进程：启动孙进程(休眠 120s)，把孙 pid 写入文件，然后自己休眠 120s
    parent_script = (
        "import subprocess, sys, time\n"
        "gc = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(120)'])\n"
        f"open({str(gc_pid_file)!r}, 'w').write(str(gc.pid))\n"
        "print('parent-started', flush=True)\n"
        "time.sleep(120)\n"
    )

    t0 = time.time()
    with pytest.raises(ExternalToolTimeout):
        run_with_timeout(
            [sys.executable, "-c", parent_script],
            cwd=str(tmp_path), timeout_s=3)
    elapsed = time.time() - t0

    # 核心验证 1：及时返回（旧实现会无限阻塞在孙进程继承的管道句柄上）
    assert elapsed < 25, f"超时后未及时返回，耗时 {elapsed:.1f}s（疑似管道句柄阻塞）"

    # 核心验证 2：孙进程被进程树 kill（不残留孤儿）
    assert gc_pid_file.exists(), "孙进程未启动，测试无效"
    gc_pid = int(gc_pid_file.read_text())
    # 给 taskkill 一点时间生效
    for _ in range(20):
        if not _pid_exists(gc_pid):
            break
        time.sleep(0.25)
    assert not _pid_exists(gc_pid), f"孙进程 {gc_pid} 残留未杀（孤儿进程）"


def test_run_with_timeout_nonzero_exit():
    """非零退出码应正常返回（不抛异常），供上层检查输出。"""
    rc, out, err = run_with_timeout(
        [sys.executable, "-c", "import sys; print('partial-output'); sys.exit(3)"],
        cwd=".", timeout_s=20)
    assert rc == 3
    assert "partial-output" in out


def test_kill_process_tree_nonexistent_pid():
    """kill_process_tree 对不存在的 pid 应静默（不抛异常）。"""
    kill_process_tree(99999999)  # 不应抛异常
