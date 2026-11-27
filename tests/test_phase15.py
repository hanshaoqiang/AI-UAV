"""Phase 1.5 新增模块测试：寄生阻力、推进、采样、批处理、数据库。"""
import math

import pytest

from src.core.contracts import (Aircraft, BatteryParams, FuselageParams,
                                MotorParams, PropellerParams, PropulsionParams,
                                TailParams, WingParams)
from src.performance.parasite_drag import (ParasiteDragModel, skin_friction,
                                           _fuselage_wetted_area, _wing_wetted_area)
from src.propulsion.model import (PropulsionModel, motor_efficiency,
                                  propeller_thrust_power)
from src.sampling.design_sampler import (DesignSampler, default_design_space,
                                         design_to_specs, specs_to_mission_dict)


# ============================================================
# 寄生阻力模型
# ============================================================
def test_skin_friction_laminar():
    """低雷诺数层流 Blasius: C_f = 1.328/sqrt(Re)。"""
    re = 1e5
    cf = skin_friction(re)
    assert cf == pytest.approx(1.328 / math.sqrt(re), rel=1e-3)


def test_skin_friction_turbulent_positive():
    """高雷诺数湍流摩擦系数应为正且随 Re 增大而减小。"""
    cf_1e6 = skin_friction(1e6)
    cf_1e7 = skin_friction(1e7)
    assert 0.003 < cf_1e6 < 0.006
    assert cf_1e7 < cf_1e6


def test_wetted_areas_positive():
    assert _wing_wetted_area(3.0, 1.2, 0.12) > 2.4   # 约 2·S·(1+...)
    assert _fuselage_wetted_area(2.0, 0.3) > 1.0


def test_parasite_drag_components():
    """寄生阻力分项齐全且 CD0 > 0。"""
    ac = Aircraft(
        wing=WingParams(span_m=3.0, area_m2=1.2, aspect_ratio=7.5),
        htail=TailParams(area_m2=0.24, arm_m=1.4),
        vtail=TailParams(area_m2=0.12, arm_m=1.4),
        fuselage=FuselageParams(length_m=2.0, max_diameter_m=0.3),
    )
    model = ParasiteDragModel()
    res = model.evaluate(ac, reynolds=5e5, mach=0.1)
    assert res.cd0 > 0.0
    assert set(res.components.keys()) == {"wing", "htail", "vtail", "fuselage", "misc"}
    assert res.components["wing"] > 0
    assert res.components["fuselage"] > 0
    # 分项之和 ≈ 总 CD0
    assert sum(res.components.values()) == pytest.approx(res.cd0, rel=1e-6)


def test_parasite_drag_scales_with_wetted_area():
    """浸润面积越大（机翼面积越大）寄生阻力越大。"""
    ac_small = Aircraft(
        wing=WingParams(span_m=2.0, area_m2=0.6, aspect_ratio=6.7),
        htail=TailParams(area_m2=0.12, arm_m=1.0),
        vtail=TailParams(area_m2=0.06, arm_m=1.0),
        fuselage=FuselageParams(length_m=1.2, max_diameter_m=0.18),
    )
    ac_big = Aircraft(
        wing=WingParams(span_m=4.0, area_m2=2.4, aspect_ratio=6.7),
        htail=TailParams(area_m2=0.48, arm_m=1.8),
        vtail=TailParams(area_m2=0.24, arm_m=1.8),
        fuselage=FuselageParams(length_m=3.0, max_diameter_m=0.45),
    )
    model = ParasiteDragModel()
    cd0_small = model.evaluate(ac_small, 5e5, 0.1).cd0
    cd0_big = model.evaluate(ac_big, 5e5, 0.1).cd0
    # 大飞机浸润面积比参考面积更大 → CD0 更大（同几何形状比例）
    assert cd0_big > cd0_small


# ============================================================
# 推进模型
# ============================================================
def test_propeller_thrust_zero_velocity():
    """静止推力 > 0（桨盘动量理论）。"""
    t, p, eta = propeller_thrust_power(0.4, 0.24, 6000.0, 0.0, 1.225)
    assert t > 0
    assert p > 0
    # 静止时推进效率为 0（无前进速度）
    assert eta == 0.0


def test_propeller_thrust_increases_with_rpm():
    t1, _, _ = propeller_thrust_power(0.4, 0.24, 6000.0, 10.0, 1.225)
    t2, _, _ = propeller_thrust_power(0.4, 0.24, 9000.0, 10.0, 1.225)
    assert t2 > t1


def test_motor_efficiency_peak():
    """电机效率在 60~80% 负载处接近峰值。"""
    eta_peak = motor_efficiency(0.7)
    eta_low = motor_efficiency(0.1)
    eta_high = motor_efficiency(1.4)
    assert eta_peak > eta_low
    assert eta_peak > eta_high
    assert 0.3 < eta_peak <= 0.85


def test_propulsion_model_evaluate():
    """推进系统评估产出完整结果。"""
    ac = Aircraft(
        propulsion=PropulsionParams(
            n_props=1,
            # Phase 2.5 BEMT：桨距比≈1.0（pitch=0.4）匹配 30m/s 巡航工作点；
            # 旧值 0.24（pitch_ratio=0.6）在 BEMT 下桨叶进入风车状态（推力为 0）。
            propellers=[PropellerParams(diameter_m=0.4, pitch_m=0.4, rpm=6000.0)],
            motor=MotorParams(max_power_kw=1.5, efficiency=0.85),
            battery=BatteryParams(voltage_v=22.2, capacity_ah=20.0, mass_kg=5.0),
        ),
    )
    model = PropulsionModel()
    res = model.evaluate(ac, velocity_ms=30.0, rho=1.225, required_thrust_n=15.0)
    assert res.thrust_required_n == pytest.approx(15.0)
    assert res.thrust_available_n > 0
    assert res.electric_power_w > 0
    assert res.shaft_power_w > 0
    assert res.n_propellers == 1
    assert 0 < res.propeller_efficiency <= 0.92


def test_propulsion_multi_propeller():
    """多桨（4 桨分布式）应均分推力。"""
    props = [PropellerParams(diameter_m=0.25, pitch_m=0.15, rpm=8000.0,
                             location="distributed", y_m=y)
             for y in (-1.0, -0.3, 0.3, 1.0)]
    ac = Aircraft(
        propulsion=PropulsionParams(
            n_props=4, propellers=props,
            motor=MotorParams(max_power_kw=0.5, efficiency=0.85),
            battery=BatteryParams(),
        ),
    )
    model = PropulsionModel()
    res = model.evaluate(ac, 25.0, 1.225, required_thrust_n=40.0)
    assert res.n_propellers == 4
    assert len(res.propellers) == 4


# ============================================================
# 采样器
# ============================================================
def test_random_sample_count_and_range():
    space = default_design_space()
    sampler = DesignSampler(space, seed=1)
    designs = sampler.random_sample(50)
    assert len(designs) == 50
    for d in designs:
        assert 50.0 <= d["range_km"] <= 300.0
        assert 6.5 <= d["aspect_ratio"] <= 14.0
        assert d["n_props"] in [1, 2, 3, 4]
        assert d["prop_layout"] in ["nose", "tail", "wing", "distributed"]


def test_lhs_sample_stratification():
    """LHS 采样应在每个维度上分层覆盖。"""
    space = default_design_space()
    sampler = DesignSampler(space, seed=2)
    n = 20
    designs = sampler.lhs_sample(n)
    assert len(designs) == n
    # 每维取值应近似覆盖整个区间（分位数跨度大）
    values = sorted(d["range_km"] for d in designs)
    assert values[0] < 120.0           # 低分位在区间前段
    assert values[-1] > 250.0          # 高分位在区间后段


def test_design_to_specs_structure():
    d = {"range_km": 200.0, "cruise_speed_ms": 30.0, "payload_kg": 5.0,
         "endurance_h": 2.0, "altitude_m": 500.0, "aspect_ratio": 9.0,
         "taper_ratio": 0.5, "htail_volume_coeff": 0.55,
         "vtail_volume_coeff": 0.04, "n_props": 2, "prop_layout": "wing"}
    specs = design_to_specs(d)
    assert set(specs.keys()) == {"mission", "geometry", "propulsion"}
    assert specs["mission"]["range_km"] == 200.0
    assert specs["geometry"]["aspect_ratio"] == 9.0
    assert specs["propulsion"]["n_props"] == 2


def test_specs_to_mission_dict_filters():
    m = {"range_km": 300.0, "cruise_speed_ms": 25.0, "unknown_key": 1.0}
    out = specs_to_mission_dict(m)
    assert "unknown_key" not in out
    assert out["range_km"] == 300.0
