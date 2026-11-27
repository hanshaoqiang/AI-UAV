"""M02: Aircraft 装配与几何校验。"""
from __future__ import annotations

import logging
from dataclasses import asdict

from src.core.contracts import Aircraft, MissionSpec
from src.geometry.sizing import size_aircraft

logger = logging.getLogger("uavdp.geometry")


def build_aircraft(mission: MissionSpec, design: dict, sizing: dict) -> Aircraft:
    """入口：任务需求 → Aircraft 标准几何对象。"""
    ac = size_aircraft(mission, design, sizing)
    validate(ac)
    return ac


def validate(ac: Aircraft) -> None:
    """几何合理性校验（硬约束，违例抛错）。"""
    w, h, v, f = ac.wing, ac.htail, ac.vtail, ac.fuselage
    checks = {
        "翼展>0": w.span_m > 0.5,
        "展弦比合理(4~20)": 4.0 <= w.aspect_ratio <= 20.0,
        "锥比合理(0.2~1)": 0.2 <= w.taper_ratio <= 1.0,
        "平尾面积比合理(0.10~0.35)": 0.10 <= h.area_m2 / w.area_m2 <= 0.35,
        "垂尾面积比合理(0.03~0.35)": 0.03 <= v.area_m2 / w.area_m2 <= 0.35,
        "尾容量Vh合理(0.3~0.8)": 0.3 <= ac.htail_volume_coeff <= 0.8,
        "尾容量Vv合理(0.02~0.08)": 0.02 <= ac.vtail_volume_coeff <= 0.08,
        "尾臂为正": h.arm_m > 0.2 and v.arm_m > 0.2,
        "机身长径比(4~12)": 4.0 <= f.length_m / max(f.max_diameter_m, 1e-6) <= 12.0,
        "翼载为正": ac.wing_loading_kg_m2 > 3.0,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise ValueError(f"几何校验未通过: {failed}")
    logger.debug("几何校验通过: %d 项", len(checks))


def aircraft_summary(ac: Aircraft) -> dict:
    """报告用摘要（扁平化）。"""
    d = asdict(ac)
    d.update({
        "wing_loading_kgm2": round(ac.wing_loading_kg_m2, 2),
        "htail_volume_coeff": round(ac.htail_volume_coeff, 3),
        "vtail_volume_coeff": round(ac.vtail_volume_coeff, 3),
        "wing_loading_Wm2": round(ac.power_loading_w_kg, 1),
    })
    return d
