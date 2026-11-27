"""Phase 2: DatasetGenerator 测试。

覆盖：
  - 配置加载（YAML / 兜底）
  - 三种采样方法（random / lhs / sobol）
  - 任务组装
  - 端到端生成（mock worker，避免真实 OpenVSP/XFOIL）
  - 校验过滤（写库前拒绝 error 级样本）
  - 导出 YAML / JSON + SQLite 读回
"""
import copy
import hashlib
import json

import pytest
import yaml

from src.core.config import load_config
from src.core.contracts import AeroResult, PerformanceResult
from src.dataset.generator import (DatasetGenerator, default_dataset_config,
                                   load_dataset_config)
from src.database.sqlite_manager import SQLiteManager
from src.geometry.aircraft import build_aircraft


# ============================================================
# mock worker（模块级，spawn 可导入）
# ============================================================
def _mock_good_task(config, mission, design_cfg):
    return _build_mock_result(config, mission, design_cfg, bad=False)


def _mock_bad_task(config, mission, design_cfg):
    return _build_mock_result(config, mission, design_cfg, bad=True)


def _build_mock_result(config, mission, design_cfg, bad: bool) -> dict:
    """构造伪造 pipeline 结果（纯几何 + 合理气动/性能，不跑 OpenVSP/XFOIL）。"""
    aircraft = build_aircraft(mission, design_cfg, config["sizing"])
    aero = AeroResult(
        CL_alpha_per_rad=5.0, CL_alpha_0deg=0.3, CL_max=1.3,
        CD0=0.03, k_induced=0.07, CD0_parasitic=0.02,
        LD_max=15.0, alpha_LDmax_deg=5.0, Cm_alpha_per_rad=-0.5,
        reference_x_m=aircraft.cg_x_m)
    if bad:
        # NaN → sanity 校验 error → 应被过滤
        perf = PerformanceResult(
            v_stall_ms=float("nan"), v_cruise_ms=mission.cruise_speed_ms,
            LD_cruise=10.0, LD_max=15.0, thrust_weight_ratio=0.4,
            power_available_w=2000.0, excess_power_w=500.0,
            motor_efficiency=0.85, propeller_efficiency=0.7,
            range_km=float("nan"), endurance_h=float("nan"),
            climb_rate_ms=3.0, static_margin=float("nan"),
            neutral_point_x_m=aircraft.cg_x_m + 0.3, cg_x_m=aircraft.cg_x_m)
    else:
        perf = PerformanceResult(
            v_stall_ms=15.0, v_cruise_ms=mission.cruise_speed_ms,
            CL_cruise=0.5, CD_cruise=0.05, LD_cruise=10.0, LD_max=15.0,
            thrust_weight_ratio=0.4, power_available_w=2000.0,
            excess_power_w=500.0, motor_efficiency=0.85,
            propeller_efficiency=0.7, range_km=mission.range_km * 1.05,
            endurance_h=mission.endurance_h * 1.05, climb_rate_ms=3.0,
            static_margin=0.10, neutral_point_x_m=aircraft.cg_x_m + 0.3,
            cg_x_m=aircraft.cg_x_m,
            htail_volume_coeff=aircraft.htail_volume_coeff,
            vtail_volume_coeff=aircraft.vtail_volume_coeff)
    key = json.dumps({"m": mission.to_dict(), "d": design_cfg},
                     sort_keys=True, default=str)
    run_id = "mock_" + hashlib.md5(key.encode()).hexdigest()[:10]
    return {"run_id": run_id, "run_dir": "", "aircraft": aircraft, "aero": aero,
            "airfoil_polar": None, "performance": perf, "report": "",
            "vsp3": "", "stl": None, "db_saved": False, "elapsed_s": 0.01}


# ============================================================
# fixtures / helpers
# ============================================================
def _make_test_config(tmp_path) -> dict:
    """深拷贝配置并把 db/数据/工具路径指向临时目录（隔离真实环境）。"""
    cfg = copy.deepcopy(load_config("config/mission.yaml"))
    cfg["paths"]["db_path"] = str(tmp_path / "test.sqlite")
    cfg["paths"]["data_dir"] = str(tmp_path / "results")
    cfg["tools"]["openvsp_script"] = str(tmp_path / "fake_vspscript.exe")
    cfg["tools"]["xfoil"] = str(tmp_path / "fake_xfoil.exe")
    return cfg


# ============================================================
# 配置加载
# ============================================================
def test_load_dataset_config(tmp_path):
    p = tmp_path / "ds.yaml"
    p.write_text("dataset:\n  method: sobol\n  seed: 7\n", encoding="utf-8")
    cfg = load_dataset_config(p)
    assert cfg["method"] == "sobol"
    assert cfg["seed"] == 7


def test_default_dataset_config():
    cfg = default_dataset_config()
    assert cfg["method"] in ("random", "lhs", "sobol")
    assert "formats" in cfg


def test_load_dataset_config_missing():
    with pytest.raises(FileNotFoundError):
        load_dataset_config("no_such_file.yaml")


# ============================================================
# 采样 + 任务组装
# ============================================================
def test_sample_methods(tmp_path):
    cfg = _make_test_config(tmp_path)
    gen = DatasetGenerator(cfg)
    for method in ("random", "lhs", "sobol"):
        designs = gen._sample(8, method, seed=42)
        assert len(designs) == 8
        assert all("aspect_ratio" in d for d in designs)


def test_sample_invalid_method(tmp_path):
    gen = DatasetGenerator(_make_test_config(tmp_path))
    with pytest.raises(ValueError):
        gen.generate(3, method="bogus")


def test_build_tasks(tmp_path):
    gen = DatasetGenerator(_make_test_config(tmp_path))
    designs = gen._sample(4, "lhs", seed=1)
    tasks = gen._build_tasks(designs, "ds_x")
    assert len(tasks) == 4
    assert tasks[0]["design_id"] == "ds_x_00000"
    assert tasks[3]["design_id"] == "ds_x_00003"
    assert tasks[0]["mission"].range_km == pytest.approx(designs[0]["range_km"])
    assert "aspect_ratio" in tasks[0]["design_cfg"]
    assert "n_props" in tasks[0]["design_cfg"]


# ============================================================
# 端到端生成（mock worker）
# ============================================================
def test_generate_good_mock(tmp_path):
    cfg = _make_test_config(tmp_path)
    out = tmp_path / "out"
    gen = DatasetGenerator(cfg, worker_fn=_mock_good_task, max_retries=0)
    summary = gen.generate(4, method="lhs", seed=5, dataset_id="ds_good",
                           output_dir=out, formats=["yaml", "json", "csv"], resume=False,
                           prefilter_enabled=False)
    assert summary["stats"]["success"] == 4
    assert summary["stats"]["rejected"] == 0
    assert summary["stats"]["failed"] == 0
    assert summary["outputs"]["n_samples"] == 4
    assert summary["prefilter"]["enabled"] is False

    # 导出文件存在
    yaml_path = summary["outputs"]["yaml"]
    json_path = summary["outputs"]["json"]
    assert (tmp_path / "out" / "ds_good.yaml").exists()
    assert (tmp_path / "out" / "ds_good.json").exists()

    # 内容校验
    data = yaml.safe_load(open(yaml_path, encoding="utf-8"))
    assert data["dataset"]["dataset_id"] == "ds_good"
    assert data["dataset"]["n_samples"] == 4
    assert len(data["samples"]) == 4
    s0 = data["samples"][0]
    assert "mission" in s0 and "aircraft" in s0 and "performance" in s0
    assert "run_id" in s0 and "design_id" in s0

    jdata = json.load(open(json_path, encoding="utf-8"))
    assert jdata["dataset"]["n_samples"] == 4
    assert len(jdata["samples"]) == 4

    # CSV 导出（Phase 2.5 C3 D6）：含 design_id/run_id 表头
    import csv
    csv_path = summary["outputs"]["csv"]
    assert (tmp_path / "out" / "ds_good.csv").exists()
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    assert "design_id" in rows[0] and "run_id" in rows[0]


def test_generate_bad_filtered(tmp_path):
    cfg = _make_test_config(tmp_path)
    gen = DatasetGenerator(cfg, worker_fn=_mock_bad_task, max_retries=0)
    summary = gen.generate(3, method="lhs", seed=5, dataset_id="ds_bad",
                           output_dir=tmp_path / "out", formats=["json"],
                           resume=False, prefilter_enabled=False)
    # NaN 样本被校验拒绝，不入库
    assert summary["stats"]["success"] == 0
    assert summary["stats"]["rejected"] == 3
    assert summary["stats"]["failed"] == 0
    assert summary["outputs"]["n_samples"] == 0


def test_generate_sobol(tmp_path):
    cfg = _make_test_config(tmp_path)
    gen = DatasetGenerator(cfg, worker_fn=_mock_good_task, max_retries=0)
    summary = gen.generate(4, method="sobol", seed=9, dataset_id="ds_sobol",
                           output_dir=tmp_path / "out", formats=["json"],
                           resume=False, prefilter_enabled=False)
    assert summary["method"] == "sobol"
    # sobol 采样可能产生边缘不可行设计（build_aircraft 内部几何校验拒绝 → failed），
    # 因此只断言：总任务守恒 + 至少 1 组成功 + 导出数与成功数一致。
    s = summary["stats"]
    assert s["total"] == 4
    assert s["success"] >= 1
    assert s["success"] + s["failed"] + s["rejected"] == 4
    assert summary["outputs"]["n_samples"] == s["success"]


# ============================================================
# SQLite 读回
# ============================================================
def test_fetch_dataset_samples_roundtrip(tmp_path):
    from src.dataset.metadata import build_metadata
    cfg = _make_test_config(tmp_path)
    db = SQLiteManager(cfg["paths"]["db_path"])
    try:
        run_id = "run_abc"
        db.start_run(run_id)
        db.save_run(run_id,
                    {"mission_name": "t", "range_km": 100.0},
                    {"wing": {"span_m": 3.0}, "mtow_kg": 10.0},
                    {"n_props": 1},
                    {"LD_max": 15.0},
                    {"range_km": 100.0})
        db.finish_run(run_id, 1.0, "success")
        db.save_metadata(build_metadata(
            run_id, dataset_id="ds_r", random_seed=1, sample_method="lhs",
            extra={"design_id": "d_1"}))
        samples = db.fetch_dataset_samples("ds_r")
        assert len(samples) == 1
        s = samples[0]
        assert s["design_id"] == "d_1"
        assert s["run_id"] == "run_abc"
        assert s["mission"]["range_km"] == 100.0
        assert s["performance"]["range_km"] == 100.0
        assert s["metadata"]["sample_method"] == "lhs"
    finally:
        db.close()


def test_fetch_dataset_samples_empty(tmp_path):
    cfg = _make_test_config(tmp_path)
    db = SQLiteManager(cfg["paths"]["db_path"])
    try:
        assert db.fetch_dataset_samples("nonexistent") == []
    finally:
        db.close()
