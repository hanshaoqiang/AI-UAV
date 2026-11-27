"""Phase 2: DataValidator 数据质量控制测试。

覆盖：
  - 正常样本 → valid=True，无 error
  - 各类异常样本 → error 级失败（几何/重量/静稳定/推进/NaN/气动）
  - 航程未达任务目标 → warning（valid 仍 True）
  - filter / is_valid / validate_result / 配置加载
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.contracts import (AeroResult, Aircraft, FuselageParams, MissionSpec,
                                PerformanceResult, TailParams, WingParams)
from src.validation import (DataValidator, default_validation_config,
                            load_validation_config)
from src.validation.validator import _builtin_validation_config, _is_bad


# ---------------- 构造器 ----------------
def make_good(mission: MissionSpec | None = None):
    """构造一组物理合理、满足任务约束的设计结果。"""
    mission = mission or MissionSpec(range_km=100.0, cruise_speed_ms=30.0,
                                     payload_kg=2.0, endurance_h=2.0, altitude_m=500.0)
    wing = WingParams(span_m=3.0, area_m2=1.2, aspect_ratio=7.5, taper_ratio=0.5,
                      sweep_quarter_chord_deg=2.0, dihedral_deg=3.0, twist_tip_deg=-2.0,
                      airfoil_root="clarky", airfoil_tip="clarky")
    htail = TailParams(area_m2=0.24, aspect_ratio=4.0, taper_ratio=0.6,
                       sweep_quarter_chord_deg=5.0, arm_m=1.4, airfoil="naca0008")
    vtail = TailParams(area_m2=0.06, aspect_ratio=1.8, taper_ratio=0.6,
                       sweep_quarter_chord_deg=12.0, arm_m=1.4, airfoil="naca0008")
    fus = FuselageParams(length_m=2.0, max_diameter_m=0.35)
    ac = Aircraft(wing=wing, htail=htail, vtail=vtail, fuselage=fus,
                  mtow_kg=15.0, empty_mass_kg=8.0, payload_kg=2.0, battery_mass_kg=5.0,
                  wing_x_m=0.4, htail_x_m=1.8, vtail_x_m=1.8, cg_x_m=0.7)
    aero = AeroResult(CD0=0.03, k_induced=0.07, CL_alpha_per_rad=4.5, CL_alpha_0deg=0.0,
                      CL_max=1.1, LD_max=15.0, alpha_LDmax_deg=5.0,
                      Cm_alpha_per_rad=-0.5, reference_x_m=0.7)
    perf = PerformanceResult(
        v_stall_ms=14.0, v_cruise_ms=30.0, alpha_cruise_deg=3.0, CL_cruise=0.55,
        CD_cruise=0.05, LD_cruise=11.0, LD_max=15.0, drag_cruise_n=15.0,
        power_cruise_w=800.0, thrust_required_n=15.0, thrust_available_n=25.0,
        thrust_weight_ratio=0.17, power_available_w=1500.0, excess_power_w=500.0,
        motor_efficiency=0.85, propeller_efficiency=0.7, range_km=120.0,
        endurance_h=2.2, climb_rate_ms=3.0, static_margin=0.10,
        neutral_point_x_m=0.76, cg_x_m=0.7, htail_volume_coeff=0.7, vtail_volume_coeff=0.023)
    return mission, ac, aero, perf


def _result_dict(mission, ac, aero, perf) -> dict:
    return {"mission": mission, "aircraft": ac, "aero": aero, "performance": perf}


# ---------------- 正常/基础 ----------------
def test_valid_sample_passes():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is True
    assert len(res.errors) == 0
    assert len(res.warnings) == 0
    assert res.n_checks > 0
    assert res.summary()["valid"] is True


def test_config_has_all_categories():
    cfg = default_validation_config()
    for cat in ("geometry", "weight", "stability", "propulsion", "aerodynamics", "range"):
        assert cat in cfg, f"缺少阈值类别: {cat}"


def test_load_validation_config_roundtrip():
    p = ROOT / "config" / "validation.yaml"
    cfg = load_validation_config(p)
    assert cfg["geometry"]["span_m_min"] == 0.5
    assert cfg["weight"]["mtow_kg"] == [0.5, 5000.0]
    # 缺文件报错
    with pytest.raises(FileNotFoundError):
        load_validation_config(ROOT / "config" / "no_such_validation.yaml")


def test_builtin_fallback_matches_default_structure():
    builtin = _builtin_validation_config()
    assert set(builtin.keys()) == set(default_validation_config().keys())


def test_is_bad():
    import math
    assert _is_bad(None) is True
    assert _is_bad(float("nan")) is True
    assert _is_bad(float("inf")) is True
    assert _is_bad(1.0) is False
    assert _is_bad(True) is False


# ---------------- 各异常类别 ----------------
def test_nan_rejected():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    perf.range_km = float("nan")
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is False
    assert any(c.category == "sanity" and not c.passed for c in res.errors)


def test_geometry_infeasible_rejected():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    ac.wing.taper_ratio = 2.0   # 锥比超出 [0.2, 1.0]，且根弦 < 梢弦
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is False
    assert any(c.category == "geometry" and not c.passed for c in res.errors)


def test_weight_unreasonable_rejected():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    ac.empty_mass_kg = 20.0     # 空重 > MTOW
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is False
    assert any(c.category == "weight" and not c.passed for c in res.errors)


def test_static_unstable_rejected():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    perf.static_margin = -0.1   # 静不稳定
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is False
    assert any(c.category == "stability" and not c.passed for c in res.errors)


def test_propulsion_insufficient_rejected():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    perf.thrust_available_n = 0.0
    perf.thrust_weight_ratio = 0.0
    perf.excess_power_w = -100.0
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is False
    assert any(c.category == "propulsion" and not c.passed for c in res.errors)


def test_aero_degenerate_rejected():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    aero.CL_alpha_per_rad = 0.0   # 升力线斜率为 0（面元退化）
    aero.CL_max = 0.0
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is False
    assert any(c.category == "aerodynamics" and not c.passed for c in res.errors)


# ---------------- 航程约束（warning） ----------------
def test_range_below_mission_is_warning_not_error():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    perf.range_km = 80.0         # < 任务目标 100 km → warning
    perf.endurance_h = 1.5       # < 任务目标 2.0 h → warning
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is True          # 仍为物理有效样本
    assert any(c.category == "range" and c.severity == "warning"
               and not c.passed for c in res.warnings)
    assert all(c.severity != "error" for c in res.warnings)


def test_range_zero_is_error():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    perf.range_km = 0.0
    res = v.validate(mission, ac, aero, perf)
    assert res.valid is False
    assert any(c.category == "range" and c.severity == "error" and not c.passed
               for c in res.errors)


def test_no_mission_skips_warning_checks():
    v = DataValidator()
    _, ac, aero, perf = make_good()
    perf.range_km = 80.0
    res = v.validate(None, ac, aero, perf)
    assert res.valid is True
    assert all(c.category != "range" or c.severity != "warning" for c in res.checks)


# ---------------- validate_result / filter ----------------
def test_validate_result_from_dict():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    res = v.validate_result(_result_dict(mission, ac, aero, perf))
    assert res.valid is True
    # 不含 mission 键时，mission 参数优先
    res2 = v.validate_result({"aircraft": ac, "aero": aero, "performance": perf},
                             mission=mission)
    assert res2.valid is True


def test_filter_splits_valid_and_rejected():
    v = DataValidator()
    mission1, ac1, aero1, perf1 = make_good()
    _, ac2, aero2, perf2 = make_good()
    perf2.range_km = float("nan")   # 异常
    _, ac3, aero3, perf3 = make_good()
    perf3.static_margin = -0.2      # 异常
    results = [
        _result_dict(mission1, ac1, aero1, perf1),
        _result_dict(mission1, ac2, aero2, perf2),
        _result_dict(mission1, ac3, aero3, perf3),
    ]
    valid, rejected = v.filter(results)
    assert len(valid) == 1
    assert len(rejected) == 2


def test_filter_strict_excludes_warnings():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    perf.range_km = 80.0   # warning
    valid, rejected = v.filter([_result_dict(mission, ac, aero, perf)], strict=True)
    assert len(valid) == 0
    assert len(rejected) == 1
    # 非 strict 则通过
    valid2, rejected2 = v.filter([_result_dict(mission, ac, aero, perf)], strict=False)
    assert len(valid2) == 1


def test_validation_result_serialization():
    v = DataValidator()
    mission, ac, aero, perf = make_good()
    perf.range_km = float("nan")
    res = v.validate(mission, ac, aero, perf)
    d = res.to_dict()
    assert d["valid"] is False
    assert isinstance(d["checks"], list)
    assert all("category" in c and "passed" in c and "severity" in c for c in d["checks"])
    s = res.summary()
    assert s["n_errors"] == len(res.errors)
