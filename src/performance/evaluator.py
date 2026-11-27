"""M06: PerformanceEvaluator — 气动结果 + 任务 → 飞行性能 (PerformanceResult)。

本模块独立于 OpenVSP：只消费 AeroResult / AirfoilPolar / MissionSpec 接口。
Phase 1.5：融合 ParasiteDragModel（寄生阻力）+ PropulsionModel（推进闭环）。
"""
from __future__ import annotations

import logging
import math

from src.core.contracts import (AeroResult, Aircraft, AirfoilPolar,
                                MissionSpec, PerformanceResult)
from src.performance.parasite_drag import ParasiteDragModel
from src.propulsion.model import PropulsionModel

logger = logging.getLogger("uavdp.performance")

G = 9.81


def air_density(altitude_m: float) -> float:
    return 1.225 * (1.0 - 2.25577e-5 * altitude_m) ** 4.2559


class PerformanceEvaluator:
    """输入: AeroResult + AirfoilPolar + Aircraft + MissionSpec; 输出: PerformanceResult。"""

    def __init__(self, sizing: dict, parasite_cfg: dict | None = None,
                 propulsion_cfg: dict | None = None):
        self.cfg = sizing
        self.parasite = ParasiteDragModel(parasite_cfg or sizing.get("parasite_drag"))
        self.propulsion = PropulsionModel(propulsion_cfg or sizing)

    def evaluate(self, aero: AeroResult, polar: AirfoilPolar | None,
                 ac: Aircraft, mission: MissionSpec) -> PerformanceResult:
        rho = air_density(mission.altitude_m)
        rho0 = 1.225
        w = ac.mtow_kg * G
        s = ac.wing.area_m2
        cbar = ac.wing.mean_chord_m
        ar = ac.wing.aspect_ratio
        v_c = mission.cruise_speed_ms

        # ---- 失速 ----
        cl_max_3d = 1.0
        if polar and polar.converged and polar.cl_max > 0.2:
            cl_max_3d = polar.cl_max * self.cfg["cl_max_3d_factor"]
        elif aero.CL_max and aero.CL_max > 0.2:
            cl_max_3d = min(aero.CL_max, 1.4)
        cl_max_3d = float(min(max(cl_max_3d, 0.5), 1.6))   # 合理范围保护
        v_stall = (2.0 * w / (rho0 * s * cl_max_3d)) ** 0.5   # 海平面最保守

        # ---- 寄生阻力（Phase 1.5: 独立模型）----
        re_cruise = rho * v_c * cbar / 1.7894e-5
        mach = v_c / 340.3   # 声速近似
        parasite = self.parasite.evaluate(ac, re_cruise, mach, s)
        cd0_parasitic = parasite.cd0
        # 注意：aero.CD0 是 VLM 极线拟合截距（数值伪零升阻力），非物理阻力，
        # 不可与组件叠加法的 cd0_parasitic 叠加（Phase 2.5 修复重复计数）。
        # VLM 真实贡献仅为诱导阻力 k_induced·CL²。

        # ---- 巡航状态（诱导阻力=VLM拟合k，寄生阻力=组件叠加法）----
        cl_cr = w / (0.5 * rho * v_c ** 2 * s)
        cl_cruise, _, alpha_cr = _interp_polar(aero, cl_cr)
        # 总阻力 = 诱导(VLM k·CL²) + 寄生（波阻在亚声速≈0）
        cd_ind_cr = aero.k_induced * cl_cruise ** 2
        cd_wave_cruise = 0.0
        cd_cruise = cd_ind_cr + cd0_parasitic
        ld_cruise = cl_cruise / cd_cruise if cd_cruise > 0 else 0.0
        drag_cruise = 0.5 * rho * v_c ** 2 * s * cd_cruise

        # ---- 推进闭环（Phase 1.5: PropulsionModel）----
        thrust_required = drag_cruise
        prop_res = self.propulsion.evaluate(ac, v_c, rho, thrust_required)
        eta_prop = prop_res.propeller_efficiency if prop_res.propeller_efficiency > 0 \
            else self.cfg["prop_efficiency"]
        p_cruise_elec = prop_res.electric_power_w
        p_available_w = prop_res.thrust_available_n * v_c  # 可用推力功率
        thrust_available = prop_res.thrust_available_n

        # ---- 航程/续航（电推进 Breguet）----
        e_batt_j = ac.propulsion.battery.energy_j * ac.propulsion.battery.usable_fraction
        usable = 1.0  # 已包含在 energy_j 的 usable_fraction
        ld_best = max(aero.LD_max, 1.0)
        # 巡航总效率 = 螺旋桨×电机×ESC
        eta_total = eta_prop * prop_res.motor_efficiency * prop_res.esc_efficiency
        range_m = e_batt_j * eta_total * ld_cruise / w
        endurance_s = range_m / v_c if v_c > 0 else 0.0
        endurance_alt_s = e_batt_j / p_cruise_elec if p_cruise_elec > 0 else 0.0
        endurance_s = min(endurance_s, endurance_alt_s)

        # ---- 爬升（剩余功率法）----
        v_climb = v_c
        p_req_climb = drag_cruise * v_climb / eta_prop
        excess = p_available_w - p_req_climb
        climb_rate = max(excess / w, 0.0)

        # ---- 稳定性 ----
        sm = 0.0
        if aero.CL_alpha_per_rad != 0 and aero.Cm_alpha_per_rad != 0:
            sm = -aero.Cm_alpha_per_rad / aero.CL_alpha_per_rad
        neutral_point_x = ac.cg_x_m + sm * cbar

        # Phase 2.5：配平前原始稳定性信号（保留，不被重心配平覆盖）
        trimmed = aero.neutral_point_x_pre_trim_m != 0.0
        if trimmed and aero.CL_alpha_per_rad != 0:
            sm_pre = -aero.Cm_alpha_pre_trim_per_rad / aero.CL_alpha_per_rad
        else:
            sm_pre = sm
        np_pre = aero.neutral_point_x_pre_trim_m if trimmed else neutral_point_x
        cg_pre = aero.cg_x_pre_trim_m if trimmed else ac.cg_x_m
        cg_shift = aero.cg_shift_m if trimmed else 0.0
        sm_target = aero.static_margin_target if trimmed else 0.0

        # ---- 推重比 ----
        tw = thrust_available / w

        # Phase 2.5：推进转速记录（设计/求解/实际工作，取首桨代表值）
        prop_design_rpm = prop_solve_rpm = prop_operating_rpm = 0.0
        if prop_res.propellers:
            pd = prop_res.propellers[0]
            prop_design_rpm = pd.get("design_rpm", 0.0)
            prop_solve_rpm = pd.get("solve_rpm", 0.0)
            prop_operating_rpm = pd.get("operating_rpm", 0.0)

        # Phase 2.5 C3 D3：桨尖速度（区分设计转速 vs 实际工作转速，避免标签误导）
        tip_speed_design_ms = tip_speed_operating_ms = 0.0
        if ac.propulsion.propellers:
            prop_diam = ac.propulsion.propellers[0].diameter_m
            tip_speed_design_ms = math.pi * (prop_design_rpm / 60.0) * prop_diam
            tip_speed_operating_ms = math.pi * (prop_operating_rpm / 60.0) * prop_diam

        # Phase 2.5 C3 D4：含寄生阻力的最大升阻比（VLM 的 LD_max 仅含诱导阻力，
        # 偏乐观；此处对极线各点补寄生阻力后取最大）
        ld_max_total = aero.LD_max
        if aero.CL and aero.k_induced > 0:
            ld_series = [cl / (aero.k_induced * cl * cl + cd0_parasitic)
                         for cl in aero.CL
                         if (aero.k_induced * cl * cl + cd0_parasitic) > 1e-9]
            if ld_series:
                ld_max_total = max(ld_series)

        # 航程裕度（Phase 2.5：预测航程 - 任务航程；任务航程为输入目标）
        range_margin_km = (range_m - mission.range_km * 1000.0) / 1000.0 \
            if mission.range_km else 0.0

        res = PerformanceResult(
            v_stall_ms=v_stall,
            v_cruise_ms=v_c,
            alpha_cruise_deg=alpha_cr,
            CL_cruise=cl_cruise,
            CD_cruise=cd_cruise,
            LD_cruise=ld_cruise,
            LD_max=aero.LD_max,
            LD_max_total=ld_max_total,
            drag_cruise_n=drag_cruise,
            power_cruise_w=p_cruise_elec,
            cd_induced_cruise=cd_ind_cr,
            cd_parasitic_cruise=cd0_parasitic,
            cd_wave_cruise=cd_wave_cruise,
            thrust_required_n=thrust_required,
            thrust_available_n=thrust_available,
            thrust_weight_ratio=tw,
            power_available_w=p_available_w,
            excess_power_w=excess,
            motor_efficiency=prop_res.motor_efficiency,
            propeller_efficiency=eta_prop,
            prop_design_rpm=prop_design_rpm,
            prop_solve_rpm=prop_solve_rpm,
            prop_operating_rpm=prop_operating_rpm,
            tip_speed_design_ms=tip_speed_design_ms,
            tip_speed_operating_ms=tip_speed_operating_ms,
            range_km=range_m / 1000.0,
            range_margin_km=range_margin_km,
            endurance_h=endurance_s / 3600.0,
            climb_rate_ms=climb_rate,
            static_margin=sm,
            neutral_point_x_m=neutral_point_x,
            cg_x_m=ac.cg_x_m,
            static_margin_pre_trim=sm_pre,
            neutral_point_x_pre_trim_m=np_pre,
            cg_x_pre_trim_m=cg_pre,
            cg_shift_m=cg_shift,
            static_margin_target=sm_target,
            htail_volume_coeff=ac.htail_volume_coeff,
            vtail_volume_coeff=ac.vtail_volume_coeff,
        )
        res.constraints_pass = self._check_constraints(res, mission, ac)

        # 回填气动结果阻力分解（供报告/入库）
        aero.CD0_parasitic = cd0_parasitic
        aero.CD0_wave = cd_wave_cruise

        logger.info(
            "性能评估: v_stall=%.1f m/s, L/D_cr=%.1f, P_cr=%.0f W, "
            "R=%.0f km, E=%.1f h, ROC=%.1f m/s, SM=%.3f, CD0_par=%.5f",
            res.v_stall_ms, res.LD_cruise, res.power_cruise_w,
            res.range_km, res.endurance_h, res.climb_rate_ms, res.static_margin,
            cd0_parasitic)
        return res

    def _check_constraints(self, res: PerformanceResult, mission: MissionSpec,
                           ac: Aircraft) -> dict:
        checks = {
            "航程": (res.range_km >= mission.range_km,
                     f"{res.range_km:.0f} ≥ {mission.range_km:.0f} km"),
            "续航": (res.endurance_h >= mission.endurance_h,
                     f"{res.endurance_h:.1f} ≥ {mission.endurance_h:.1f} h"),
            "静稳定裕度": (mission.static_margin_min <= res.static_margin
                          <= mission.static_margin_max,
                          f"{res.static_margin:.3f} ∈ "
                          f"[{mission.static_margin_min}, {mission.static_margin_max}]"),
        }
        if mission.stall_speed_max_ms:
            checks["失速速度"] = (res.v_stall_ms <= mission.stall_speed_max_ms,
                                 f"{res.v_stall_ms:.1f} ≤ {mission.stall_speed_max_ms:.1f} m/s")
        if mission.climb_rate_min_ms:
            checks["爬升率"] = (res.climb_rate_ms >= mission.climb_rate_min_ms,
                                f"{res.climb_rate_ms:.1f} ≥ {mission.climb_rate_min_ms:.1f} m/s")
        return {k: {"pass": ok, "detail": detail}
                for k, (ok, detail) in checks.items()}


def _interp_polar(aero: AeroResult, cl_target: float) -> tuple:
    """按 CL 返回 (CL, 诱导阻力CD_ind, alpha)。

    Phase 2.5：CD_ind = k_induced·CL²（VLM 拟合的诱导阻力），
    不再返回含拟合截距 CD0 的原始极线 CD（避免与寄生阻力重复计数）。
    alpha 在极线线性区插值，超出范围用升力线斜率外推。
    """
    pairs = sorted(zip(aero.CL, aero.CD, aero.alpha_deg))
    cls = [p[0] for p in pairs]
    if not cls:
        return cl_target, aero.k_induced * cl_target ** 2, 3.0
    if min(cls) <= cl_target <= max(cls):
        for (cl0, cd0, a0), (cl1, cd1, a1) in zip(pairs, pairs[1:]):
            if cl0 <= cl_target <= cl1 and cl1 > cl0:
                f = (cl_target - cl0) / (cl1 - cl0)
                return cl_target, aero.k_induced * cl_target ** 2, a0 + f * (a1 - a0)
    # 超出扫描范围: alpha 用升力线斜率外推
    alpha0 = aero.CL_alpha_0deg
    alpha = alpha0 + (cl_target / aero.CL_alpha_per_rad) if aero.CL_alpha_per_rad else 3.0
    return cl_target, aero.k_induced * cl_target ** 2, alpha
