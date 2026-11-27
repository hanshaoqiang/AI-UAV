"""Phase 2 模块 1：Design Space 扩展测试。

覆盖：
  - 设计空间 YAML 加载与序列化（禁止硬编码）
  - 新增设计变量（后掠/上反/扭转/翼型/安装位置/尾翼锥比后掠/螺旋桨直径转速）
  - Sobol 采样
  - design_to_specs 新变量映射
  - 几何管道贯通（采样值流入 WingParams/TailParams/PropellerParams）
"""
from __future__ import annotations

import pytest

from src.core.contracts import MissionSpec
from src.sampling.design_sampler import (DesignSampler, DesignSpace,
                                         default_design_space, design_to_specs,
                                         load_design_space)

NEW_CONTINUOUS = [
    "wing_sweep_deg", "wing_dihedral_deg", "wing_twist_deg", "wing_x_fraction",
    "htail_taper_ratio", "htail_sweep_deg", "vtail_taper_ratio", "vtail_sweep_deg",
    # Phase 2.5 BEMT：prop_diameter_m（绝对直径）改为 prop_diameter_factor（直径缩放系数），
    # 使桨盘直径与任务功率/推力匹配；prop_diameter_m 保留为手动/demo 覆盖项（非采样变量）。
    "prop_diameter_factor", "prop_rpm",
]
NEW_DISCRETE = ["airfoil_root", "airfoil_tip"]


# ============================================================
# 设计空间定义
# ============================================================
def test_default_space_loads_from_yaml():
    """默认设计空间应从 YAML 加载（含 Phase 2 扩展变量）。"""
    space = default_design_space()
    names = set(space.names())
    assert "range_km" in names
    assert "aspect_ratio" in names
    for v in NEW_CONTINUOUS + NEW_DISCRETE:
        assert v in names, f"缺少新增变量 {v}"
    # 离散翼型白名单非空
    root = next(d for d in space.discrete if d.name == "airfoil_root")
    tip = next(d for d in space.discrete if d.name == "airfoil_tip")
    assert len(root.choices) >= 10
    assert len(tip.choices) >= len(root.choices)


def test_design_space_roundtrip():
    """to_dict → from_dict 应保持变量一致。"""
    space = default_design_space()
    restored = DesignSpace.from_dict(space.to_dict())
    assert restored.names() == space.names()
    for a, b in zip(space.continuous, restored.continuous):
        assert (a.name, a.lo, a.hi, a.log_scale) == (b.name, b.lo, b.hi, b.log_scale)
    for a, b in zip(space.discrete, restored.discrete):
        assert a.name == b.name and a.choices == b.choices


def test_load_design_space_file():
    """直接加载 config/design_space.yaml。"""
    from src.sampling.design_sampler import DEFAULT_SPACE_PATH
    space = load_design_space(DEFAULT_SPACE_PATH)
    assert space.n_dims() > 11   # 比 Phase 1.5 的 11 维更多


def test_load_design_space_missing_raises():
    with pytest.raises(FileNotFoundError):
        load_design_space("__nonexistent_design_space__.yaml")


# ============================================================
# 采样
# ============================================================
def test_random_sample_new_variables_in_range():
    space = default_design_space()
    sampler = DesignSampler(space, seed=3)
    designs = sampler.random_sample(100)
    for d in designs:
        assert 0.0 <= d["wing_sweep_deg"] <= 20.0
        assert 0.0 <= d["wing_dihedral_deg"] <= 5.0
        assert -3.0 <= d["wing_twist_deg"] <= 0.0
        assert 0.25 <= d["wing_x_fraction"] <= 0.40
        assert 0.70 <= d["prop_diameter_factor"] <= 1.60
        assert 4000.0 <= d["prop_rpm"] <= 9000.0
        assert d["airfoil_root"] in _root_choices(space)
        assert d["airfoil_tip"] in _tip_choices(space)


def test_sobol_sample_count_and_range():
    space = default_design_space()
    sampler = DesignSampler(space, seed=5)
    n = 16  # 2 的幂，Sobol 平衡性最佳
    designs = sampler.sobol_sample(n)
    assert len(designs) == n
    for d in designs:
        assert 6.5 <= d["aspect_ratio"] <= 14.0
        assert d["n_props"] in [1, 2, 3, 4]


def test_sobol_sample_deterministic_with_seed():
    space = default_design_space()
    a = DesignSampler(space, seed=99).sobol_sample(8)
    b = DesignSampler(space, seed=99).sobol_sample(8)
    assert [x["range_km"] for x in a] == [x["range_km"] for x in b]


# ============================================================
# design_to_specs 映射
# ============================================================
def test_design_to_specs_maps_new_variables():
    d = {"range_km": 200.0, "cruise_speed_ms": 30.0, "payload_kg": 5.0,
         "endurance_h": 2.0, "altitude_m": 500.0, "aspect_ratio": 9.0,
         "taper_ratio": 0.5, "wing_sweep_deg": 10.0, "wing_dihedral_deg": 3.0,
         "wing_twist_deg": -1.0, "wing_x_fraction": 0.32,
         "htail_volume_coeff": 0.55, "vtail_volume_coeff": 0.04,
         "htail_aspect_ratio": 4.0, "htail_taper_ratio": 0.6, "htail_sweep_deg": 8.0,
         "vtail_aspect_ratio": 1.8, "vtail_taper_ratio": 0.6, "vtail_sweep_deg": 18.0,
         "airfoil_root": "naca2412", "airfoil_tip": "naca2412",
         "n_props": 2, "prop_layout": "wing", "prop_diameter_m": 0.5, "prop_rpm": 7000.0}
    specs = design_to_specs(d)
    g, p = specs["geometry"], specs["propulsion"]
    assert g["wing_sweep_deg"] == 10.0
    assert g["wing_dihedral_deg"] == 3.0
    assert g["wing_twist_deg"] == -1.0
    assert g["wing_x_fraction"] == 0.32
    assert g["htail_taper_ratio"] == 0.6
    assert g["vtail_sweep_deg"] == 18.0
    assert p["prop_diameter_m"] == 0.5
    assert p["prop_rpm"] == 7000.0


# ============================================================
# 几何管道贯通（新增变量流入标准几何对象）
# ============================================================
def test_geometry_plumbing_new_variables(demo_config):
    from src.geometry.aircraft import build_aircraft

    design = {
        "aspect_ratio": 8.0, "taper_ratio": 0.5,
        "airfoil_root": "clarky", "airfoil_tip": "clarky",
        "wing_sweep_deg": 12.0, "wing_dihedral_deg": 4.0,
        "wing_twist_deg": -1.5, "wing_x_fraction": 0.32,
        "htail_volume_coeff": 0.5, "htail_aspect_ratio": 4.0,
        "htail_taper_ratio": 0.6, "htail_sweep_deg": 8.0,
        "vtail_volume_coeff": 0.04, "vtail_aspect_ratio": 1.8,
        "vtail_taper_ratio": 0.6, "vtail_sweep_deg": 18.0,
        "n_props": 2, "prop_layout": "wing",
        "prop_diameter_m": 0.5, "prop_rpm": 7000.0,
    }
    mission = MissionSpec(range_km=200.0, cruise_speed_ms=30.0,
                          payload_kg=5.0, endurance_h=2.0, altitude_m=500.0)
    ac = build_aircraft(mission, design, demo_config["sizing"])
    assert ac.wing.sweep_quarter_chord_deg == pytest.approx(12.0)
    assert ac.wing.dihedral_deg == pytest.approx(4.0)
    assert ac.wing.twist_tip_deg == pytest.approx(-1.5)
    assert ac.htail.taper_ratio == pytest.approx(0.6)
    assert ac.htail.sweep_quarter_chord_deg == pytest.approx(8.0)
    assert ac.vtail.taper_ratio == pytest.approx(0.6)
    assert ac.vtail.sweep_quarter_chord_deg == pytest.approx(18.0)
    assert ac.propulsion.propellers[0].diameter_m == pytest.approx(0.5)
    assert ac.propulsion.propellers[0].rpm == pytest.approx(7000.0)


def test_root_tip_chord_are_derived():
    """根弦/梢弦为派生量：由面积+展弦比+锥比确定（不独立采样）。"""
    from src.core.contracts import WingParams
    w = WingParams(span_m=3.0, area_m2=1.2, aspect_ratio=7.5, taper_ratio=0.5)
    # cr = 2·c̄/(1+λ), c̄ = S/b
    cbar = 1.2 / 3.0
    assert w.root_chord_m == pytest.approx(2 * cbar / 1.5)
    assert w.tip_chord_m == pytest.approx(w.root_chord_m * 0.5)


def test_airfoil_whitelist_exists(uiuc_dir):
    """白名单内翼型必须真实存在于 UIUC 库。"""
    from src.airfoil.uiuc_database import AirfoilDatabase
    db = AirfoilDatabase(str(uiuc_dir))
    space = default_design_space()
    for d in space.discrete:
        if d.name in ("airfoil_root", "airfoil_tip"):
            for name in d.choices:
                assert db.exists(name), f"白名单翼型不在库中: {name}"


# ---------------- 辅助 ----------------
def _root_choices(space: DesignSpace) -> list:
    return next(d.choices for d in space.discrete if d.name == "airfoil_root")


def _tip_choices(space: DesignSpace) -> list:
    return next(d.choices for d in space.discrete if d.name == "airfoil_tip")
