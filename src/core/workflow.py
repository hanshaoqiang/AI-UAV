"""L2 Workflow: Pipeline 编排 — Mission → Geometry → OpenVSP → XFOIL → VSPAero
→ Performance → Report (→ Database)。

只依赖各模块的公开接口(契约)，不触碰任何模块内部实现。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from src.airfoil.uiuc_database import AirfoilDatabase
from src.core.contracts import (AeroResult, Aircraft, AirfoilPolar, MissionSpec,
                                PerformanceResult)
from src.core.logger import setup_logger
from src.database.sqlite_manager import SQLiteManager
from src.geometry.aircraft import build_aircraft
from src.openvsp.adapter import OpenVSPAdapter
from src.performance.evaluator import PerformanceEvaluator
from src.report.generator import ReportGenerator
from src.vspaero.adapter import VSPAeroAdapter
from src.xfoil.adapter import XFOILAdapter
from src.geometry.sizing import air_density

logger = logging.getLogger("uavdp.workflow")


class DesignPipeline:
    """Phase 1 设计分析闭环。"""

    def __init__(self, config: dict, enable_db: bool = True):
        self.cfg = config
        self.enable_db = enable_db          # False → 不写库（供并行 worker 单写者架构）
        tools = config["tools"]
        self.data_root = Path(config["paths"]["data_dir"])
        self.run_dir: Path | None = None
        self.airfoil_db = AirfoilDatabase(tools["uiuc_db_dir"])
        self.openvsp = OpenVSPAdapter(tools["openvsp_script"], self.airfoil_db)
        self.xfoil = XFOILAdapter(tools["xfoil"])
        self.vspaero = VSPAeroAdapter(tools["openvsp_script"])
        self.evaluator = PerformanceEvaluator(config["sizing"])
        self.reporter = ReportGenerator()
        self._db: SQLiteManager | None = None

    # ---------------- 主流程 ----------------
    def run(self, mission: MissionSpec, design_cfg: dict | None = None) -> dict:
        t0 = time.time()
        run_id = self._new_run_id(mission)
        self.run_dir = self.data_root / run_id
        setup_logger(self.run_dir)
        logger.info("===== 设计运行开始: %s =====", run_id)
        logger.info("任务: 航程%.0fkm 巡航%.0fm/s 载荷%.1fkg 续航%.1fh 高度%.0fm",
                    mission.range_km, mission.cruise_speed_ms, mission.payload_kg,
                    mission.endurance_h, mission.altitude_m)

        # 1) Mission → Geometry（design_cfg 可选覆盖，不改动 self.cfg 状态）
        design = self.cfg["design"]
        if design_cfg:
            design = {**design, **design_cfg}
        aircraft = build_aircraft(mission, design, self.cfg["sizing"])

        # 2) Aircraft → vsp3: 完整模型 + 气动模型(仅升力面)
        vsp3_path, aero_vsp3, stl_path = self.openvsp.build(aircraft, self.run_dir)

        # 3) XFOIL 翼型 2D 分析(主翼根弦, 巡航雷诺数)
        rho = air_density(mission.altitude_m)
        re_cruise = (rho * mission.cruise_speed_ms
                     * aircraft.wing.root_chord_m / 1.78e-5)
        af_path = self.airfoil_db.find_or_default(aircraft.wing.airfoil_root)
        acfg = self.cfg["analysis"]
        polar = self.xfoil.analyze(
            af_path, self.run_dir, re_cruise,
            alpha_start=acfg["xfoil_alpha_start"], alpha_step=acfg["xfoil_alpha_step"],
            alpha_end=acfg["xfoil_alpha_end"], max_iter=acfg["xfoil_max_iter"])

        # 4) vsp3(气动) → VSPAero 整机气动
        aero = self.vspaero.analyze(
            aero_vsp3, self.run_dir, self.cfg["analysis"],
            cg_x_m=aircraft.cg_x_m, wing_aspect_ratio=aircraft.wing.aspect_ratio)

        # 4a) 重心配平: 依据中性点将重心放到目标静稳定裕度(力矩线性转移, 免重跑)
        _trim_cg(aero, aircraft, mission)

        # 5) Performance
        perf = self.evaluator.evaluate(aero, polar, aircraft, mission)

        # 6) Report
        artifacts = {"vsp3": vsp3_path.name, "aero_vsp3": aero_vsp3.name,
                     "stl": stl_path.name if stl_path else None,
                     "polar": aero_vsp3.with_suffix(".polar").name}
        report_path = self.reporter.generate(
            mission, aircraft, aero, polar, perf, self.run_dir, run_id, artifacts)

        # 7) Database（enable_db=False 时跳过，由并行主进程单写者负责）
        db_saved = False
        if self.enable_db:
            try:
                self._db = SQLiteManager(self.cfg["paths"]["db_path"])
                self._db.start_run(run_id)
                self._db.save_run(run_id, mission.to_dict(), aircraft.to_dict(),
                                  aircraft.propulsion.to_dict(), aero.to_dict(),
                                  perf.to_dict())
                self._db.finish_run(run_id, time.time() - t0, "success")
                db_saved = True
            except Exception as e:  # 数据库失败不阻塞主流程
                logger.warning("入库失败(非致命): %s", e)

        dt = time.time() - t0
        logger.info("===== 设计运行完成: %s (用时 %.0fs) =====", run_id, dt)
        return {
            "run_id": run_id,
            "run_dir": str(self.run_dir),
            "aircraft": aircraft,
            "aero": aero,
            "airfoil_polar": polar,
            "performance": perf,
            "report": str(report_path),
            "vsp3": str(vsp3_path),
            "stl": str(stl_path) if stl_path else None,
            "db_saved": db_saved,
            "elapsed_s": dt,
        }

    def _new_run_id(self, mission: MissionSpec) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        key = json.dumps(mission.to_dict(), sort_keys=True).encode()
        digest = hashlib.md5(key).hexdigest()[:6]
        return f"{stamp}_{mission.mission_name}_{digest}"


def _trim_cg(aero: AeroResult, aircraft: Aircraft, mission: MissionSpec) -> None:
    """按目标静稳定裕度放置重心。

    力矩参考点转移是精确线性运算: Cm(new) = Cm(old) + CL·Δx/c̄,
    因此调整重心无需重跑 VSPAero。物理含义: 通过电池/载荷布置实现配平。

    Phase 2.5：配平前先保留原始气动稳定性信号（Cmα、中性点、原始重心、
    调整量、目标裕度），不被配平覆盖，供稳定性标签与 AI 训练溯源。
    """
    cbar = aircraft.wing.mean_chord_m
    cl_a = aero.CL_alpha_per_rad
    cm_a = aero.Cm_alpha_per_rad
    if cl_a == 0 or cm_a == 0:
        return
    sm_current = -cm_a / cl_a
    np_x = aircraft.cg_x_m + sm_current * cbar
    sm_target = 0.5 * (mission.static_margin_min + mission.static_margin_max)
    cg_new = np_x - sm_target * cbar
    shift = cg_new - aircraft.cg_x_m

    # 保留配平前原始信号（Phase 2.5）
    aero.Cm_alpha_pre_trim_per_rad = cm_a
    aero.neutral_point_x_pre_trim_m = np_x
    aero.cg_x_pre_trim_m = aircraft.cg_x_m
    aero.cg_shift_m = shift
    aero.static_margin_target = sm_target

    aero.Cm = [cm + cl * shift / cbar for cm, cl in zip(aero.Cm, aero.CL)]
    aero.Cm_alpha_per_rad = cm_a + cl_a * shift / cbar
    aero.reference_x_m = cg_new
    aircraft.cg_x_m = cg_new
    logger.info("重心配平: NP=%.3f m, SM %.3f→%.3f(目标), CG %.3f→%.3f m",
                np_x, sm_current, sm_target, aero.cg_x_pre_trim_m, cg_new)
