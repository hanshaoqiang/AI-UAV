"""M02: 尾翼参数化（平尾/垂尾）。尾容量系数法。"""
from __future__ import annotations

from src.core.contracts import TailParams, WingParams


def size_htail(wing: WingParams, v_h: float, arm_m: float, ar_h: float = 4.0,
               taper_ratio: float = 0.6, sweep_quarter_chord_deg: float = 5.0) -> TailParams:
    """平尾: Vh = Sh·Lh/(S·c̄) → Sh = Vh·S·c̄/Lh。面积取全平（左右合计）。

    Phase 2：锥比/后掠升级为可配置设计变量（带默认值，保持向后兼容）。
    """
    area = v_h * wing.area_m2 * wing.mean_chord_m / arm_m
    return TailParams(
        area_m2=area, aspect_ratio=ar_h, taper_ratio=taper_ratio,
        sweep_quarter_chord_deg=sweep_quarter_chord_deg, arm_m=arm_m, airfoil="naca0008",
    )


def size_vtail(wing: WingParams, v_v: float, arm_m: float, ar_v: float = 1.8,
               taper_ratio: float = 0.6, sweep_quarter_chord_deg: float = 12.0) -> TailParams:
    """垂尾: Vv = Sv·Lv/(S·b) → Sv = Vv·S·b/Lv。单片面积。

    Phase 2：锥比/后掠升级为可配置设计变量（带默认值，保持向后兼容）。
    """
    area = v_v * wing.area_m2 * wing.span_m / arm_m
    return TailParams(
        area_m2=area, aspect_ratio=ar_v, taper_ratio=taper_ratio,
        sweep_quarter_chord_deg=sweep_quarter_chord_deg, arm_m=arm_m, airfoil="naca0008",
    )
