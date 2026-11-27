"""外部工具子进程调用的统一超时兜底（跨平台）。

背景（Phase 2.5 C2 生产事故）：
  VSPAero 卡死后整批 10164 任务死锁 8+ 小时。根因是 subprocess.run 的两个隐蔽缺陷：
  1. subprocess.run(capture_output=True, timeout=) 超时后只 kill 直接子进程
     (vspscript.exe)，不 kill 孙进程 (vspaero.exe)，孙进程成孤儿挂死；
  2. capture_output 用管道捕获 stdout/stderr，孙进程继承管道写端句柄，直接子进程
     被杀后 communicate() 会无限阻塞等待管道 EOF → 超时兜底形同虚设。

方案：
  - stdout/stderr 重定向到临时文件(非管道)，孙进程继承的是文件句柄，不会阻塞读取；
  - 超时后先用 taskkill /T /F(Windows) / killpg(Unix) 杀整个进程树(含孙进程)，
    再读回输出，确保不残留孤儿进程。

接口稳定：纯新增工具模块，不改变既有 adapter 的对外契约。
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger("uavdp.subprocess")

_IS_WINDOWS = os.name == "nt"

# 清理进程树自身的最长等待（防 taskkill/waitt 自身卡死）
_KILL_TIMEOUT_S = 30


class ExternalToolTimeout(RuntimeError):
    """外部工具运行超时（adapter 各自决定抛给上层分类还是降级）。"""


def kill_process_tree(pid: int) -> None:
    """终止整个进程树（含孙进程）。尽力而为，失败仅告警不抛异常。

    Windows 用 taskkill /T /F（按父-子关系遍历）；Unix 用 killpg（依赖
    start_new_session=True 建立的进程组）。
    """
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=_KILL_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("taskkill /T /F /PID %s 失败: %s", pid, e)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception as e:  # noqa: BLE001
            logger.warning("killpg(%s) 失败: %s", pid, e)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _cleanup(paths: tuple[str, ...]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def run_with_timeout(
    cmd: Sequence[str],
    cwd: str | Path,
    timeout_s: float,
    env: Optional[dict] = None,
    stdin_file=None,
) -> tuple[int, str, str]:
    """启动外部工具并等待，超时杀整个进程树，返回 (returncode, stdout, stderr)。

    参数：
      cmd        命令列表（首个元素为可执行文件路径）
      cwd        工作目录
      timeout_s  超时秒数
      env        可选环境变量（覆盖/追加）
      stdin_file 可选：已打开的读模式文件对象，重定向到子进程 stdin。
                 用文件而非管道（XFOIL Fortran 运行时经管道读入会丢行）。

    返回：
      (returncode:int, stdout:str, stderr:str)。超时抛 ExternalToolTimeout。

    实现要点：
      - stdout/stderr 用临时文件重定向（非 PIPE），避免孙进程继承管道写端句柄
        导致读取永久阻塞；
      - 超时后先 kill_process_tree 杀整树，再读回已写入的输出。
    """
    cwd = str(cwd)
    out_fd, out_path = tempfile.mkstemp(suffix=".out", prefix="uavdp_")
    err_fd, err_path = tempfile.mkstemp(suffix=".err", prefix="uavdp_")

    popen_kwargs: dict = dict(cwd=cwd, env=env, stdout=out_fd, stderr=err_fd)
    if not _IS_WINDOWS:
        # 建立独立进程组，供 killpg 杀整树
        popen_kwargs["start_new_session"] = True
    if stdin_file is not None:
        popen_kwargs["stdin"] = stdin_file

    try:
        proc = subprocess.Popen([str(x) for x in cmd], **popen_kwargs)
    except Exception:
        os.close(out_fd)
        os.close(err_fd)
        _cleanup((out_path, err_path))
        raise
    finally:
        # Popen 已把文件描述符复制给子进程，父进程立即关闭自己的句柄
        os.close(out_fd)
        os.close(err_fd)

    timed_out = False
    try:
        returncode = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc.pid)
        try:
            returncode = proc.wait(timeout=_KILL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                returncode = proc.wait(timeout=_KILL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                returncode = -1

    out = _read_text(out_path)
    err = _read_text(err_path)
    _cleanup((out_path, err_path))

    if timed_out:
        tool = os.path.basename(str(cmd[0])) if cmd else "tool"
        raise ExternalToolTimeout(
            f"外部工具 {tool} 超时(>{timeout_s:.0f}s)，已终止其进程树")

    return returncode, out, err
