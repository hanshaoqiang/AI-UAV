"""Phase 2.5: BEMT（叶素动量理论）螺旋桨求解器。

将推进模型从「动量理论 + 经验 CT/CP 系数」升级为经典 Glauert BEMT：
  - 桨叶离散为若干径向截面；
  - 每截面用叶素理论（截面翼型 2D 极线 Cl/Cd）算法向/切向力；
  - 与动量理论联立，迭代求解轴向诱导因子 a 与切向诱导因子 a'；
  - 含 Prandtl 桨尖损失因子 F；
  - 输出推力 / 扭矩 / 轴功率 / 螺旋桨效率。

模型范围与限制（概念设计精度，详见 MODEL_LIMITATIONS）：
  - 截面翼型极线来自 XFOIL 离线预计算查找表（二维插值 Re × α）；
  - 高载荷区（a 很大，如静止推力）用数值钳制而非 Glauert 经验修正；
  - 不含桨毂/桨根涡修正，不含压缩性修正（桨尖马赫 < 0.7 时可忽略）。

设计原则：
  - 纯 Python + numpy，无外部工具调用（毫秒级，可同时用于预筛与完整分析）；
  - 接口与 PropulsionModel 解耦，只依赖 PolarTable + 几何参数。
"""
from __future__ import annotations

import bisect
import json
import logging
import math
from pathlib import Path

import numpy as np

logger = logging.getLogger("uavdp.propulsion.bemt")

RHO0 = 1.225
MU = 1.7894e-5          # 空气动力黏度 kg/(m·s)
RPM2RAD = math.pi / 30.0   # RPM → rad/s


# ============================================================
# 极线查找表（XFOIL 预计算）
# ============================================================
class PolarTable:
    """桨叶截面翼型 2D 极线查找表：Cl(α, Re)、Cd(α, Re)。

    数据为规则网格：alpha_deg 单调递增（均匀步长），reynolds 单调递增。
    查询时对 log10(Re) 与 α 做二维线性插值；越界时外推/钳制。
    """

    def __init__(self, reynolds: list[float], alpha_deg: list[float],
                 cl: list[list[float]], cd: list[list[float]],
                 airfoil_name: str = ""):
        self.airfoil_name = airfoil_name
        self.reynolds = np.asarray(reynolds, dtype=float)
        self.alpha_deg = np.asarray(alpha_deg, dtype=float)
        # CL/CD: [n_re, n_alpha]
        self.cl = np.asarray(cl, dtype=float)
        self.cd = np.asarray(cd, dtype=float)
        if self.cl.shape != (len(self.reynolds), len(self.alpha_deg)):
            raise ValueError("CL 形状必须为 (n_reynolds, n_alpha)")
        if self.cd.shape != self.cl.shape:
            raise ValueError("CD 与 CL 形状不一致")
        if len(self.reynolds) < 2 or len(self.alpha_deg) < 3:
            raise ValueError("极线表至少需要 2 个雷诺数与 3 个攻角点")
        self._log_re = np.log10(self.reynolds)

    @classmethod
    def from_json(cls, path: str | Path) -> "PolarTable":
        p = Path(path)
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(
            reynolds=d["reynolds"], alpha_deg=d["alpha_deg"],
            cl=d["CL"], cd=d["CD"],
            airfoil_name=d.get("airfoil", p.stem),
        )

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({
            "airfoil": self.airfoil_name,
            "reynolds": self.reynolds.tolist(),
            "alpha_deg": self.alpha_deg.tolist(),
            "CL": self.cl.tolist(),
            "CD": self.cd.tolist(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def interp(self, alpha_rad: float, re: float) -> tuple[float, float]:
        """返回 (Cl, Cd)。α 单位弧度；Re 任意（越界钳制）。纯 Python 手写插值。"""
        alpha_deg = alpha_rad * 180.0 / math.pi
        log_re = math.log10(max(re, 1.0))
        log_grid = self._log_re
        # ---- Re 轴：先沿 log10(Re) 线性插值（越界钳制）----
        n_re = len(log_grid)
        if log_re <= log_grid[0]:
            cl_re, cd_re = self.cl[0], self.cd[0]
        elif log_re >= log_grid[-1]:
            cl_re, cd_re = self.cl[-1], self.cd[-1]
        else:
            i = bisect.bisect_right(log_grid, log_re) - 1
            f = (log_re - log_grid[i]) / (log_grid[i + 1] - log_grid[i])
            cl_re = self.cl[i] * (1 - f) + self.cl[i + 1] * f
            cd_re = self.cd[i] * (1 - f) + self.cd[i + 1] * f
        # ---- α 轴：线性插值 + 线性外推 ----
        return _interp_alpha(self.alpha_deg, cl_re, cd_re, alpha_deg)


def _interp_alpha(alpha_grid: np.ndarray, cl: np.ndarray, cd: np.ndarray,
                  alpha: float) -> tuple[float, float]:
    """沿攻角轴线性插值；越界用线性外推（Cd 外推下限钳制 > 0）。"""
    n = len(alpha_grid)
    a0 = float(alpha_grid[0])
    a1 = float(alpha_grid[1])
    if alpha <= a0:
        da = a1 - a0
        slope_cl = (cl[1] - cl[0]) / da
        slope_cd = (cd[1] - cd[0]) / da
        return float(cl[0] + slope_cl * (alpha - a0)), \
            float(max(cd[0] + slope_cd * (alpha - a0), 1e-4))
    if alpha >= float(alpha_grid[-1]):
        da = float(alpha_grid[-1]) - float(alpha_grid[-2])
        slope_cl = (cl[-1] - cl[-2]) / da
        slope_cd = (cd[-1] - cd[-2]) / da
        return float(cl[-1] + slope_cl * (alpha - float(alpha_grid[-1]))), \
            float(max(cd[-1] + slope_cd * (alpha - float(alpha_grid[-1])), 1e-4))
    i = bisect.bisect_right(alpha_grid, alpha) - 1
    i = max(0, min(i, n - 2))
    f = (alpha - float(alpha_grid[i])) / float(alpha_grid[i + 1] - alpha_grid[i])
    cl_v = cl[i] * (1 - f) + cl[i + 1] * f
    cd_v = cd[i] * (1 - f) + cd[i + 1] * f
    return float(cl_v), float(max(cd_v, 1e-4))


# ============================================================
# 标准桨叶几何（弦长线性 taper + 扭转线性，0.75R 桨距角由几何螺距给定）
# ============================================================
class BladeGeometry:
    """由直径/几何螺距/桨叶数 + 经验形状参数构建桨叶径向离散几何。"""

    def __init__(self, diameter_m: float, pitch_m: float, n_blades: int,
                 chord_ratio_75: float = 0.12, chord_taper: float = 0.5,
                 twist_deg: float = -10.0, hub_ratio: float = 0.15,
                 n_sections: int = 16):
        self.diameter_m = float(diameter_m)
        self.R = self.diameter_m / 2.0
        self.n_blades = int(n_blades)
        self.hub_ratio = float(hub_ratio)
        self.n_sections = int(n_sections)
        self.chord_ratio_75 = float(chord_ratio_75)
        self.chord_taper = float(chord_taper)
        self.twist_deg = float(twist_deg)

        # 0.75R 桨距角（几何螺距定义：pitch = 2π·r_75·tan(β_75)）
        beta_75 = math.atan(pitch_m / (0.75 * math.pi * self.diameter_m)) \
            if self.diameter_m > 0 else 0.0

        # 弦长线性分布 c(r) = c_root + (c_tip-c_root)·(r/R - hub)/(1-hub)
        c_75 = chord_ratio_75 * self.diameter_m
        denom = 1.0 + (chord_taper - 1.0) * (0.75 - hub_ratio) / (1.0 - hub_ratio)
        c_root = c_75 / denom if denom > 1e-6 else c_75
        c_tip = chord_taper * c_root
        self.c_root = float(c_root)
        self.c_tip = float(c_tip)

        # 截面径向位置（中点积分：从 hub 到 tip 均分）
        r_edges = np.linspace(hub_ratio * self.R, self.R, n_sections + 1)
        self.r = 0.5 * (r_edges[:-1] + r_edges[1:])       # 截面中心半径
        self.dr = r_edges[1:] - r_edges[:-1]
        xr = self.r / self.R
        self.chord = c_root + (c_tip - c_root) * (xr - hub_ratio) / (1.0 - hub_ratio)
        # 扭转线性：β(r) = β_75 + twist·(r/R - 0.75)，twist<0 → 梢部桨距角更小（洗出）
        self.beta = beta_75 + math.radians(twist_deg) * (xr - 0.75)

    @property
    def solidity_75(self) -> float:
        c_75 = self.chord_ratio_75 * self.diameter_m
        return self.n_blades * c_75 / (2.0 * math.pi * 0.75 * self.R)


# ============================================================
# BEMT 求解器
# ============================================================
class BEMTSolver:
    """经典 Glauert BEMT 求解器。

    用法：
      solver = BEMTSolver(polar_table, cfg)
      thrust, shaft_power, eta = solver.thrust_power(diameter, pitch, rpm, v, rho, n_blades)
    """

    def __init__(self, polar_table: PolarTable, cfg: dict | None = None):
        self.polar = polar_table
        cfg = cfg or {}
        self.max_iter = int(cfg.get("bemt_max_iter", 60))
        self.tol = float(cfg.get("bemt_tolerance", 1e-2))
        self.relax = float(cfg.get("bemt_relaxation", 0.35))
        self.n_sections = int(cfg.get("bemt_n_sections", 12))
        self.hub_ratio = float(cfg.get("bemt_hub_ratio", 0.15))
        self.chord_ratio_75 = float(cfg.get("blade_chord_ratio_75", 0.12))
        self.chord_taper = float(cfg.get("blade_chord_taper", 0.5))
        self.twist_deg = float(cfg.get("blade_twist_deg", -10.0))
        self.a_max = float(cfg.get("bemt_a_max", 0.8))

    def thrust_power(self, diameter_m: float, pitch_m: float, rpm: float,
                     velocity_ms: float, rho: float,
                     n_blades: int = 2) -> tuple[float, float, float]:
        """返回 (推力 N, 轴功率 W, 螺旋桨效率)。

        velocity_ms 应 > 0（巡航工况）；接近 0 时做数值下界防护。
        """
        if diameter_m <= 0 or rpm <= 0 or n_blades < 1:
            return 0.0, 0.0, 0.0
        v = max(velocity_ms, 0.5)          # 数值下界，避免静止发散
        omega = rpm * RPM2RAD
        geo = BladeGeometry(
            diameter_m, pitch_m, n_blades,
            chord_ratio_75=self.chord_ratio_75, chord_taper=self.chord_taper,
            twist_deg=self.twist_deg, hub_ratio=self.hub_ratio,
            n_sections=self.n_sections)

        # 诱导因子初始猜测
        a = np.zeros(self.n_sections)
        ap = np.zeros(self.n_sections)
        thrust = 0.0
        torque = 0.0

        for _ in range(self.max_iter):
            a_new = np.zeros(self.n_sections)
            ap_new = np.zeros(self.n_sections)
            thrust = 0.0
            torque = 0.0
            for i in range(self.n_sections):
                r = geo.r[i]
                c = geo.chord[i]
                beta = geo.beta[i]
                # 局部速度三角形
                v_axial = v * (1.0 + a[i])
                v_tan = omega * r * (1.0 - ap[i])
                w_local = math.hypot(v_axial, v_tan)
                phi = math.atan2(v_axial, v_tan)
                if phi <= 1e-4 or phi >= math.pi / 2 - 1e-4:
                    continue
                alpha = beta - phi
                re_local = rho * w_local * c / MU
                cl, cd = self.polar.interp(alpha, re_local)

                cn = cl * math.cos(phi) - cd * math.sin(phi)
                ct = cl * math.sin(phi) + cd * math.cos(phi)

                sigma = geo.n_blades * c / (2.0 * math.pi * r)
                sin_phi = math.sin(phi)
                cos_phi = math.cos(phi)

                # Prandtl 桨尖损失
                f_arg = geo.n_blades / 2.0 * (geo.R - r) / (r * sin_phi)
                f_arg = max(f_arg, 0.0)
                F = 2.0 / math.pi * math.acos(math.exp(-f_arg))
                F = max(F, 1e-6)

                # 轴向诱导因子（动量理论 a = σ·Cn/(4F sin²φ - σ·Cn)）
                denom_a = 4.0 * F * sin_phi ** 2 - sigma * cn
                if denom_a > 1e-9:
                    a_i = sigma * cn / denom_a
                else:
                    a_i = self.a_max  # 高载荷：交给钳制
                a_i = min(max(a_i, 0.0), self.a_max)

                # 切向诱导因子（a' = σ·Ct/(4F sinφ cosφ + σ·Ct)）
                denom_ap = 4.0 * F * sin_phi * cos_phi + sigma * ct
                ap_i = sigma * ct / denom_ap if denom_ap > 1e-9 else 0.0
                ap_i = min(max(ap_i, -0.5), 0.5)

                a_new[i] = a_i
                ap_new[i] = ap_i

                # 叶素力
                d_t = 0.5 * rho * w_local ** 2 * c * cn * geo.n_blades * geo.dr[i]
                d_q = 0.5 * rho * w_local ** 2 * c * ct * geo.n_blades * r * geo.dr[i]
                thrust += d_t
                torque += d_q

            # 松弛更新 + 收敛判断
            a = self.relax * a_new + (1.0 - self.relax) * a
            ap = self.relax * ap_new + (1.0 - self.relax) * ap
            if np.max(np.abs(a_new - a)) < self.tol and \
               np.max(np.abs(ap_new - ap)) < self.tol:
                break

        # 风车/透平状态（前进比过高、桨叶进入负攻角）：净推力为负。
        # 该状态下动量理论的轴向诱导因子 a<0，Glauert 公式不再适用，
        # 且极线外推到极端负攻角会产生无物理意义的正扭矩。对预筛/巡航用途，
        # 风车状态的桨不产生可用推力，直接返回 0（避免污染可用推力扫描）。
        if thrust <= 0.0:
            return 0.0, 0.0, 0.0

        shaft_power = torque * omega
        eta = thrust * velocity_ms / shaft_power if shaft_power > 0 else 0.0
        eta = float(max(0.0, min(eta, 0.95)))   # 物理限幅（桨效率 < 1）
        return float(max(thrust, 0.0)), float(max(shaft_power, 0.0)), eta


# ============================================================
# 默认查找表（懒加载 + 缓存）
# ============================================================
_DEFAULT_POLAR_PATH = Path(__file__).resolve().parents[2] / "data" / "polars" / "clarky_polar.json"
_polar_cache: dict[str, PolarTable] = {}


def load_polar_table(path: str | Path | None = None) -> PolarTable:
    """加载桨叶极线查找表。path=None 时用默认 clarky 表。"""
    p = Path(path) if path else _DEFAULT_POLAR_PATH
    key = str(p.resolve())
    if key not in _polar_cache:
        _polar_cache[key] = PolarTable.from_json(p)
    return _polar_cache[key]


def default_bemt_solver(cfg: dict | None = None) -> BEMTSolver:
    """构建使用默认 clarky 查找表的 BEMT 求解器。"""
    return BEMTSolver(load_polar_table(), cfg)
