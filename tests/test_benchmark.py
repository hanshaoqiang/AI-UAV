"""Phase 2: BenchmarkRunner 测试。

覆盖：
  - 配置加载（YAML / 兜底）
  - 样本 → DataFrame 字段提取
  - 端到端基准（mock worker）统计 + 绘图 + 报告
  - SQLite 耗时查询
"""
import copy
import hashlib
import json
import os

import pytest

from src.benchmark.benchmark import (BenchmarkRunner, default_benchmark_config,
                                     load_benchmark_config, samples_to_dataframe)
from src.core.config import load_config
from src.core.contracts import AeroResult, PerformanceResult
from src.database.sqlite_manager import SQLiteManager
from src.geometry.aircraft import build_aircraft


# ============================================================
# mock worker（模块级，spawn 可导入）
# ============================================================
def _mock_task(config, mission, design_cfg):
    """构造伪造 pipeline 结果（纯几何 + 合理气动/性能，不跑 OpenVSP/XFOIL）。"""
    aircraft = build_aircraft(mission, design_cfg, config["sizing"])
    aero = AeroResult(CL_alpha_per_rad=5.0, CL_max=1.3, CD0=0.03, k_induced=0.07,
                      CD0_parasitic=0.02, LD_max=15.0, Cm_alpha_per_rad=-0.5,
                      reference_x_m=aircraft.cg_x_m)
    perf = PerformanceResult(
        v_stall_ms=15.0, v_cruise_ms=mission.cruise_speed_ms,
        CL_cruise=0.5, CD_cruise=0.05, LD_cruise=10.0, LD_max=15.0,
        thrust_weight_ratio=0.4, power_available_w=2000.0, excess_power_w=500.0,
        motor_efficiency=0.85, propeller_efficiency=0.7,
        range_km=mission.range_km * 1.05, endurance_h=mission.endurance_h * 1.05,
        climb_rate_ms=3.0, static_margin=0.10,
        neutral_point_x_m=aircraft.cg_x_m + 0.3, cg_x_m=aircraft.cg_x_m,
        htail_volume_coeff=aircraft.htail_volume_coeff,
        vtail_volume_coeff=aircraft.vtail_volume_coeff)
    key = json.dumps({"m": mission.to_dict(), "d": design_cfg},
                     sort_keys=True, default=str)
    run_id = "mock_" + hashlib.md5(key.encode()).hexdigest()[:10]
    return {"run_id": run_id, "run_dir": "", "aircraft": aircraft, "aero": aero,
            "airfoil_polar": None, "performance": perf, "report": "",
            "vsp3": "", "stl": None, "db_saved": False, "elapsed_s": 0.05}


def _always_fail(config, mission, design_cfg):
    raise RuntimeError("always fail")


def _make_test_config(tmp_path) -> dict:
    cfg = copy.deepcopy(load_config("config/mission.yaml"))
    cfg["paths"]["db_path"] = str(tmp_path / "test.sqlite")
    cfg["paths"]["data_dir"] = str(tmp_path / "results")
    cfg["tools"]["openvsp_script"] = str(tmp_path / "fake_vspscript.exe")
    cfg["tools"]["xfoil"] = str(tmp_path / "fake_xfoil.exe")
    return cfg


# ============================================================
# 配置
# ============================================================
def test_load_benchmark_config(tmp_path):
    p = tmp_path / "b.yaml"
    p.write_text("benchmark:\n  n: 500\n  method: sobol\n", encoding="utf-8")
    cfg = load_benchmark_config(p)
    assert cfg["n"] == 500
    assert cfg["method"] == "sobol"


def test_default_benchmark_config():
    cfg = default_benchmark_config()
    assert cfg["n"] >= 100
    assert cfg["method"] in ("random", "lhs", "sobol")


# ============================================================
# 样本 → DataFrame
# ============================================================
def test_samples_to_dataframe():
    sample = {
        "mission": {"range_km": 200.0, "cruise_speed_ms": 30.0, "payload_kg": 5.0,
                    "endurance_h": 2.0, "altitude_m": 500.0},
        "aircraft": {
            "wing": {"aspect_ratio": 8.0, "taper_ratio": 0.5,
                     "sweep_quarter_chord_deg": 5.0, "dihedral_deg": 3.0,
                     "twist_tip_deg": -2.0},
            "propulsion": {"n_props": 2, "prop_diameter_m": 0.5,
                           "propellers": [{"diameter_m": 0.5, "rpm": 6000.0}]},
        },
        "performance": {"range_km": 210.0, "endurance_h": 2.1, "LD_max": 15.0,
                        "LD_cruise": 10.0, "static_margin": 0.1,
                        "climb_rate_ms": 3.0, "power_cruise_w": 1000.0,
                        "v_stall_ms": 15.0, "v_cruise_ms": 30.0,
                        "propeller_efficiency": 0.7, "htail_volume_coeff": 0.5,
                        "vtail_volume_coeff": 0.04},
    }
    df = samples_to_dataframe([sample])
    assert df.loc[0, "range_km_req"] == 200.0
    assert df.loc[0, "aspect_ratio"] == 8.0
    assert df.loc[0, "prop_rpm"] == 6000.0
    assert df.loc[0, "n_props"] == 2
    assert df.loc[0, "LD_max"] == 15.0
    assert df.loc[0, "htail_volume_coeff"] == 0.5


def test_samples_to_dataframe_empty():
    df = samples_to_dataframe([])
    assert df.empty


# ============================================================
# SQLite 耗时查询
# ============================================================
def test_fetch_dataset_elapsed(tmp_path):
    from src.dataset.metadata import build_metadata
    cfg = _make_test_config(tmp_path)
    db = SQLiteManager(cfg["paths"]["db_path"])
    try:
        for i, rid in enumerate(["r1", "r2"]):
            db.start_run(rid)
            db.finish_run(rid, 1.5 + i, "success")
            db.save_metadata(build_metadata(rid, dataset_id="ds_e", random_seed=1,
                                            sample_method="lhs"))
        db.start_run("r3")
        db.finish_run("r3", 9.9, "failed")
        db.save_metadata(build_metadata("r3", dataset_id="ds_e", random_seed=1,
                                        sample_method="lhs"))
        elapsed = db.fetch_dataset_elapsed("ds_e")
        assert sorted(elapsed) == [1.5, 2.5]  # 仅成功任务
    finally:
        db.close()


# ============================================================
# 端到端基准（mock worker）
# ============================================================
def test_benchmark_run_end_to_end(tmp_path):
    cfg = _make_test_config(tmp_path)
    runner = BenchmarkRunner(
        cfg, benchmark_cfg={"min_corr_samples": 3},
        worker_fn=_mock_task, log_level=40)
    summary = runner.run(n=6, method="lhs", seed=3, dataset_id="bench_t",
                         output_dir=tmp_path / "out", report_dir=tmp_path / "report",
                         resume=False, prefilter_enabled=False)

    b = summary["benchmark"]
    assert b["n_requested"] == 6
    assert b["n_valid_samples"] == 6
    assert b["success_rate"] == 1.0
    assert b["avg_elapsed_s"] > 0
    assert b["db_size_mb"] >= 0

    # 报告 + 图文件存在
    assert os.path.exists(summary["report"])
    assert os.path.exists(summary["figures"]["param_dist"])
    assert os.path.exists(summary["figures"]["perf_dist"])
    assert os.path.exists(summary["figures"]["correlation"])  # min_corr=3 < 6

    # 报告内容
    text = open(summary["report"], encoding="utf-8").read()
    assert "Benchmark Report" in text
    assert "成功率" in text
    assert "CPU 利用率" in text


def test_benchmark_no_samples_skips_plots(tmp_path):
    """无有效样本时应跳过绘图且不报错。"""
    cfg = _make_test_config(tmp_path)
    runner = BenchmarkRunner(cfg, benchmark_cfg={"min_corr_samples": 3},
                             worker_fn=_always_fail, log_level=40)
    summary = runner.run(n=2, method="lhs", seed=3, dataset_id="bench_empty",
                         output_dir=tmp_path / "out", report_dir=tmp_path / "report",
                         resume=False, prefilter_enabled=False)
    assert summary["benchmark"]["n_valid_samples"] == 0
    assert summary["benchmark"]["success_rate"] == 0.0
    assert summary["figures"] == {}
    assert os.path.exists(summary["report"])
