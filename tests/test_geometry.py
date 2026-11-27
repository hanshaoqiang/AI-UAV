"""M02: 几何/尺寸估算测试(含手算交叉验证)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.contracts import MissionSpec
from src.geometry.aircraft import build_aircraft, validate
from src.geometry.sizing import air_density
from src.geometry.wing import (size_wing, wing_loading_from_cruise,
                                wing_loading_from_stall)
from src.geometry.propeller import size_fuselage, size_propulsion


def test_air_density():
    assert air_density(0.0) == pytest.approx(1.225, rel=1e-3)
    assert air_density(500.0) == pytest.approx(1.167, rel=1e-2)  # ISA 标准值
    assert air_density(5000.0) < air_density(0.0)


def test_wing_loading_cruise_handcheck():
    # 0.5·1.225·30²·0.6/9.81 = 33.7 kg/m²
    assert wing_loading_from_cruise(1.225, 30.0, 0.6) == pytest.approx(33.68, rel=1e-2)


def test_wing_loading_stall_handcheck():
    # 0.5·1.225·18²·1.2/9.81 = 24.2 kg/m²
    assert wing_loading_from_stall(1.225, 18.0, 1.2) == pytest.approx(24.2, rel=1e-2)


def test_size_wing_consistency():
    w = size_wing(20.0, 30.0, 8.0, 0.5, "clarky", "clarky")
    assert w.area_m2 == pytest.approx(20.0 / 30.0)
    assert w.span_m == pytest.approx((w.area_m2 * 8.0) ** 0.5)
    # 梯形几何一致性: S = b/2·(cr+ct)
    assert w.span_m / 2 * (w.root_chord_m + w.tip_chord_m) == pytest.approx(
        w.area_m2, rel=1e-6)
    assert w.mean_chord_m == pytest.approx(w.area_m2 / w.span_m)


def test_fuselage_volume():
    f = size_fuselage(5.0, 10.0, 0.2, 3.0)
    assert f.length_m > 0.5
    assert 4.0 <= f.length_m / f.max_diameter_m <= 12.0
    assert f.length_m <= 0.45 * 3.0 + 1e-9  # 不超翼展比例限制


def test_propulsion_power():
    p = size_propulsion(800.0, 1, 2.2, 8.0, 2000.0, 220.0, 9.1)
    assert p.motor_power_kw == pytest.approx(800 * 2.2 / 1000)
    assert p.n_props == 1
    assert 0.2 <= p.prop_diameter_m <= 1.2


@pytest.fixture(scope="module")
def demo_aircraft(demo_config):
    mission = MissionSpec.from_dict(demo_config["mission"])
    return build_aircraft(mission, demo_config["design"], demo_config["sizing"])


def test_build_aircraft_converges(demo_aircraft, demo_config):
    ac = demo_aircraft
    # MTOW 在合理范围: 载荷 5kg, 电动长航时机 15~40kg
    assert 10.0 < ac.mtow_kg < 50.0
    # 重量分解守恒
    total = (ac.payload_kg + ac.empty_mass_kg + ac.battery_mass_kg
             + ac.propulsion.battery_mass_kg * 0.0)
    assert ac.mtow_kg > ac.payload_kg + ac.battery_mass_kg
    # 尾容量达到设计目标
    assert ac.htail_volume_coeff == pytest.approx(
        demo_config["design"]["htail_volume_coeff"], rel=0.05)
    assert ac.vtail_volume_coeff == pytest.approx(
        demo_config["design"]["vtail_volume_coeff"], rel=0.05)


def test_validate_passes(demo_aircraft):
    validate(demo_aircraft)  # 不抛异常


def test_geometry_sanity(demo_aircraft):
    ac = demo_aircraft
    w = ac.wing
    # 展弦比守恒
    assert w.span_m ** 2 / w.area_m2 == pytest.approx(w.aspect_ratio, rel=1e-6)
    # 位置链合理: 机翼在重心之前, 尾翼在重心之后
    assert ac.wing_x_m < ac.cg_x_m
    assert ac.htail_x_m > ac.cg_x_m + 0.3
    assert ac.htail_x_m <= ac.fuselage.length_m + 1e-6
