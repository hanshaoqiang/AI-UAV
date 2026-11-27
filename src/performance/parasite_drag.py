"""Phase 1.5: ParasiteDragModel — 寄生阻力（零升阻力）估算。

独立接口：输入 Geometry + Reynolds + Mach + 表面积 → 输出 CD0。

CD_parasitic 组成：
  - 机翼摩阻（平板湍流边界层 + 形状因子）
  - 机身摩阻（等效旋成体）
  - 平尾摩阻
  - 垂尾摩阻
  - 设备/外露部件（电机座、起落架、整流罩、天线等，用当量平板面积叠加）

方法：经典组件叠加法（component buildup），
  CD0 = Σ (C_f · FF · Q · S_wet_i) / S_ref

其中：
  C_f   平板湍流摩擦系数（Schoenherr / 简化 Prandtl-Schlichting）
  FF    形状因子（form factor，考虑厚度/压力梯度）
  Q     干扰因子（interference factor）
  S_wet 浸润面积
  S_ref 参考面积（机翼平面面积）

不依赖 VSPAero；由 PerformanceEvaluator 融合到 CD_total。
"""
from __future__ import annotations

import logging
import math

from src.core.contracts import Aircraft

logger = logging.getLogger("uavdp.performance.parasite")

# 空气常数
_MU = 1.7894e-5   # 海平面动力粘度 Pa·s


def skin_friction(reynolds: float, mach: float = 0.0) -> float:
    """平板湍流摩擦系数 C_f。

    采用 Prandtl-Schlichting 光滑平板公式（含可压缩性修正）：
      C_f = 0.455 / (log10(Re)^2.58 · (1 + 0.144·M²)^0.65)
    对极低 Re（层流占主导，如小型无人机）回退到层流 Blasius。
    """
    if reynolds <= 0:
        return 0.0
    if reynolds < 5e5:
        # 层流 Blasius: C_f = 1.328 / sqrt(Re)
        return 1.328 / math.sqrt(reynolds)
    cf = 0.455 / (math.log10(reynolds) ** 2.58)
    if mach > 0:
        cf /= (1.0 + 0.144 * mach ** 2) ** 0.65
    return cf


def _wing_wetted_area(span_m: float, area_m2: float, thickness_ratio: float) -> float:
    """梯形翼浸润面积近似：S_wet ≈ 2·S·(1 + 0.25·t/c)。"""
    return 2.0 * area_m2 * (1.0 + 0.25 * thickness_ratio)


def _tail_wetted_area(area_m2: float, thickness_ratio: float) -> float:
    """尾翼浸润面积近似（两侧）。"""
    return 2.0 * area_m2 * (1.0 + 0.25 * thickness_ratio)


def _fuselage_wetted_area(length_m: float, diameter_m: float) -> float:
    """旋成体机身浸润面积（等效圆柱 + 修正）。S ≈ π·d·L·(0.85)。"""
    return math.pi * diameter_m * length_m * 0.85


class ParasiteDragModel:
    """组件叠加法零升阻力模型。

    输入：
        aircraft:  Aircraft（Geometry）
        reynolds:  雷诺数（参考机翼 MAC）
        mach:      马赫数
        s_ref:     参考面积（默认机翼平面面积）
    输出：
        CD0（相对 s_ref）
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        # 各组件 (form_factor, interference_factor, thickness_ratio 占位)
        # 形状因子经验值
        self.ff_wing = float(self.cfg.get("ff_wing", 1.35))       # 机翼形状因子
        self.ff_tail = float(self.cfg.get("ff_tail", 1.30))       # 尾翼
        self.ff_fuselage = float(self.cfg.get("ff_fuselage", 1.15))  # 机身
        self.q_wing = float(self.cfg.get("q_wing", 1.0))          # 干扰因子
        self.q_tail = float(self.cfg.get("q_tail", 1.03))
        self.q_fuselage = float(self.cfg.get("q_fuselage", 1.0))
        # 设备/外露部件当量平板面积（m²），经验默认值
        self.s_misc = float(self.cfg.get("misc_drag_area_m2", 0.006))

    def evaluate(self, aircraft: Aircraft, reynolds: float, mach: float,
                 s_ref: float | None = None) -> "ParasiteDragResult":
        """返回 ParasiteDragResult（含分项明细）。"""
        s_ref = s_ref if s_ref and s_ref > 0 else aircraft.wing.area_m2
        cf = skin_friction(reynolds, mach)
        w, h, v, f = aircraft.wing, aircraft.htail, aircraft.vtail, aircraft.fuselage

        # 各组件浸润面积
        s_wet_wing = _wing_wetted_area(w.span_m, w.area_m2, self.cfg.get("t_c_wing", 0.12))
        s_wet_htail = _tail_wetted_area(h.area_m2, self.cfg.get("t_c_tail", 0.08))
        s_wet_vtail = _tail_wetted_area(v.area_m2, self.cfg.get("t_c_tail", 0.08))
        s_wet_fuse = _fuselage_wetted_area(f.length_m, f.max_diameter_m)

        # 各组件阻力面积 = C_f · FF · Q · S_wet
        drag_area_wing = cf * self.ff_wing * self.q_wing * s_wet_wing
        drag_area_htail = cf * self.ff_tail * self.q_tail * s_wet_htail
        drag_area_vtail = cf * self.ff_tail * self.q_tail * s_wet_vtail
        drag_area_fuse = cf * self.ff_fuselage * self.q_fuselage * s_wet_fuse
        # 设备/外露部件（起落架、电机座、天线等）用当量平板面积
        drag_area_misc = cf * 1.2 * self.s_misc

        total_drag_area = (drag_area_wing + drag_area_htail + drag_area_vtail
                           + drag_area_fuse + drag_area_misc)
        cd0 = total_drag_area / s_ref

        result = ParasiteDragResult(
            cd0=cd0,
            reynolds=reynolds,
            mach=mach,
            cf=cf,
            components={
                "wing": drag_area_wing / s_ref,
                "htail": drag_area_htail / s_ref,
                "vtail": drag_area_vtail / s_ref,
                "fuselage": drag_area_fuse / s_ref,
                "misc": drag_area_misc / s_ref,
            },
            wetted_areas={
                "wing": s_wet_wing, "htail": s_wet_htail,
                "vtail": s_wet_vtail, "fuselage": s_wet_fuse,
            },
        )
        logger.info("寄生阻力: CD0=%.5f (Re=%.2e M=%.2f) 分解=%s",
                    cd0, reynolds, mach,
                    {k: f"{v:.5f}" for k, v in result.components.items()})
        return result


class ParasiteDragResult:
    """寄生阻力结果（数据契约）。"""

    def __init__(self, cd0: float, reynolds: float, mach: float, cf: float,
                 components: dict, wetted_areas: dict):
        self.cd0 = cd0
        self.reynolds = reynolds
        self.mach = mach
        self.cf = cf
        self.components = components          # 各组件对 CD0 的贡献
        self.wetted_areas = wetted_areas

    def to_dict(self) -> dict:
        return {
            "cd0": self.cd0, "reynolds": self.reynolds, "mach": self.mach,
            "cf": self.cf, "components": self.components,
            "wetted_areas": self.wetted_areas,
        }
