"""M05-A: XFOILAdapter — 翼型 .dat → 2D 极线 (AirfoilPolar)。

批处理驱动: 命令脚本经 stdin 喂给 xfoil.exe, 极线由 PWRIT 落盘后解析。
XFOIL 不收敛时标记 converged=False 并降级(性能模块用经验值)。
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.core.contracts import AirfoilPolar
from src.core.subprocess_util import ExternalToolTimeout, run_with_timeout

logger = logging.getLogger("uavdp.xfoil")


class XFOILAdapter:
    """输入: 翼型 dat 路径 + 雷诺数 + 攻角范围; 输出: AirfoilPolar。"""

    def __init__(self, xfoil_exe: str, timeout_s: int = 120):
        self.xfoil_exe = str(xfoil_exe)
        self.timeout_s = timeout_s

    def analyze(self, airfoil_file: Path, run_dir: Path, reynolds: float,
                alpha_start: float = -4.0, alpha_step: float = 1.0,
                alpha_end: float = 12.0, max_iter: int = 100) -> AirfoilPolar:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        airfoil_file = Path(airfoil_file)
        # 注意: Fortran 文件名长度有限, 极线保存用相对短文件名(cwd=run_dir)
        polar_file = run_dir / f"{airfoil_file.stem}_polar.dat"

        commands = "\n".join([
            f"LOAD {airfoil_file.resolve()}",
            "PANE",
            "OPER",
            f"VISC {reynolds:.0f}",
            f"ITER {int(max_iter)}",
            "PACC",
            polar_file.name,        # 相对路径(短), 避免 Fortran 文件名截断
            "",                     # dump filename: 无
            # 注意: ASEQ 参数序为 (起始角, 终止角, 步长)
            f"ASEQ {alpha_start} {alpha_end} {alpha_step}",
            "QUIT",
        ]) + "\n"

        logger.info("XFOIL: %s Re=%.2e α=[%.1f, %.1f]°",
                    airfoil_file.name, reynolds, alpha_start, alpha_end)
        # 注意: 必须用文件重定向 stdin —— Fortran 运行时经管道读入会丢行
        cmd_file = run_dir / "xfoil_commands.txt"
        cmd_file.write_text(commands, encoding="ascii")
        try:
            with open(cmd_file, "r", encoding="ascii") as fh:
                _, out, err = run_with_timeout(
                    [self.xfoil_exe], run_dir, self.timeout_s, stdin_file=fh)
            out = (out or "") + (err or "")
        except ExternalToolTimeout:
            logger.warning("XFOIL 超时: %s", airfoil_file.name)
            return AirfoilPolar(airfoil_file.stem, reynolds, converged=False)

        (run_dir / "xfoil.log").write_text(out, encoding="utf-8")
        if not polar_file.exists():
            logger.warning("XFOIL 极线未生成: %s", airfoil_file.name)
            return AirfoilPolar(airfoil_file.stem, reynolds, converged=False)

        polar = parse_polar(polar_file, airfoil_file.stem, reynolds)
        if polar.CL:
            logger.info("XFOIL 完成: %s CL_max=%.2f @ α=%.1f° (%d点)",
                        airfoil_file.stem, polar.cl_max, polar.alpha_stall_deg,
                        len(polar.CL))
        else:
            polar.converged = False
        return polar


def parse_polar(polar_file: Path, name: str, reynolds: float) -> AirfoilPolar:
    """解析 XFOIL PWRIT 极线(format 1): 列 = alpha CL CD CDp CM ...。"""
    lines = Path(polar_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    alpha, cl, cd, cm = [], [], [], []
    for ln in lines:
        t = ln.split()
        if len(t) < 5:
            continue
        try:
            a, c_l, c_d, _cdp, c_m = (float(t[0]), float(t[1]), float(t[2]),
                                      float(t[3]), float(t[4]))
        except ValueError:
            continue
        alpha.append(a)
        cl.append(c_l)
        cd.append(c_d)
        cm.append(c_m)

    polar = AirfoilPolar(name, reynolds, alpha, cl, cd, cm)
    if cl:
        i_max = max(range(len(cl)), key=lambda i: cl[i])
        polar.cl_max = cl[i_max]
        polar.alpha_stall_deg = alpha[i_max]
        # XFOIL 失速后 CL 下降的判定
        if i_max < len(cl) - 2:
            polar.alpha_stall_deg = alpha[i_max]
    return polar
