"""Phase 2: ParallelBatchRunner 多进程批量运行测试。

覆盖：
  - 纯逻辑：max_workers 资源控制、断点恢复 state 读写、单写者入库
  - 多进程端到端：假任务函数（spawn），验证任务分配 + 结果收集 + 单写者 + 元数据
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
from src.core.parallel_runner import (ParallelBatchRunner, _default_max_workers,
                                      _parallel_worker)
from src.database.sqlite_manager import SQLiteManager


# ---------------- 模块级假任务函数（spawn 可 pickle） ----------------
def _fake_task_fn(config, mission, design_cfg):
    """模拟 pipeline.run 返回（不调用外部工具）。"""
    wing = WingParams(span_m=3.0, area_m2=1.2, aspect_ratio=7.5, taper_ratio=0.5)
    htail = TailParams(area_m2=0.24, aspect_ratio=4.0, arm_m=1.4)
    vtail = TailParams(area_m2=0.06, aspect_ratio=1.8, arm_m=1.4)
    fus = FuselageParams(length_m=2.0, max_diameter_m=0.35)
    ac = Aircraft(wing=wing, htail=htail, vtail=vtail, fuselage=fus,
                  mtow_kg=15.0, empty_mass_kg=8.0, payload_kg=2.0,
                  battery_mass_kg=5.0, cg_x_m=0.7)
    aero = AeroResult(CD0=0.03, k_induced=0.07, CL_alpha_per_rad=4.5,
                      CL_alpha_0deg=0.0, CL_max=1.1, LD_max=15.0,
                      Cm_alpha_per_rad=-0.5)
    perf = PerformanceResult(v_stall_ms=14.0, LD_cruise=11.0, LD_max=15.0,
                             thrust_available_n=25.0, thrust_weight_ratio=0.17,
                             excess_power_w=500.0, motor_efficiency=0.85,
                             propeller_efficiency=0.7, range_km=120.0,
                             endurance_h=2.2, static_margin=0.10,
                             neutral_point_x_m=0.76, cg_x_m=0.7)
    return {"run_id": f"mock_{mission.mission_name}", "aircraft": ac,
            "aero": aero, "performance": perf, "elapsed_s": 0.05}


def _fake_task_fn_failing(config, mission, design_cfg):
    raise RuntimeError("模拟失败")


def _fake_task_fn_hanging(config, mission, design_cfg):
    """模拟 worker 卡死（长时间不返回），用于验证无进展看门狗。"""
    import time
    time.sleep(3600)
    return {}


def _make_task(i: int) -> dict:
    mission = MissionSpec(mission_name=f"t{i}", range_km=100.0, cruise_speed_ms=30.0,
                          payload_kg=2.0, endurance_h=2.0, altitude_m=500.0)
    return {"design_id": f"d{i}", "mission": mission, "design_cfg": None}


def _make_config(tmp_path: Path) -> dict:
    return {
        "tools": {"uiuc_db_dir": str(ROOT / "uiuc" / "coord_seligFmt"),
                  "openvsp_script": "C:/OpenVSP-3.55.0-win64/vspscript.exe",
                  "xfoil": "C:/Xfoil/xfoil.exe"},
        "paths": {"db_path": str(tmp_path / "t.sqlite"),
                  "data_dir": str(tmp_path / "data")},
        "parallel": {"max_workers": 2},
    }


# ---------------- 纯逻辑测试 ----------------
def test_default_max_workers_uses_config():
    assert _default_max_workers({"parallel": {"max_workers": 2}}) == 2


def test_default_max_workers_default_is_conservative():
    import os
    n_cpu = os.cpu_count() or 4
    assert _default_max_workers({}) == min(8, n_cpu)


def test_default_max_workers_zero_means_auto():
    import os
    n_cpu = os.cpu_count() or 4
    assert _default_max_workers({"parallel": {"max_workers": 0}}) == min(8, n_cpu)


def test_default_max_workers_capped_by_cpu():
    import os
    n_cpu = os.cpu_count() or 4
    assert _default_max_workers({"parallel": {"max_workers": 999}}) == n_cpu


def test_state_roundtrip(tmp_path):
    sf = tmp_path / "state.json"
    runner = ParallelBatchRunner(_make_config(tmp_path), state_file=sf)
    # 初始无 state
    assert runner._load_completed() == {}
    # 保存后读回
    runner._save_state("b1", {"d1": "r1", "d2": "r2"})
    assert runner._load_completed() == {"d1": "r1", "d2": "r2"}


def test_persist_success_writes_db(tmp_path):
    from src.dataset.metadata import build_metadata  # noqa: F401
    config = _make_config(tmp_path)
    runner = ParallelBatchRunner(config)
    db = SQLiteManager(config["paths"]["db_path"])
    try:
        task = _make_task(0)
        result = _fake_task_fn(config, task["mission"], None)
        runner._persist_success(db, "d0", task["mission"], result, "b1",
                                "ds_x", 42, "lhs", collect_metadata=True)
        assert db.count("missions") == 1
        assert db.count("geometries") == 1
        assert db.count("performances") == 1
        assert db.count("run_history") == 1
        assert db.count_success() == 1
        # 元数据
        assert db.count_metadata("ds_x") == 1
        meta = db.get_metadata("mock_t0")
        assert meta is not None
        assert meta["random_seed"] == 42
        assert meta["sample_method"] == "lhs"
        assert meta["openvsp_version"] == "3.55.0"
    finally:
        db.close()


# ---------------- 多进程端到端 ----------------
def test_parallel_run_end_to_end(tmp_path):
    config = _make_config(tmp_path)
    tasks = [_make_task(i) for i in range(3)]
    runner = ParallelBatchRunner(config, max_workers=2, worker_fn=_fake_task_fn,
                                 state_file=tmp_path / "state.json")
    stats = runner.run(tasks, resume=False, dataset_id="ds_test",
                       random_seed=42, sample_method="lhs")
    assert stats["success"] == 3
    assert stats["failed"] == 0
    assert stats["total"] == 3
    # 单写者入库验证
    db = SQLiteManager(config["paths"]["db_path"])
    try:
        assert db.count("missions") == 3
        assert db.count("performances") == 3
        assert db.count_metadata("ds_test") == 3
        rows = db.list_metadata("ds_test")
        assert all(r["random_seed"] == 42 for r in rows)
        assert {r["sample_method"] for r in rows} == {"lhs"}
    finally:
        db.close()


def test_parallel_run_resume_skips_completed(tmp_path):
    config = _make_config(tmp_path)
    tasks = [_make_task(i) for i in range(3)]
    sf = tmp_path / "state.json"
    runner = ParallelBatchRunner(config, max_workers=2, worker_fn=_fake_task_fn,
                                 state_file=sf)
    # 第一次跑完 3 个
    stats1 = runner.run(tasks, resume=False, dataset_id="ds")
    assert stats1["success"] == 3
    # 第二次 resume：应全部跳过
    runner2 = ParallelBatchRunner(config, max_workers=2, worker_fn=_fake_task_fn,
                                  state_file=sf)
    stats2 = runner2.run(tasks, resume=True, dataset_id="ds")
    assert stats2["skipped"] == 3
    assert stats2["pending"] == 0
    assert stats2["success"] == 0


def test_parallel_run_failure_counted(tmp_path):
    config = _make_config(tmp_path)
    tasks = [_make_task(i) for i in range(2)]
    runner = ParallelBatchRunner(config, max_workers=2, max_retries=1,
                                 worker_fn=_fake_task_fn_failing)
    stats = runner.run(tasks, resume=False, collect_metadata=False)
    assert stats["failed"] == 2
    assert stats["success"] == 0


def test_parallel_run_stall_watchdog(tmp_path):
    """无进展看门狗：worker 卡死时主进程在阈值后强制终止，不静默死锁。

    Phase 2.5 C2 生产事故回归：VSPAero 卡死曾致整批死锁 8 小时；
    本测试验证 stall_timeout_s 触发后主进程及时终止，剩余任务标记 failed。
    """
    import time
    config = _make_config(tmp_path)
    tasks = [_make_task(i) for i in range(2)]
    runner = ParallelBatchRunner(config, max_workers=2,
                                 worker_fn=_fake_task_fn_hanging,
                                 stall_timeout_s=4)
    t0 = time.time()
    stats = runner.run(tasks, resume=False, collect_metadata=False)
    elapsed = time.time() - t0
    # 看门狗应及时触发（4s 阈值 + join 兜底 ≈ 15s），远小于卡死时长 3600s
    assert elapsed < 60, f"看门狗未及时触发，耗时 {elapsed:.1f}s"
    assert stats["success"] == 0
    assert stats["failed"] == 2
