"""M02: 初步尺寸估算 — 重量迭代主循环。

Mission → Aircraft（Geometry 对象）。

迭代逻辑（电推进概念设计经典链路）:
  MTOW 猜测 → 翼载(巡航CL/失速约束) → 机翼 → 机身/尾翼 → 巡航阻力(寄生+诱导)
  → 功率需求 → 电池质量 + 电机质量 + 结构质量 → 新 MTOW → 松弛迭代至收敛

Phase 2.5（阻力/功率估算对齐）:
  寄生阻力复用 ParasiteDragModel（组件叠加法，与 PerformanceEvaluator 一致），
  诱导阻力用可配置 Oswald 效率，消除定尺寸阶段 cd0=0.035 硬编码导致的功率低估。
"""
from __future__ import annotations

import logging
import math

from src.core.contracts import Aircraft, MissionSpec
from src.geometry.propeller import size_propulsion, size_fuselage
from src.geometry.tail import size_htail, size_vtail
from src.geometry.wing import size_wing, wing_loading_from_cruise, wing_loading_from_stall
from src.performance.parasite_drag import ParasiteDragModel

logger = logging.getLogger("uavdp.geometry")

G = 9.81


def air_density(altitude_m: float) -> float:
    return 1.225 * (1.0 - 2.25577e-5 * altitude_m) ** 4.2559


def _cruise_drag_power(mtow: float, wing, htail, vtail, fus, rho: float, v: float,
                       sizing: dict, parasite: ParasiteDragModel, oswald: float) -> dict:
    """巡航阻力与功率估算（与 PerformanceEvaluator 的寄生阻力模型一致）。

    返回 {cl_cr, cd0, cd, drag_n, p_cruise_w, ld}。
    """
    ac_min = Aircraft(wing=wing, htail=htail, vtail=vtail, fuselage=fus)
    re_cr = rho * v * wing.mean_chord_m / 1.7894e-5
    mach = v / 340.3
    cd0 = parasite.evaluate(ac_min, re_cr, mach, wing.area_m2).cd0
    cl_cr = mtow * G / (0.5 * rho * v ** 2 * wing.area_m2)
    cd = cd0 + cl_cr ** 2 / (math.pi * wing.aspect_ratio * oswald)
    drag = 0.5 * rho * v ** 2 * wing.area_m2 * cd
    p_cruise_w = drag * v / sizing["prop_efficiency"]
    ld = cl_cr / cd if cd > 0 else 0.0
    return {"cl_cr": cl_cr, "cd0": cd0, "cd": cd, "drag_n": drag,
            "p_cruise_w": p_cruise_w, "ld": ld}


def size_aircraft(mission: MissionSpec, design: dict, sizing: dict) -> Aircraft:
    """初步尺寸估算主入口。"""
    rho = air_density(mission.altitude_m)
    v = mission.cruise_speed_ms
    ar = float(design["aspect_ratio"])
    taper = float(design["taper_ratio"])

    # Phase 2 新增设计变量（带默认值，向后兼容；根/梢弦为派生量不在此列）
    wing_sweep = float(design.get("wing_sweep_deg", 2.0))
    wing_dihedral = float(design.get("wing_dihedral_deg", 3.0))
    wing_twist = float(design.get("wing_twist_deg", -2.0))
    wing_x_frac = float(design.get("wing_x_fraction", 0.30))
    h_taper = float(design.get("htail_taper_ratio", 0.6))
    h_sweep = float(design.get("htail_sweep_deg", 5.0))
    v_taper = float(design.get("vtail_taper_ratio", 0.6))
    v_sweep = float(design.get("vtail_sweep_deg", 12.0))
    prop_diameter = design.get("prop_diameter_m")   # None → 功率反推（绝对覆盖，向后兼容）
    prop_diameter_factor = design.get("prop_diameter_factor")  # 直径缩放系数（Phase 2.5 采样变量）
    prop_rpm = float(design.get("prop_rpm", 6000.0))

    # Phase 2.5：Oswald 效率可配（诱导阻力）；寄生阻力复用 ParasiteDragModel
    oswald = float(sizing.get("oswald_efficiency", 0.85))
    parasite = ParasiteDragModel(sizing.get("parasite_drag"))
    cl_max_3d = sizing["cl_max_2d"] * sizing["cl_max_3d_factor"]

    # ---- 初始猜测 ----
    if mission.mtow_kg:
        mtow = float(mission.mtow_kg)
    else:
        mtow = max(mission.payload_kg / 0.25, 4.0)

    relaxation = sizing["relaxation"]
    max_iter = sizing["max_iterations"]
    tol = sizing["tolerance"]

    # 重量迭代（机身/尾翼也移入环内，使寄生阻力估算自洽；用上一轮 m_batt/m_motor 估机身）
    m_batt_est = 0.0
    m_motor_est = 0.0
    for it in range(max_iter):
        # 1) 翼载: 巡航 CL 目标 与 失速约束 取小
        ws = wing_loading_from_cruise(rho, v, sizing["cl_cruise_target"])
        if mission.stall_speed_max_ms:
            ws_stall = wing_loading_from_stall(
                1.225, mission.stall_speed_max_ms, cl_max_3d)  # 失速按海平面密度保守
            ws = min(ws, ws_stall)

        # 2) 机翼
        wing = size_wing(mtow, ws, ar, taper,
                         design["airfoil_root"], design["airfoil_tip"],
                         wing_sweep, wing_dihedral, wing_twist)

        # 3) 机身/尾翼（用上一轮 m_batt/m_motor 估算机身；首轮为 0 仅载荷定机身）
        fus = size_fuselage(mission.payload_kg, m_batt_est, m_motor_est,
                            wing.span_m, wing.mean_chord_m)
        wing_x = wing_x_frac * fus.length_m
        cg_x = wing_x + sizing.get("cg_mac_fraction", 0.25) * wing.mean_chord_m
        tail_x = 0.88 * fus.length_m
        h_arm = tail_x - cg_x
        htail = size_htail(wing, design["htail_volume_coeff"], h_arm,
                           design["htail_aspect_ratio"],
                           taper_ratio=h_taper, sweep_quarter_chord_deg=h_sweep)
        vtail = size_vtail(wing, design["vtail_volume_coeff"], h_arm,
                           design["vtail_aspect_ratio"],
                           taper_ratio=v_taper, sweep_quarter_chord_deg=v_sweep)

        # 4) 巡航阻力 → 功率（寄生阻力与完整分析一致）
        est = _cruise_drag_power(mtow, wing, htail, vtail, fus, rho, v,
                                 sizing, parasite, oswald)
        p_cruise_w = est["p_cruise_w"]

        # 5) 电池/电机/结构重量
        e_req_wh = (p_cruise_w * mission.endurance_h
                    / (sizing["motor_efficiency"] * sizing["esc_efficiency"]
                       * sizing["battery_usable_fraction"]))
        m_batt = e_req_wh / sizing["battery_specific_energy_wh_kg"]
        p_max_total_w = p_cruise_w * sizing["power_margin_factor"]
        m_motor = p_max_total_w / 1000.0 / sizing["motor_specific_power_kw_kg"]
        m_empty = sizing["empty_mass_fraction"] * mtow
        mtow_new = mission.payload_kg + m_empty + m_batt + m_motor

        # 发散防护：MTOW 爆炸或为负 → 该任务物理不可行
        mtow_limit = float(sizing.get("mtow_limit_kg", 5000.0))
        if mtow_new > mtow_limit or mtow_new <= 0:
            raise ValueError(
                f"重量迭代发散: MTOW={mtow_new:.0f} kg 超出合理范围"
                f"(载荷{mission.payload_kg}kg/续航{mission.endurance_h}h/"
                f"高度{mission.altitude_m}m 不可行)")

        # 收敛判定
        delta = abs(mtow_new - mtow) / max(mtow, 1e-6)
        mtow = (1 - relaxation) * mtow + relaxation * mtow_new
        m_batt_est, m_motor_est = m_batt, m_motor
        if delta < tol:
            logger.info("重量迭代收敛: 第%d步, MTOW=%.2f kg, W/S=%.2f, "
                        "CL_cr=%.2f, L/D=%.1f, CD0=%.4f, P_cr=%.0f W, m_batt=%.2f kg",
                        it + 1, mtow, ws, est["cl_cr"], est["ld"], est["cd0"],
                        p_cruise_w, m_batt)
            break
    else:
        logger.warning("重量迭代未收敛(最大%d步), 末值 MTOW=%.2f", max_iter, mtow)
        raise ValueError(f"重量迭代未收敛(最大{max_iter}步)")

    # ---- 最终装配（用收敛后的 mtow/m_batt/m_motor 重算，保证自洽） ----
    ws = wing_loading_from_cruise(rho, v, sizing["cl_cruise_target"])
    if mission.stall_speed_max_ms:
        ws = min(ws, wing_loading_from_stall(
            1.225, mission.stall_speed_max_ms, cl_max_3d))
    wing = size_wing(mtow, ws, ar, taper,
                     design["airfoil_root"], design["airfoil_tip"],
                     wing_sweep, wing_dihedral, wing_twist)
    est = _cruise_drag_power(mtow, wing, htail, vtail, fus, rho, v,
                             sizing, parasite, oswald)  # 尾翼/机身用环内末值
    # 用最终翼面积重算（机身/尾翼亦按最终质量重定，保证尾臂/面积一致）
    fus = size_fuselage(mission.payload_kg, m_batt, m_motor,
                        wing.span_m, wing.mean_chord_m)
    wing_x = wing_x_frac * fus.length_m
    cg_x = wing_x + sizing.get("cg_mac_fraction", 0.25) * wing.mean_chord_m
    tail_x = 0.88 * fus.length_m
    h_arm = tail_x - cg_x
    htail = size_htail(wing, design["htail_volume_coeff"], h_arm,
                       design["htail_aspect_ratio"],
                       taper_ratio=h_taper, sweep_quarter_chord_deg=h_sweep)
    vtail = size_vtail(wing, design["vtail_volume_coeff"], h_arm,
                       design["vtail_aspect_ratio"],
                       taper_ratio=v_taper, sweep_quarter_chord_deg=v_sweep)
    est = _cruise_drag_power(mtow, wing, htail, vtail, fus, rho, v,
                             sizing, parasite, oswald)
    p_cruise_w = est["p_cruise_w"]
    e_req_wh = (p_cruise_w * mission.endurance_h
                / (sizing["motor_efficiency"] * sizing["esc_efficiency"]
                   * sizing["battery_usable_fraction"]))
    m_batt = e_req_wh / sizing["battery_specific_energy_wh_kg"]
    p_max_total_w = p_cruise_w * sizing["power_margin_factor"]
    m_motor = p_max_total_w / 1000.0 / sizing["motor_specific_power_kw_kg"]
    n_props = int(design.get("n_props", 1))

    prop = size_propulsion(p_cruise_w, n_props, sizing["power_margin_factor"],
                           sizing["motor_specific_power_kw_kg"],
                           e_req_wh, sizing["battery_specific_energy_wh_kg"], m_batt,
                           prop_diameter_hint=prop_diameter,
                           prop_diameter_factor=prop_diameter_factor,
                           prop_layout=design.get("prop_layout", "nose"),
                           prop_rpm=prop_rpm,
                           fuselage_length_m=fus.length_m,
                           wing_x_m=wing_x, wing_span_m=wing.span_m,
                           cruise_speed_ms=v,
                           design_alpha_deg=float(sizing.get("blade_design_alpha_deg", 5.0)),
                           rho=rho,
                           n_blades=int(sizing.get("blade_n_blades", 2)),
                           prop_efficiency=float(sizing.get("prop_efficiency", 0.7)),
                           bemt_cfg=sizing)
    v_arm = tail_x - cg_x

    result = Aircraft(
        wing=wing, htail=htail, vtail=vtail, fuselage=fus, propulsion=prop,
        mtow_kg=mtow,
        empty_mass_kg=sizing["empty_mass_fraction"] * mtow,
        payload_kg=mission.payload_kg,
        battery_mass_kg=m_batt,
        wing_x_m=wing_x, htail_x_m=tail_x, vtail_x_m=tail_x,
        htail_z_m=0.35 * fus.length_m / 7.0,          # 平尾约位于机身轴高度
        cg_x_m=cg_x,
    )
    logger.info("布局完成: b=%.2f m, S=%.2f m², AR=%.1f, L_fus=%.2f m, "
                "Sh=%.3f m², Sv=%.3f m², Vh=%.3f, Vv=%.3f, MTOW=%.2f kg",
                wing.span_m, wing.area_m2, wing.aspect_ratio, fus.length_m,
                htail.area_m2, vtail.area_m2,
                result.htail_volume_coeff, result.vtail_volume_coeff, mtow)
    return result
