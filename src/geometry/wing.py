"""M02: 机翼参数化。由任务需求与设计变量确定机翼几何。"""
from __future__ import annotations

from src.core.contracts import MissionSpec, WingParams


def size_wing(
    mtow_kg: float,
    wing_loading_kgm2: float,
    aspect_ratio: float,
    taper_ratio: float,
    airfoil_root: str,
    airfoil_tip: str,
    sweep_quarter_chord_deg: float = 2.0,
    dihedral_deg: float = 3.0,
    twist_tip_deg: float = -2.0,
) -> WingParams:
    """由翼载与展弦比确定机翼。

    W/S 优先由巡航升力系数目标确定，同时满足失速约束（在 sizing 主循环中已取小）。
    Phase 2：后掠/上反/扭转升级为可配置设计变量（带默认值，保持向后兼容）。
    根弦/梢弦为派生量：由面积 + 展弦比 + 锥比唯一确定（见 WingParams 属性）。
    """
    area = mtow_kg / wing_loading_kgm2
    span = (area * aspect_ratio) ** 0.5
    return WingParams(
        span_m=span,
        area_m2=area,
        aspect_ratio=aspect_ratio,
        taper_ratio=taper_ratio,
        sweep_quarter_chord_deg=sweep_quarter_chord_deg,
        dihedral_deg=dihedral_deg,
        twist_tip_deg=twist_tip_deg,
        airfoil_root=airfoil_root,
        airfoil_tip=airfoil_tip,
    )


def wing_loading_from_cruise(rho: float, v_ms: float, cl_cruise: float) -> float:
    """巡航翼载 [kg/m²]: W/S = 0.5·ρ·V²·CL。"""
    return 0.5 * rho * v_ms ** 2 * cl_cruise / 9.81


def wing_loading_from_stall(rho: float, v_stall: float, cl_max3d: float) -> float:
    """失速约束翼载 [kg/m²]。"""
    return 0.5 * rho * v_stall ** 2 * cl_max3d / 9.81
