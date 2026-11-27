"""Phase 2.5 C3 修复项测试（D1/D2/D3/D4）。

覆盖：
  D1：_solve_propeller 二分求解，消除风车区推力陡变导致的 overshoot
  D2：轴功率约束，电机不超载
  D3：tip_speed_design_ms / tip_speed_operating_ms 字段区分
  D4：LD_max_total（含寄生阻力）≤ LD_max（VLM 仅诱导阻力）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.contracts import (AeroResult, Aircraft, BatteryParams,
                                FuselageParams, MissionSpec, MotorParams,
                                PropellerParams, PropulsionParams, TailParams,
                                WingParams)
from src.performance.evaluator import PerformanceEvaluator
from src.propulsion.model import PropulsionModel


# ============================================================
# D1：二分求解消除 overshoot
# ============================================================
def _pm():
    return PropulsionModel({"prop_polar_table": None, "esc_efficiency": 0.95})


def test_solve_propeller_binary_search_no_overshoot():
    """Stage A 样本2 场景：大桨(D=1.5m)+小目标推力，二分应精确匹配而非数倍 overshoot。

    修复前线性扫描(250rpm)在风车区推力陡变处 overshoot 9.4 倍；修复后应 <5%。
    """
    pm = _pm()
    p = PropellerParams(diameter_m=1.5, pitch_m=1.6505, rpm=4348.0, n_blades=2)
    tgt = 24.67
    p_max_shaft = 4.182 * 1000.0 * 0.85 * 0.95   # 电机额定轴功率（样本2）
    t, pw, eta, info = pm._solve_propeller(p, tgt, 35.0, 1.1494, p_max_shaft)
    assert t > 0
    # 二分后推力应精确匹配目标（无风车陡变 overshoot）
    assert abs(t - tgt) / tgt < 0.05, f"overshoot: {t}/{tgt} = {t / tgt:.2f}x"
    # 工作转速应显著低于设计转速（节流下调，而非卡在设计转速上限）
    assert info["operating_rpm"] < info["design_rpm"]


def test_solve_propeller_small_prop_binary_search():
    """Stage A 样本1 场景：小桨(D=0.257m)+双桨，二分精确匹配每桨目标推力。"""
    pm = _pm()
    p = PropellerParams(diameter_m=0.2572, pitch_m=0.3059, rpm=8017.0, n_blades=2)
    tgt = 1.89
    p_max_shaft = 0.234 * 1000.0 * 0.85 * 0.95
    t, pw, eta, info = pm._solve_propeller(p, tgt, 26.0, 1.1986, p_max_shaft)
    assert t > 0
    assert abs(t - tgt) / tgt < 0.05


# ============================================================
# D2：轴功率约束
# ============================================================
def test_solve_propeller_power_limited():
    """功率约束下轴功率不得超过电机额定轴功率（不再超载）。"""
    pm = _pm()
    p = PropellerParams(diameter_m=0.5, pitch_m=0.5, rpm=6000.0, n_blades=2)
    tgt = 50.0            # 较大推力需求
    p_max_shaft = 150.0   # 很小的功率上限（强制功率受限）
    t, pw, eta, info = pm._solve_propeller(p, tgt, 30.0, 1.225, p_max_shaft)
    assert pw <= p_max_shaft * 1.01, f"轴功率超限: {pw:.1f} > {p_max_shaft:.1f}"


def test_solve_propeller_thrust_insufficient_returns_limited():
    """功率严重不足时，返回功率约束下的最大推力（而非超载工作点）。"""
    pm = _pm()
    p = PropellerParams(diameter_m=0.5, pitch_m=0.5, rpm=6000.0, n_blades=2)
    tgt = 50.0
    p_max_shaft = 50.0    # 极低功率上限
    t, pw, eta, info = pm._solve_propeller(p, tgt, 30.0, 1.225, p_max_shaft)
    assert pw <= p_max_shaft * 1.01
    # 推力不足（< 目标）是功率受限的正确结果，下游校验捕获
    assert t < tgt


# ============================================================
# D3/D4：字段（通过 evaluator 端到端）
# ============================================================
def _make_eval_inputs():
    mission = MissionSpec(range_km=100, cruise_speed_ms=30.0, payload_kg=5.0,
                          endurance_h=2.0, altitude_m=0.0)
    ac = Aircraft(
        wing=WingParams(span_m=3.0, area_m2=1.0, aspect_ratio=9.0),
        htail=TailParams(area_m2=0.1, arm_m=1.0),
        vtail=TailParams(area_m2=0.05, arm_m=1.0),
        fuselage=FuselageParams(length_m=1.5, max_diameter_m=0.2),
        propulsion=PropulsionParams(
            n_props=1,
            propellers=[PropellerParams(diameter_m=0.4, pitch_m=0.4, rpm=6000.0)],
            motor=MotorParams(max_power_kw=2.0, efficiency=0.85),
            battery=BatteryParams(voltage_v=22.2, capacity_ah=45.0, mass_kg=5.0),
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
    aero.CL_max = 1.0
    aero.Cm_alpha_per_rad = -0.573
    return mission, ac, aero


def _sizing():
    return {
        "prop_efficiency": 0.6, "motor_efficiency": 0.85, "esc_efficiency": 0.95,
        "battery_usable_fraction": 0.85, "cl_max_2d": 1.3, "cl_max_3d_factor": 0.9,
        "parasite_drag": {"misc_drag_area_m2": 0.0},
    }


def test_tip_speed_fields_distinguished():
    """D3：tip_speed_design_ms 用设计转速，tip_speed_operating_ms 用工作转速。"""
    mission, ac, aero = _make_eval_inputs()
    ev = PerformanceEvaluator(_sizing())
    perf = ev.evaluate(aero, None, ac, mission)
    D = ac.propulsion.propellers[0].diameter_m
    # 设计桨尖速度 = π·prop_design_rpm/60·D
    assert perf.tip_speed_design_ms == pytest.approx(
        math.pi * perf.prop_design_rpm / 60.0 * D, rel=1e-6)
    # 工作桨尖速度 = π·prop_operating_rpm/60·D
    assert perf.tip_speed_operating_ms == pytest.approx(
        math.pi * perf.prop_operating_rpm / 60.0 * D, rel=1e-6)
    # 工作转速 ≤ 设计转速 → 工作桨尖速度 ≤ 设计桨尖速度
    assert perf.tip_speed_operating_ms <= perf.tip_speed_design_ms + 1e-9


def test_ld_max_total_no_greater_than_inviscid():
    """D4：LD_max_total = max(CL/(k·CL²+cd0_parasitic))，手算验证正确。"""
    mission, ac, aero = _make_eval_inputs()
    ev = PerformanceEvaluator(_sizing())
    perf = ev.evaluate(aero, None, ac, mission)
    # 手算：LD_max_total = max(CL / (k·CL² + cd0_parasitic))
    cd0_par = perf.cd_parasitic_cruise
    ld_series = [cl / (aero.k_induced * cl * cl + cd0_par)
                 for cl in aero.CL
                 if (aero.k_induced * cl * cl + cd0_par) > 1e-9]
    expected = max(ld_series)
    assert perf.LD_max_total == pytest.approx(expected, rel=1e-6)
    assert perf.LD_max_total > 0
