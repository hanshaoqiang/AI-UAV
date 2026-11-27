"""Phase 2.5 阶段C1 测试：预筛接入 / 拒绝记录 / 失败分类 / 稳定性标签 / 数据链路。

覆盖（区分单元 / 集成，不含真实外部工具调用）：
  - 预筛开关（enabled on/off）与 config 控制
  - 预筛拒绝样本记录完整性（design_id / 原始变量 / 原因 / 指标 / 阈值 / 时间）
  - 失败阶段分类（几何不可行 / 外部工具 / 气动分析 / 其他）
  - 稳定性标签入库读回（区分实际计算值与人为目标裕度）
  - 任务输入与预测输出不混淆（mission_range vs predicted_range vs margin）
  - 断点恢复幂等（预筛拒绝 INSERT OR REPLACE）
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.core.config import load_config
from src.core.contracts import (AeroResult, Aircraft, MissionSpec,
                                PerformanceResult)
from src.core.parallel_runner import (ParallelBatchRunner, classify_failure_str)
from src.database.sqlite_manager import SQLiteManager
from src.dataset.generator import DatasetGenerator, default_dataset_config


# ============================================================
# 模块级 worker（spawn 可 pickle）
# ============================================================
def _mock_good(config, mission, design_cfg):
    from src.geometry.aircraft import build_aircraft
    ac = build_aircraft(mission, design_cfg, config["sizing"])
    aero = AeroResult(CL_alpha_per_rad=5.0, CL_max=1.3, CD0=0.03, k_induced=0.07,
                      CD0_parasitic=0.02, LD_max=15.0, Cm_alpha_per_rad=-0.5,
                      Cm_alpha_pre_trim_per_rad=-1.2,
                      neutral_point_x_pre_trim_m=0.9, cg_x_pre_trim_m=0.7,
                      cg_shift_m=0.02, static_margin_target=0.10,
                      reference_x_m=ac.cg_x_m)
    perf = PerformanceResult(
        v_stall_ms=15.0, v_cruise_ms=mission.cruise_speed_ms,
        CL_cruise=0.5, CD_cruise=0.05, LD_cruise=10.0, LD_max=15.0,
        thrust_weight_ratio=0.4, power_available_w=2000.0, excess_power_w=500.0,
        motor_efficiency=0.85, propeller_efficiency=0.7,
        range_km=mission.range_km * 1.1, endurance_h=mission.endurance_h * 1.1,
        range_margin_km=mission.range_km * 0.1,
        climb_rate_ms=3.0, static_margin=0.10, static_margin_pre_trim=0.24,
        neutral_point_x_m=0.9, cg_x_m=0.7, cg_x_pre_trim_m=0.7,
        cg_shift_m=0.02, static_margin_target=0.10,
        htail_volume_coeff=ac.htail_volume_coeff,
        vtail_volume_coeff=ac.vtail_volume_coeff)
    return {"run_id": f"mock_{mission.mission_name}_{mission.range_km:.0f}",
            "run_dir": "", "aircraft": ac, "aero": aero,
            "airfoil_polar": None, "performance": perf, "report": "",
            "vsp3": "", "stl": None, "db_saved": False, "elapsed_s": 0.01}


def _geom_fail(config, mission, design_cfg):
    raise ValueError("几何校验未通过: ['翼展>0']")


def _aero_fail(config, mission, design_cfg):
    raise RuntimeError("VSPAero 极线文件未生成: foo.polar")


def _ext_fail(config, mission, design_cfg):
    raise RuntimeError("vsp3 未生成: foo.vsp3")


def _mixed_worker(config, mission, design_cfg):
    """按 mission_name 分派到不同失败阶段（模块级，spawn 可 pickle）。"""
    name = mission.mission_name
    if name == "g":
        return _geom_fail(config, mission, design_cfg)
    if name == "a":
        return _aero_fail(config, mission, design_cfg)
    return _ext_fail(config, mission, design_cfg)


# ============================================================
# fixtures / helpers
# ============================================================
def _make_config(tmp_path: Path) -> dict:
    cfg = copy.deepcopy(load_config("config/mission.yaml"))
    cfg["paths"]["db_path"] = str(tmp_path / "test.sqlite")
    cfg["paths"]["data_dir"] = str(tmp_path / "results")
    cfg["tools"]["openvsp_script"] = str(tmp_path / "fake_vspscript.exe")
    cfg["tools"]["xfoil"] = str(tmp_path / "fake_xfoil.exe")
    return cfg


def _feasible_design() -> dict:
    return {
        "range_km": 100.0, "cruise_speed_ms": 25.0, "payload_kg": 2.0,
        "endurance_h": 1.5, "altitude_m": 500.0,
        "aspect_ratio": 8.0, "taper_ratio": 0.5,
        "wing_sweep_deg": 2.0, "wing_dihedral_deg": 3.0, "wing_twist_deg": -2.0,
        "wing_x_fraction": 0.30,
        "htail_volume_coeff": 0.5, "htail_aspect_ratio": 4.0,
        "htail_taper_ratio": 0.6, "htail_sweep_deg": 5.0,
        "vtail_volume_coeff": 0.04, "vtail_aspect_ratio": 1.8,
        "vtail_taper_ratio": 0.6, "vtail_sweep_deg": 12.0,
        "airfoil_root": "clarky", "airfoil_tip": "clarky",
        "n_props": 1, "prop_layout": "nose",
        # Phase 2.5 BEMT：D=0.3 匹配本任务（≈4kg MTOW，25m/s）功率反推直径；
        # 旧值 D=0.5 桨盘偏大，BEMT 下巡航效率崩塌 → 推力不足。
        "prop_diameter_m": 0.3, "prop_rpm": 6000.0,
    }


def _thrust_reject_design() -> dict:
    """小直径 + 低转速 → 可用推力不足 → 硬约束拒绝。"""
    return dict(_feasible_design(), prop_diameter_m=0.2, prop_rpm=4000.0)


# ============================================================
# C1.1 预筛接入 + 拒绝记录
# ============================================================
def test_prefilter_and_build_tasks_splits(tmp_path):
    """预筛应把可行/不可行设计拆分，拒绝样本记录完整字段。"""
    gen = DatasetGenerator(_make_config(tmp_path))
    designs = [_feasible_design(), _thrust_reject_design()]
    tasks, rejections = gen._prefilter_and_build_tasks(designs, "ds_t", enabled=True)

    assert len(tasks) == 1
    assert tasks[0]["design_id"] == "ds_t_00000"
    assert len(rejections) == 1
    r = rejections[0]
    assert r["design_id"] == "ds_t_00001"
    assert r["dataset_id"] == "ds_t"
    assert r["generated_at"]                      # 样本生成时间
    assert r["design_vars"]["prop_rpm"] == 4000.0   # 原始设计变量
    names = [f["name"] for f in r["failure_reasons"]]
    assert "推力不足" in names                     # 失败原因
    assert any("value" in f and "limit" in f for f in r["failure_reasons"])
    assert r["thresholds"]["hard"]["thrust_margin_min"] == 1.0   # 阈值
    assert r["metrics"]["thrust_available_n"] < r["metrics"]["thrust_required_n"]


def test_prefilter_disabled_passes_all(tmp_path):
    """关闭预筛时全部设计进入任务，无拒绝。"""
    gen = DatasetGenerator(_make_config(tmp_path))
    designs = [_feasible_design(), _thrust_reject_design()]
    tasks, rejections = gen._prefilter_and_build_tasks(designs, "ds_t", enabled=False)
    assert len(tasks) == 2
    assert rejections == []


def test_prefilter_config_switch():
    """配置文件控制预筛开关，且 generate() 显式参数可覆盖。"""
    cfg = default_dataset_config()
    assert "prefilter" in cfg
    assert cfg["prefilter"]["enabled"] is True     # 正式流程默认开启
    gen = DatasetGenerator(load_config("config/mission.yaml"))
    assert gen._prefilter_enabled(None) is True
    assert gen._prefilter_enabled(False) is False
    assert gen._prefilter_enabled(True) is True


def test_generate_records_rejections_to_db_and_json(tmp_path):
    """端到端：预筛开启 + mock worker，拒绝样本写库 + 导出 JSON，summary 统计正确。"""
    cfg = _make_config(tmp_path)
    gen = DatasetGenerator(cfg, worker_fn=_mock_good, max_retries=0)
    out = tmp_path / "out"
    summary = gen.generate(6, method="lhs", seed=42, dataset_id="ds_pre",
                           output_dir=out, formats=["json"], resume=False,
                           prefilter_enabled=True)

    pre = summary["prefilter"]
    assert pre["enabled"] is True
    assert pre["n_sampled"] == 6
    assert pre["n_rejected"] + pre["n_passed"] == 6
    assert pre["n_passed"] == summary["stats"]["total"]

    # SQLite 读回拒绝记录
    db = SQLiteManager(cfg["paths"]["db_path"])
    try:
        n_rej = db.count_prefilter_rejections("ds_pre")
        assert n_rej == pre["n_rejected"]
        rejs = db.fetch_prefilter_rejections("ds_pre")
        if rejs:
            r0 = rejs[0]
            assert r0["design_id"].startswith("ds_pre_")
            assert isinstance(r0["design_vars"], dict)
            assert isinstance(r0["failure_reasons"], list)
            assert r0["failure_reasons"]  # 至少一条失败原因
    finally:
        db.close()

    # JSON 导出存在
    if pre["n_rejected"] > 0:
        assert (out / "ds_pre_prefilter_rejected.json").exists()


# ============================================================
# C1.1 失败阶段分类
# ============================================================
def test_classify_failure_str():
    assert classify_failure_str("ValueError: 几何校验未通过: ['翼展>0']") == "geometry_infeasible"
    assert classify_failure_str("ValueError: 重量迭代发散: MTOW=9999") == "geometry_infeasible"
    assert classify_failure_str("RuntimeError: VSPAero 极线文件未生成") == "aero_failed"
    assert classify_failure_str("RuntimeError: vsp3 未生成: x.vsp3") == "external_tool_failed"
    assert classify_failure_str("RuntimeError: vspscript 超时") == "external_tool_failed"
    assert classify_failure_str("SomethingElse blew up") == "other_failed"


def test_failure_classification_in_stats(tmp_path):
    """几何/气动/外部工具失败应分别计入对应分类统计。"""
    cfg = _make_config(tmp_path)
    tasks = [
        {"design_id": "d_geom", "mission": MissionSpec(mission_name="g"),
         "design_cfg": None},
        {"design_id": "d_aero", "mission": MissionSpec(mission_name="a"),
         "design_cfg": None},
        {"design_id": "d_ext", "mission": MissionSpec(mission_name="e"),
         "design_cfg": None},
    ]
    # 三个 worker 分别失败于不同阶段（单 worker 串行执行保证顺序无关）
    runner = ParallelBatchRunner(cfg, max_workers=1, max_retries=0,
                                 worker_fn=_mixed_worker)
    stats = runner.run(tasks, resume=False, collect_metadata=False)
    assert stats["failed"] == 3
    assert stats["failed_geometry_infeasible"] == 1
    assert stats["failed_aero_failed"] == 1
    assert stats["failed_external_tool_failed"] == 1


# ============================================================
# C1.3 稳定性标签入库读回（区分实际计算值与人为目标）
# ============================================================
def test_stability_labels_roundtrip_and_not_confounded(tmp_path):
    from src.dataset.metadata import build_metadata
    cfg = _make_config(tmp_path)
    db = SQLiteManager(cfg["paths"]["db_path"])
    try:
        run_id = "run_stab"
        aero = AeroResult(
            CL_alpha_per_rad=5.0, Cm_alpha_per_rad=-0.5,
            Cm_alpha_pre_trim_per_rad=-1.2,
            neutral_point_x_pre_trim_m=0.9, cg_x_pre_trim_m=0.7,
            cg_shift_m=0.02, static_margin_target=0.10).to_dict()
        perf = PerformanceResult(
            static_margin=0.10, static_margin_pre_trim=0.24,
            neutral_point_x_m=0.9, cg_x_m=0.7,
            neutral_point_x_pre_trim_m=0.9, cg_x_pre_trim_m=0.7,
            cg_shift_m=0.02, static_margin_target=0.10).to_dict()
        db.start_run(run_id)
        db.save_run(run_id,
                    {"mission_name": "t", "range_km": 100.0},
                    {"wing": {"span_m": 3.0}, "mtow_kg": 10.0},
                    {"n_props": 1}, aero, perf)
        db.finish_run(run_id, 1.0, "success")
        db.save_metadata(build_metadata(run_id, dataset_id="ds_s", random_seed=1,
                                        sample_method="lhs"))

        samples = db.fetch_dataset_samples("ds_s")
        assert len(samples) == 1
        p = samples[0]["performance"]
        a = samples[0]["aerodynamics"]
        # 实际计算值（配平前）确实入库
        assert p["static_margin_pre_trim"] == pytest.approx(0.24)
        assert a["Cm_alpha_pre_trim_per_rad"] == pytest.approx(-1.2)
        # 人为目标裕度单独字段，不冒充实际值
        assert p["static_margin_target"] == pytest.approx(0.10)
        # 配平后 static_margin ≠ 配平前 static_margin_pre_trim（不混淆）
        assert p["static_margin"] != p["static_margin_pre_trim"]
    finally:
        db.close()


# ============================================================
# C1.5 标签质量：任务输入与预测输出不混淆
# ============================================================
def test_mission_vs_predicted_range_not_confounded(tmp_path):
    from src.dataset.metadata import build_metadata
    cfg = _make_config(tmp_path)
    db = SQLiteManager(cfg["paths"]["db_path"])
    try:
        run_id = "run_rng"
        db.start_run(run_id)
        db.save_run(run_id,
                    {"mission_name": "t", "range_km": 100.0},
                    {"wing": {"span_m": 3.0}, "mtow_kg": 10.0},
                    {"n_props": 1},
                    {"LD_max": 15.0},
                    {"range_km": 110.0, "range_margin_km": 10.0})
        db.finish_run(run_id, 1.0, "success")
        db.save_metadata(build_metadata(run_id, dataset_id="ds_r2", random_seed=1,
                                        sample_method="lhs", extra={"design_id": "d1"}))
        s = db.fetch_dataset_samples("ds_r2")[0]
        assert s["mission_range_km"] == 100.0       # 任务输入
        assert s["predicted_range_km"] == 110.0     # 模型预测
        assert s["range_margin_km"] == 10.0         # 裕度
        assert s["mission_range_km"] != s["predicted_range_km"]
    finally:
        db.close()


# ============================================================
# C1.6 断点恢复幂等：预筛拒绝 INSERT OR REPLACE
# ============================================================
def test_prefilter_rejection_idempotent(tmp_path):
    cfg = _make_config(tmp_path)
    db = SQLiteManager(cfg["paths"]["db_path"])
    try:
        args = ("d_x", "ds", "2026-11-25T00:00:00",
                {"aspect_ratio": 8.0}, [{"name": "桨尖速度", "detail": "230"}],
                {"tip_speed_max_ms": 230.0}, {"hard": {"tip_speed_max_ms": 220.0}})
        db.record_prefilter_rejection(*args)
        db.record_prefilter_rejection(*args)   # 重复记录（模拟断点恢复重跑）
        assert db.count_prefilter_rejections("ds") == 1
    finally:
        db.close()
