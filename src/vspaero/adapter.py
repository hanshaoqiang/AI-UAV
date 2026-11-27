"""M05-B: VSPAeroAdapter — vsp3 文件 → 整机气动数据库 (AeroResult)。

Python 主控: 渲染扫描脚本 → vspscript.exe 运行 VSPAEROSweep → 解析 .polar。
气动拟合: CD = CD0 + k·CL², CLα, Cmα(关于重心)。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from src.core.contracts import AeroResult
from src.core.subprocess_util import ExternalToolTimeout, run_with_timeout

logger = logging.getLogger("uavdp.vspaero")

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "external" / "scripts"


class VSPAeroAdapter:
    """输入: vsp3 路径 + 分析配置 + 重心位置; 输出: AeroResult。"""

    def __init__(self, vspscript_exe: str, timeout_s: int = 600):
        self.vspscript_exe = str(vspscript_exe)
        self.timeout_s = timeout_s
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            keep_trailing_newline=True,
        )

    def analyze(self, vsp3_path: Path, run_dir: Path, analysis_cfg: dict,
                cg_x_m: float, wing_aspect_ratio: float) -> AeroResult:
        """运行整机 alpha 扫描并返回拟合后的气动结果。"""
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        vsp3_path = Path(vsp3_path)

        params = {
            "vsp3_path": str(vsp3_path).replace("\\", "/"),
            "alpha_start": analysis_cfg["alpha_start_deg"],
            "alpha_end": analysis_cfg["alpha_end_deg"],
            "alpha_npts": int(analysis_cfg["alpha_npts"]),
            "mach": analysis_cfg["mach"],
            "ncpu": int(analysis_cfg.get("ncpu", 4)),
            "wake_iter": int(analysis_cfg.get("wake_iterations", 3)),
            "xcg": cg_x_m,
        }
        template = self._env.get_template("aero_sweep.vspscript.j2")
        script_path = run_dir / "aero_sweep.vspscript"
        script_path.write_text(template.render(**params), encoding="utf-8")

        logger.info("VSPAero 扫描: alpha %.1f→%.1f° (%d 点)",
                    params["alpha_start"], params["alpha_end"], params["alpha_npts"])
        self._run(script_path, run_dir)

        polar_file = vsp3_path.with_suffix(".polar")
        if not polar_file.exists():
            raise RuntimeError(f"VSPAero 极线文件未生成: {polar_file}")
        result = parse_polar(polar_file, cg_x_m, wing_aspect_ratio)
        logger.info("VSPAero 完成: CLα=%.2f/rad, CD0=%.4f, L/D_max=%.1f @ α=%.1f°",
                    result.CL_alpha_per_rad, result.CD0, result.LD_max,
                    result.alpha_LDmax_deg)
        return result

    def _run(self, script_path: Path, cwd: Path) -> None:
        cmd = [self.vspscript_exe, "-script", str(script_path)]
        try:
            _, out, err = run_with_timeout(cmd, cwd, self.timeout_s)
        except ExternalToolTimeout as e:
            raise RuntimeError(f"VSPAero 超时(>{self.timeout_s}s)") from e
        log = (out or "") + (err or "")
        (cwd / "aero_sweep.stdout.log").write_text(log, encoding="utf-8")
        errors = [ln for ln in log.splitlines() if "Error Code" in ln]
        if errors:
            logger.warning("VSPAero API 错误 %d 条:\n  %s", len(errors),
                           "\n  ".join(errors[:5]))
        if "UAVDP_SWEEP_DONE" not in log:
            raise RuntimeError("VSPAero 扫描未正常完成(缺 UAVDP_SWEEP_DONE 标记)")


# ---------------- 极线解析与气动拟合 ----------------
# 注意: CMytot 才是关于 Xcg 的俯仰力矩(CMoy 近似为常数, 不可用)
_POLAR_KEYS = ("AoA", "CLtot", "CDtot", "L/D", "CMytot")


def parse_polar(polar_file: Path, cg_x_m: float, ar: float) -> AeroResult:
    """解析 VSPAero .polar 文件并拟合气动参数。"""
    lines = Path(polar_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    # 表头在第2行(索引1), 数据从第3行起
    header = None
    data_rows = []
    for ln in lines[1:]:
        tokens = ln.split()
        if header is None and "AoA" in tokens:
            header = tokens
            continue
        if header and len(tokens) == len(header):
            try:
                data_rows.append([float(t) for t in tokens])
            except ValueError:
                continue
    if header is None or not data_rows:
        raise ValueError(f"无法解析极线文件: {polar_file}")

    idx = {k: header.index(k) for k in _POLAR_KEYS if k in header}
    alpha = np.array([r[idx["AoA"]] for r in data_rows])
    cl = np.array([r[idx["CLtot"]] for r in data_rows])
    cd = np.array([r[idx["CDtot"]] for r in data_rows])
    ld = np.array([r[idx["L/D"]] for r in data_rows])
    cm = np.array([r[idx["CMytot"]] for r in data_rows]) if "CMytot" in idx else np.zeros_like(alpha)

    result = AeroResult(
        alpha_deg=alpha.tolist(),
        CL=cl.tolist(),
        CD=cd.tolist(),
        Cm=cm.tolist(),
        reference_x_m=cg_x_m,
        raw_polar_file=str(polar_file),
    )
    _fit(result, alpha, cl, cd, ld, cm, ar)
    return result


def _fit(res: AeroResult, alpha: np.ndarray, cl: np.ndarray, cd: np.ndarray,
         ld: np.ndarray, cm: np.ndarray, ar: float) -> None:
    """最小二乘拟合: CD = CD0 + k·CL²; 线性区 CLα, Cmα。"""
    # 抛物线阻力拟合
    a_mat = np.vstack([np.ones_like(cl), cl ** 2]).T
    sol, *_ = np.linalg.lstsq(a_mat, cd, rcond=None)
    res.CD0 = float(sol[0])
    res.k_induced = float(sol[1])
    res.CL_alpha_0deg = float(np.interp(0.0, alpha, cl))

    # 线性区(前2/3点, 规避大攻角非线性)
    n_lin = max(3, int(len(alpha) * 2 / 3))
    # 以 CL 单调段为准
    mono = np.where(np.diff(cl) > 0)[0]
    if len(mono) >= 2:
        lo, hi = mono[0], min(mono[-1] + 1, len(alpha))
        sl = np.polyfit(np.deg2rad(alpha[lo:hi]), cl[lo:hi], 1)[0]
        res.CL_alpha_per_rad = float(sl)
        if len(cm) == len(alpha) and np.any(cm != 0):
            res.Cm_alpha_per_rad = float(
                np.polyfit(np.deg2rad(alpha[lo:hi]), cm[lo:hi], 1)[0])
    # L/D 最大
    i_ld = int(np.argmax(ld))
    res.LD_max = float(ld[i_ld])
    res.alpha_LDmax_deg = float(alpha[i_ld])
    # CL_max: 线性外推上限(涡格法不失速), 取扫描末点 CL
    res.CL_max = float(cl[-1])
