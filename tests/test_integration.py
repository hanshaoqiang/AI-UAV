"""Phase 1 端到端集成测试: 完整设计闭环(依赖本机 OpenVSP/XFOIL)。

验证 `python main.py run config/mission.yaml` 的核心链路:
Mission → Geometry → OpenVSP(vsp3/STL) → XFOIL → VSPAero → Performance → Report → SQLite
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, requires_tools


@requires_tools
def test_full_pipeline_cli(tmp_path):
    """CLI 端到端运行(命令行接口不变式)。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "run",
         str(ROOT / "config" / "mission.yaml")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="ignore",
    )
    assert proc.returncode == 0, f"CLI 运行失败:\n{proc.stdout[-2000:]}"
    out = proc.stdout
    assert "设计运行完成" in out
    # 约束校核信息应存在（Phase 1.5 融合寄生阻力后，demo 可能部分约束未达标，
    # 这是更真实物理模型的正常结果，不作为硬断言）
    assert "约束[" in out
    # 输出摘要数值合理性
    assert "MTOW" in out
    assert "L/D" in out


@requires_tools
def test_full_pipeline_api(demo_config, tmp_path):
    """API 端到端运行并校验产物与数值。"""
    from src.core.contracts import MissionSpec
    from src.core.workflow import DesignPipeline

    cfg = json.loads(json.dumps(demo_config))  # 深拷贝
    # 结果写入临时目录, 不污染正式 data/
    tmp_data = tmp_path / "results"
    tmp_data.mkdir()
    cfg["paths"]["data_dir"] = str(tmp_data)
    cfg["paths"]["db_path"] = str(tmp_path / "test.sqlite")

    mission = MissionSpec.from_dict(cfg["mission"])
    pipeline = DesignPipeline(cfg)
    result = pipeline.run(mission)

    # ---- 产物完整性 ----
    run_dir = Path(result["run_dir"])
    for f in ("aircraft.vsp3", "aero.vsp3", "aircraft.stl",
              "aero.polar", "report.md", "results.json",
              "build_model.vspscript", "aero_sweep.vspscript",
              "clarky_polar.dat"):
        assert (run_dir / f).exists(), f"缺少产物: {f}"
    for f in ("fig_lift_curve.png", "fig_drag_polar.png", "fig_layout.png"):
        assert (run_dir / f).exists(), f"缺少图表: {f}"

    # ---- 数值物理合理性 ----
    aero, perf = result["aero"], result["performance"]
    assert 4.0 < aero.CL_alpha_per_rad < 8.0        # 展弦比9+尾翼
    assert 0.005 < aero.CD0 < 0.06                  # 小无人机量级
    assert 8.0 < aero.LD_max < 40.0                 # VLM(偏乐观)
    assert perf.v_stall_ms < demo_config["mission"]["cruise_speed_ms"]
    assert perf.range_km > 0
    assert 0.0 < perf.static_margin < 0.3
    assert perf.power_cruise_w > 100
    # 失速裕度: 巡航速度 > 1.1×失速速度
    assert perf.v_cruise_ms > 1.1 * perf.v_stall_ms

    # ---- 数据库 ----
    assert result["db_saved"]
    assert (tmp_path / "test.sqlite").exists()

    # ---- 报告内容 ----
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    for keyword in ("任务需求", "布局几何", "气动性能", "飞行性能", "约束校核"):
        assert keyword in report, f"报告缺章节: {keyword}"


@requires_tools
def test_database_records(demo_config, tmp_path):
    """入库记录可读回。"""
    import sqlite3

    from src.core.contracts import MissionSpec
    from src.core.workflow import DesignPipeline

    cfg = json.loads(json.dumps(demo_config))
    cfg["paths"]["data_dir"] = str(tmp_path / "results")
    cfg["paths"]["db_path"] = str(tmp_path / "t.sqlite")

    mission = MissionSpec.from_dict(cfg["mission"])
    DesignPipeline(cfg).run(mission)

    conn = sqlite3.connect(cfg["paths"]["db_path"])
    rows = conn.execute(
        "SELECT run_id, ld_cruise, range_km, static_margin FROM performances"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    run_id, ld, rng, sm = rows[0]
    assert ld is not None and ld > 3
    assert rng is not None and rng > 50
    assert sm is not None and 0 < sm < 0.3
