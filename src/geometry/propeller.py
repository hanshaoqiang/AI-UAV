"""M02: 机身与螺旋桨参数化。"""
from __future__ import annotations

import math

from src.core.contracts import (BatteryParams, FuselageParams, MotorParams,
                                PropellerParams, PropulsionParams)

# 模块级 BEMT 求解器缓存（避免每样本重复构建；极线表内部也有缓存）
# 缓存键取影响桨叶几何/求解结果的参数，避免不同 cfg 混用同一求解器。
_bemt_solver_cache: dict[tuple, object] = {}


def _get_bemt_solver(cfg: dict | None):
    """懒加载并缓存 BEMT 求解器（供桨距匹配巡航工作点使用）。

    按影响结果的 cfg 参数（桨叶几何 + 极线表路径）分键缓存；
    纯收敛参数（迭代/容差/松弛）不计入键（不影响几何匹配结果）。
    """
    cfg = cfg or {}
    key = (
        cfg.get("prop_polar_table"),
        float(cfg.get("blade_chord_ratio_75", 0.12)),
        float(cfg.get("blade_chord_taper", 0.5)),
        float(cfg.get("blade_twist_deg", -10.0)),
        int(cfg.get("bemt_n_sections", 12)),
        float(cfg.get("bemt_hub_ratio", 0.15)),
    )
    if key not in _bemt_solver_cache:
        from src.propulsion.bemt import BEMTSolver, load_polar_table
        _bemt_solver_cache[key] = BEMTSolver(
            load_polar_table(cfg.get("prop_polar_table")), cfg)
    return _bemt_solver_cache[key]


def _rpm_for_thrust(solver, diameter_m, pitch_m, velocity_ms, rho, n_blades,
                    thrust_target_n, design_rpm) -> float:
    """求产生目标推力的转速（≤ 设计转速）。

    Phase 2.5 性能优化：推力随转速单调非减（风车区被 clamp 为 0，之后单调增），
    用二分查找替代线性扫描（36 点 → ~13 次）。目标推力超出设计转速能力时返回设计转速。
    """
    if thrust_target_n <= 0:
        return float(design_rpm)
    lo, hi = 250.0, float(design_rpm)
    t_hi, _, _ = solver.thrust_power(diameter_m, pitch_m, hi, velocity_ms, rho, n_blades)
    if t_hi < thrust_target_n:
        return float(design_rpm)          # 设计转速也达不到 → 返回上限
    for _ in range(12):
        mid = 0.5 * (lo + hi)
        t_mid, _, _ = solver.thrust_power(diameter_m, pitch_m, mid, velocity_ms, rho, n_blades)
        if t_mid >= thrust_target_n:
            hi = mid
        else:
            lo = mid
    return float(hi)


def _match_cruise_pitch(diameter_m: float, design_rpm: float, cruise_speed_ms: float,
                        rho: float, n_blades: int, thrust_cruise_n: float,
                        design_alpha_deg: float, pitch_ratio: float,
                        cfg: dict | None = None) -> float:
    """迭代求解匹配巡航工作点的桨距。

    Phase 2.5 BEMT：桨距应使「巡航工作点」（产生巡航所需推力的转速）的
    0.75R 截面攻角 = design_alpha_deg（默认 5°），而非设计转速——
    否则巡航时节流降转速会使截面进入负攻角、效率崩塌。
    迭代 3 次（用 BEMT 反推巡航转速 → 更新桨距）收敛。

    鲁棒性：对「桨盘严重偏大/偏小」的工况，迭代会发散到不合理的桨距比
    （如 D=0.8 时 pitch/D≈3.0）。此处将桨距比钳制到 [0.35, 2.0]——
    该范围覆盖健康桨（固定桨距优化巡航时 pitch/D≈1.0~1.6），同时
    阻断发散；超出范围的设计由预筛在推力可行性检查中拒绝。
    """
    solver = _get_bemt_solver(cfg)
    n_cr = float(design_rpm)
    pitch = pitch_ratio * diameter_m
    lo, hi = 0.35 * diameter_m, 2.0 * diameter_m
    for _ in range(3):
        v_tan_75 = n_cr * math.pi * 0.75 * diameter_m / 60.0
        phi_75 = math.atan(cruise_speed_ms / v_tan_75) if v_tan_75 > 0 else 0.0
        pitch = math.pi * 0.75 * diameter_m * math.tan(
            phi_75 + math.radians(design_alpha_deg))
        pitch = min(max(pitch, lo), hi)          # 钳制桨距比，阻断发散
        n_cr = _rpm_for_thrust(solver, diameter_m, pitch, cruise_speed_ms, rho,
                               n_blades, thrust_cruise_n, design_rpm)
    return pitch


def size_fuselage(payload_kg: float, battery_kg: float, motor_kg: float,
                  wing_span_m: float, wing_mean_chord_m: float | None = None) -> FuselageParams:
    """机身：容积法（设备+电池+电机密度约 500 kg/m³，充填系数 0.55，长细比 7）。

    Phase 1.5 修复：机身长度设 MAC 下限，保证尾臂充足（尾臂 = 0.58·L - 0.25·c̄ > 0）。
    """
    v_eq = (payload_kg + battery_kg + motor_kg) / 500.0
    # V = 0.55·(π/4)·d²·L, L = 7d → V ≈ 3.02·d³
    d = max((max(v_eq, 1e-6) / 3.02) ** (1.0 / 3.0), 0.10)
    length = 7.0 * d
    # 长度不超过 0.45·翼展（小型无人机常规比例）
    length = min(length, 0.45 * wing_span_m)
    length = max(length, 0.60)
    # Phase 1.5: 保证尾臂充足——机身长度须容纳机翼 MAC 与尾翼
    # 尾臂 Lh = tail_x - cg_x = 0.58·L - 0.25·c̄ ≥ 2·c̄（无人机经验下限）
    # → L ≥ (2·c̄ + 0.25·c̄)/0.58 = 2.25·c̄/0.58
    if wing_mean_chord_m:
        l_min_for_arm = 2.25 * wing_mean_chord_m / 0.58
        length = max(length, l_min_for_arm)
    return FuselageParams(length_m=length, max_diameter_m=d)


def size_propulsion(
    power_cruise_w: float, n_props: int, power_margin: float,
    motor_specific_power_kwkg: float, battery_energy_wh: float,
    battery_specific_energy_whkg: float, battery_mass_kg: float,
    prop_diameter_hint: float | None = None,
    prop_diameter_factor: float | None = None,
    prop_layout: str = "nose",
    prop_rpm: float = 6000.0,
    pitch_ratio: float = 0.6,
    fuselage_length_m: float = 2.0,
    wing_x_m: float = 0.6,
    wing_span_m: float = 3.0,
    cruise_speed_ms: float | None = None,
    design_alpha_deg: float = 5.0,
    rho: float = 1.225,
    n_blades: int = 2,
    prop_efficiency: float = 0.7,
    bemt_cfg: dict | None = None,
) -> PropulsionParams:
    """螺旋桨/电机：功率法。单电机功率 = 总功率×余度系数/台数。

    Phase 1.5：同时生成完整螺旋桨布局（PropellerParams 列表），
    使 Propeller 成为一级设计变量（位置/直径/桨距/转速独立）。
    Phase 2：直径(prop_diameter_hint)/转速(prop_rpm)升级为可配置设计变量。
    Phase 2.5（BEMT）：
      - 直径：prop_diameter_factor 为「功率反推直径」的缩放系数，使桨盘与任务功率匹配；
        prop_diameter_hint（绝对直径）保留为手动/demo 覆盖项（优先级高于 factor）。
      - 桨距：匹配巡航工作点——迭代使巡航所需推力对应的转速下，0.75R 截面攻角
        = design_alpha_deg（默认 5°）；未提供巡航速度时回退 pitch_ratio·D。
    """
    p_total_max = power_cruise_w * power_margin
    p_single_kw = p_total_max / n_props / 1000.0
    # 电机质量
    motor_mass = p_total_max / 1000.0 / motor_specific_power_kwkg
    # 螺旋桨直径: 经验 D[m] ≈ 0.17·sqrt(P_single[W]/100)，限幅
    d_base = 0.17 * math.sqrt(p_total_max / n_props / 100.0)
    if prop_diameter_hint:
        d_prop = prop_diameter_hint                       # 绝对覆盖（demo/手动）
    elif prop_diameter_factor:
        # Phase 2.5 BEMT：直径 = 缩放系数 × 功率反推直径（与任务功率/推力匹配）
        d_prop = min(max(d_base * prop_diameter_factor, 0.15), 1.50)
    else:
        d_prop = min(max(d_base, 0.20), 1.20)             # 功率反推

    rpm = float(prop_rpm)                      # 设计转速
    # 桨距：BEMT 下匹配巡航工作点（0.75R 截面巡航攻角），否则回退 pitch_ratio·D
    if cruise_speed_ms and cruise_speed_ms > 0 and rpm > 0 and d_prop > 0:
        thrust_cruise_n = power_cruise_w * prop_efficiency / cruise_speed_ms
        pitch = _match_cruise_pitch(d_prop, rpm, cruise_speed_ms, rho, n_blades,
                                    thrust_cruise_n, design_alpha_deg,
                                    pitch_ratio, bemt_cfg)
    else:
        pitch = pitch_ratio * d_prop               # 向后兼容 fallback

    # 生成螺旋桨布局
    propellers = _build_propeller_layout(
        n_props, d_prop, pitch, rpm, prop_layout,
        fuselage_length_m, wing_x_m, wing_span_m)

    motor = MotorParams(
        max_power_kw=p_single_kw,
        efficiency=0.85,
        specific_power_kw_kg=motor_specific_power_kwkg,
    )
    battery = BatteryParams(
        voltage_v=22.2,
        capacity_ah=battery_energy_wh / 22.2 if battery_energy_wh else 0.0,
        specific_energy_wh_kg=battery_specific_energy_whkg,
        mass_kg=battery_mass_kg,
    )
    return PropulsionParams(
        n_props=n_props,
        propellers=propellers,
        motor=motor,
        battery=battery,
        # 兼容旧字段
        prop_diameter_m=d_prop,
        motor_power_kw=p_single_kw,
        battery_energy_wh=battery_energy_wh,
        battery_mass_kg=battery_mass_kg,
    )


def _build_propeller_layout(n_props: int, diameter_m: float, pitch_m: float,
                            rpm: float, layout: str, fuselage_length_m: float,
                            wing_x_m: float, wing_span_m: float) -> list[PropellerParams]:
    """按安装形式生成螺旋桨位置列表。

    layout: nose(机头) | tail(机尾) | wing(翼上) | distributed(分布式)
    """
    props: list[PropellerParams] = []
    d = diameter_m
    half_span = wing_span_m / 2.0

    if layout == "nose":
        # 机头单桨（或多桨同轴前推）
        for i in range(n_props):
            props.append(PropellerParams(
                diameter_m=d, pitch_m=pitch_m, rpm=rpm,
                x_m=-0.5 * d, y_m=0.0, z_m=0.0,
                location="nose"))
    elif layout == "tail":
        # 机尾推进（尾桨）
        for i in range(n_props):
            props.append(PropellerParams(
                diameter_m=d, pitch_m=pitch_m, rpm=rpm,
                x_m=fuselage_length_m + 0.5 * d, y_m=0.0, z_m=0.0,
                location="tail"))
    elif layout == "wing":
        # 翼上安装（沿展向分布）
        for i in range(n_props):
            # 沿半翼展均布（含对称侧）
            frac = (i + 1) / (n_props + 1)
            y = frac * half_span * (1 if i % 2 == 0 else -1)
            props.append(PropellerParams(
                diameter_m=d, pitch_m=pitch_m, rpm=rpm,
                x_m=wing_x_m, y_m=y, z_m=0.0,
                location="wing"))
    else:  # distributed 分布式
        # 分布式推进：沿翼展布置多个小桨
        for i in range(n_props):
            frac = (i + 1) / (n_props + 1) - 0.5
            y = frac * 2.0 * half_span
            props.append(PropellerParams(
                diameter_m=d, pitch_m=pitch_m, rpm=rpm,
                x_m=wing_x_m, y_m=y, z_m=0.0,
                location="distributed"))
    return props
