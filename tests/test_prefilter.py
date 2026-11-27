"""Phase 2.5 可行性预筛（Prefilter）测试。"""
import pytest

from src.core.config import load_config
from src.core.contracts import MissionSpec
from src.validation.prefilter import (FeasibilityPrefilter, PrefilterResult,
                                      default_prefilter_config, make_prefilter_check)


@pytest.fixture
def sizing():
    return load_config("config/mission.yaml")["sizing"]


@pytest.fixture
def mission():
    return MissionSpec(range_km=150.0, cruise_speed_ms=25.0, payload_kg=3.0,
                       endurance_h=1.5, altitude_m=500.0)


def _base_design() -> dict:
    return {
        "aspect_ratio": 8.0, "taper_ratio": 0.5,
        "airfoil_root": "clarky", "airfoil_tip": "clarky",
        "wing_sweep_deg": 2.0, "wing_dihedral_deg": 3.0, "wing_twist_deg": -2.0,
        "wing_x_fraction": 0.3,
        "htail_volume_coeff": 0.5, "htail_aspect_ratio": 4.0,
        "htail_taper_ratio": 0.6, "htail_sweep_deg": 5.0,
        "vtail_volume_coeff": 0.04, "vtail_aspect_ratio": 1.8,
        "vtail_taper_ratio": 0.6, "vtail_sweep_deg": 12.0,
        "n_props": 1, "prop_layout": "nose",
    }


def test_prefilter_config_loads():
    cfg = default_prefilter_config()
    assert "hard" in cfg and "soft" in cfg
    assert cfg["hard"]["thrust_margin_min"] > 0
    assert cfg["soft"]["tip_speed_warn_ms"] > 0
    assert cfg["soft"]["disk_loading_warn_w_m2"] > 0


def test_prefilter_feasible_design(sizing, mission):
    """可行带内的设计（桨盘匹配巡航速度）应通过预筛并记录指标。"""
    pre = FeasibilityPrefilter(sizing)
    # Phase 2.5 BEMT：D=0.3 匹配本任务（≈5.8kg MTOW，25m/s）功率反推直径；
    # 旧值 D=0.5 桨盘偏大，巡航效率崩塌 → 推力不足（BEMT 暴露的物理校准）。
    design = dict(_base_design(), prop_diameter_m=0.3, prop_rpm=6000.0)
    res = pre.check(mission, design)
    assert isinstance(res, PrefilterResult)
    assert res.feasible is True
    assert res.hard_failures == []
    assert res.metrics["mtow_kg"] > 0
    assert res.metrics["tip_speed_max_ms"] > 0
    assert res.metrics["thrust_available_n"] > 0


def test_prefilter_tip_speed_warns(sizing, mission):
    """桨尖速度偏高 → 软约束告警（C1.2 起降级，不硬过滤）。

    BEMT 下大直径（D=1.0）会使桨叶进入风车/低效区，同时触发「推力不足」硬约束；
    桨尖速度本身只告警、不作为硬约束。"""
    pre = FeasibilityPrefilter(sizing)
    design = dict(_base_design(), prop_diameter_m=1.0, prop_rpm=9000.0)
    res = pre.check(mission, design)
    names = [w["name"] for w in res.warnings]
    assert "桨尖速度偏高" in names
    # 桨尖速度不作为硬约束（硬过滤仅推力/推重比/几何重量）
    assert "桨尖速度" not in [f["name"] for f in res.hard_failures]


def test_prefilter_thrust_insufficient_reject(sizing, mission):
    """小直径 + 低转速 → 可用推力不足 → 硬约束过滤。"""
    pre = FeasibilityPrefilter(sizing)
    design = dict(_base_design(), prop_diameter_m=0.2, prop_rpm=4000.0)
    res = pre.check(mission, design)
    assert res.feasible is False
    names = [f["name"] for f in res.hard_failures]
    assert "推力不足" in names


def test_prefilter_disk_loading_warns(sizing, mission):
    """桨盘载荷偏高仅告警（C1.2 起降级为软约束，不硬过滤）。"""
    pre = FeasibilityPrefilter(sizing)
    design = dict(_base_design(), prop_diameter_m=0.5, prop_rpm=7000.0)
    res = pre.check(mission, design)
    # 桨盘载荷告警不应使设计被硬过滤（若同时无其他硬约束则可行）
    dl_warnings = [w for w in res.warnings if w["name"] == "桨盘载荷偏高"]
    assert dl_warnings or res.metrics.get("disk_loading_max_w_m2", 0) <= 4000.0
    # 硬过滤仅由推力/推重比/几何重量触发，不含桨盘载荷
    assert "桨盘载荷" not in [f["name"] for f in res.hard_failures]


def test_prefilter_weight_divergence(sizing):
    """超大载荷+续航 → 重量迭代发散 → 几何/重量硬约束过滤。"""
    pre = FeasibilityPrefilter(sizing)
    mission = MissionSpec(range_km=150.0, cruise_speed_ms=25.0, payload_kg=100.0,
                          endurance_h=5.0, altitude_m=500.0)
    design = dict(_base_design(), prop_diameter_m=0.5, prop_rpm=7000.0)
    res = pre.check(mission, design)
    assert res.feasible is False
    assert any(f["name"] == "几何/重量" for f in res.hard_failures)


def test_prefilter_records_reason_and_metrics(sizing, mission):
    """每个过滤样本应记录原因与具体指标。"""
    pre = FeasibilityPrefilter(sizing)
    # Phase 2.5 BEMT：D=0.7 对 3kg 任务偏大 → 推力不足但 T_avail 仍 > 0（非零指标）。
    # 旧值 D=0.2 使桨叶完全进入风车（T_avail=0），无法验证「记录非零指标」。
    design = dict(_base_design(), prop_diameter_m=0.7, prop_rpm=6000.0)
    res = pre.check(mission, design)
    assert res.feasible is False
    assert res.hard_failures[0]["name"] == "推力不足"
    assert "value" in res.hard_failures[0] and "limit" in res.hard_failures[0]
    assert res.metrics["thrust_available_n"] > 0
    assert res.metrics["thrust_required_n"] > 0


def test_make_prefilter_check_integrates_sampler(sizing):
    """预筛回调可接入 DesignSampler，随机采样应输出可行设计。"""
    from src.sampling.design_sampler import (DesignSampler, default_design_space)
    check = make_prefilter_check(sizing)
    space = default_design_space()
    sampler = DesignSampler(space, seed=9)
    designs = sampler.random_sample(5, feasibility_check=check, max_attempts=50)
    assert len(designs) == 5
    # 输出设计应全部通过预筛（若不通过则已重采样）
    for d in designs:
        assert check(d) is True
