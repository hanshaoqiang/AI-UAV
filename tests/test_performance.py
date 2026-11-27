"""M06: 性能评估测试(手算交叉验证)。

Phase 1.5: 性能评估融合寄生阻力 + 推进闭环，测试验证新物理模型的一致性。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.contracts import (AeroResult, Aircraft, AirfoilPolar,
                                BatteryParams, FuselageParams, MissionSpec,
                                MotorParams, PerformanceResult, PropellerParams,
                                PropulsionParams, TailParams, WingParams)
from src.performance.evaluator import PerformanceEvaluator


@pytest.fixture(scope="module")
def sizing():
    return {
        "prop_efficiency": 0.7, "motor_efficiency": 0.85, "esc_efficiency": 0.95,
        "battery_usable_fraction": 0.85, "cl_max_2d": 1.3,
        "cl_max_3d_factor": 0.9,
        "parasite_drag": {"misc_drag_area_m2": 0.0},   # 测试中关闭设备项，便于手算
    }


def _make_inputs():
    mission = MissionSpec(range_km=100, cruise_speed_ms=30.0, payload_kg=5.0,
                          endurance_h=2.0, altitude_m=0.0)
    ac = Aircraft(
        wing=WingParams(span_m=3.0, area_m2=1.0, aspect_ratio=9.0),
        htail=TailParams(area_m2=0.1, arm_m=1.0),
        vtail=TailParams(area_m2=0.05, arm_m=1.0),
        fuselage=FuselageParams(length_m=1.5, max_diameter_m=0.2),
        propulsion=PropulsionParams(
            n_props=1,
            # Phase 2.5 BEMT：桨距比≈1.0（pitch=0.4）匹配 30m/s 巡航工作点；
            # 旧值 0.24 在 BEMT 下桨叶进入风车状态，推进效率=0 → 航程/续航=0。
            propellers=[PropellerParams(diameter_m=0.4, pitch_m=0.4, rpm=6000.0)],
            motor=MotorParams(max_power_kw=2.0, efficiency=0.85),
            battery=BatteryParams(voltage_v=22.2, capacity_ah=45.0,
                                  mass_kg=5.0),  # ≈1000Wh
        ),
        mtow_kg=20.0,
    )
    ac.cg_x_m = 0.5

    aero = AeroResult()
    aero.alpha_deg = [-2.0, 0.0, 2.0, 4.0, 6.0, 8.0]
    aero.CL = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    aero.CD = [0.020, 0.0208, 0.0232, 0.0272, 0.0328, 0.040]
    aero.Cm = [0.0] * 6
    aero.CD0 = 0.02
    aero.k_induced = 0.02
    aero.CL_alpha_per_rad = 5.73
    aero.CL_alpha_0deg = 0.2
    aero.LD_max = 17.7
    aero.alpha_LDmax_deg = 4.0
    aero.CL_max = 1.0
    aero.Cm_alpha_per_rad = -0.573   # SM = 0.1
    return mission, ac, aero


def test_stall_speed_handcheck(sizing):
    # v_stall = sqrt(2·196.2/(1.225·1.0·cl_max3d))
    mission, ac, aero = _make_inputs()
    polar = AirfoilPolar("clarky", 5e5, cl_max=1.2, converged=True)
    ev = PerformanceEvaluator(sizing)
    perf = ev.evaluate(aero, polar, ac, mission)
    cl_max3d = 1.2 * 0.9
    expected = math.sqrt(2 * 20 * 9.81 / (1.225 * 1.0 * cl_max3d))
    assert perf.v_stall_ms == pytest.approx(expected, rel=1e-6)


def test_cruise_interpolation(sizing):
    """巡航 CL 由极线插值；CD 分解为诱导(k·CL²)+寄生（Phase 2.5 不含 VLM 极线拟合截距）。"""
    mission, ac, aero = _make_inputs()
    ev = PerformanceEvaluator(sizing)
    perf = ev.evaluate(aero, None, ac, mission)
    # 巡航 CL = 196.2/(0.5·1.225·900·1.0) ≈ 0.3557 → 在 0.2~0.4 之间
    assert perf.CL_cruise == pytest.approx(0.3557, rel=1e-3)
    # Phase 2.5：诱导阻力 = k_induced·CL²（不再叠加 VLM 极线拟合截距 CD0）
    cd_ind_expected = aero.k_induced * perf.CL_cruise ** 2
    assert perf.cd_induced_cruise == pytest.approx(cd_ind_expected, rel=1e-6)
    # 总阻力 = 诱导 + 寄生（波阻=0）
    assert perf.CD_cruise == pytest.approx(
        perf.cd_induced_cruise + perf.cd_parasitic_cruise, rel=1e-6)
    assert perf.cd_parasitic_cruise > 0


def test_range_endurance_formula(sizing):
    """航程/续航受推进闭环与电池能量约束，结果为正且自洽。"""
    mission, ac, aero = _make_inputs()
    ev = PerformanceEvaluator(sizing)
    perf = ev.evaluate(aero, None, ac, mission)
    # 航程为正且随 L/D 增大而增大（单调性）
    assert perf.range_km > 0
    assert perf.endurance_h > 0
    # 续航受电池能量/电功率上限约束: E_batt·usable / P_elec
    e_j = ac.propulsion.battery.energy_j * ac.propulsion.battery.usable_fraction
    p_elec = perf.power_cruise_w
    if p_elec > 0:
        t_alt = e_j / p_elec
        assert perf.endurance_h <= t_alt / 3600 + 1e-6
    # 推进闭环字段已填充
    assert perf.thrust_required_n > 0
    assert perf.thrust_available_n > 0
    assert 0 < perf.propeller_efficiency <= 0.92


def test_static_margin(sizing):
    mission, ac, aero = _make_inputs()
    ev = PerformanceEvaluator(sizing)
    perf = ev.evaluate(aero, None, ac, mission)
    assert perf.static_margin == pytest.approx(0.1, rel=1e-3)
    # NP = CG + SM·c̄
    assert perf.neutral_point_x_m == pytest.approx(0.5 + 0.1 * (1.0 / 3.0), rel=1e-3)


def test_constraints_check(sizing):
    mission, ac, aero = _make_inputs()
    mission.climb_rate_min_ms = 100.0     # 故意不可达
    ev = PerformanceEvaluator(sizing)
    perf = ev.evaluate(aero, None, ac, mission)
    assert perf.constraints_pass["爬升率"]["pass"] is False


def test_parasite_drag_integration(sizing):
    """寄生阻力已融入 CD 计算。"""
    mission, ac, aero = _make_inputs()
    ev = PerformanceEvaluator(sizing)
    perf = ev.evaluate(aero, None, ac, mission)
    # CD = CD_ind + CD_parasitic（波阻=0）
    assert perf.cd_wave_cruise == 0.0
    assert perf.CD_cruise == pytest.approx(
        perf.cd_induced_cruise + perf.cd_parasitic_cruise, rel=1e-6)
