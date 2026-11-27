#!/usr/bin/env python
"""Phase 2.5 C2 完成脚本：读回数据 + 万级质量分析 + 报告（跳过预筛/生成）。

背景：C2 万级生成已在后台完成（数据在 SQLite），因 VSPAero 卡死 + SQLite
写锁导致 run_c2.py 主脚本在「读回数据」阶段被中断。本脚本仅做收尾——
用正确的**累计**统计（而非断点续算的 delta 统计）生成质量报告。

用法：python scripts/finish_c2_report.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import load_config
from src.database.sqlite_manager import SQLiteManager
from src.sampling.design_sampler import DesignSampler, default_design_space


def main() -> int:
    config = load_config("config/mission.yaml")
    dataset_id = "c2_10000_lhs"
    output_dir = ROOT / "data" / "datasets"
    report_dir = ROOT / "reports" / "c2"
    report_dir.mkdir(parents=True, exist_ok=True)

    n_sampled = 17242          # LHS 采样数（seed 42，与 run_c2.py 一致）
    n_passed = 10164           # 预筛通过数（确定性）
    n_prefilter_rejected = n_sampled - n_passed

    # 1) 读回数据
    db = SQLiteManager(config["paths"]["db_path"])
    samples = db.fetch_dataset_samples(dataset_id)
    rejections = db.fetch_prefilter_rejections(dataset_id)
    db.close()

    n_valid = len(samples)                              # 库中有效样本（成功）
    n_failed = 12                                       # VSPAero 失败（aero_failed）
    n_validation_rejected = n_passed - n_valid - n_failed

    # 2) 失败样本（12 个 aero_failed，design_id 从续算日志提取）
    failed_ids = ["01420", "03400", "04734", "05211", "06091", "09223",
                  "09586", "12399", "12855", "14943", "16428", "17240"]
    failures = [{"design_id": f"{dataset_id}_{i}", "status": "aero_failed",
                 "error": "VSPAero 极线文件未生成"} for i in failed_ids]

    # 3) 累计 summary（正确口径，非断点 delta）
    summary = {
        "dataset_id": dataset_id,
        "method": "lhs",
        "seed": 42,
        "n_requested": n_sampled,
        "prefilter": {
            "enabled": True,
            "n_sampled": n_sampled,
            "n_rejected": n_prefilter_rejected,
            "n_passed": n_passed,
        },
        "stats": {
            "total": n_passed,
            "success": n_valid,
            "failed": n_failed,
            "rejected": n_validation_rejected,
            "skipped": n_valid + n_validation_rejected,
            "pending": n_failed,
            "failed_geometry_infeasible": 0,
            "failed_aero_failed": n_failed,
            "failed_external_tool_failed": 0,
            "failed_other_failed": 0,
        },
    }

    # 4) 原始采样（供 A 采样质量分析）
    space = default_design_space()
    raw_designs = DesignSampler(space, seed=42).lhs_sample(n_sampled)

    # 5) 万级质量分析
    from src.analysis.quality import DataQualityAnalyzer, build_quality_report
    analyzer = DataQualityAnalyzer(
        samples=samples, prefilter_rejections=rejections,
        failures=failures, gen_summary=summary, misclassification=None)
    analysis = {
        "sampling_quality": analyzer.sampling_quality(raw_designs),
        "screening_quality": analyzer.screening_quality(),
        "physical_reasonableness": analyzer.physical_reasonableness(),
        "label_quality": analyzer.label_quality(),
        "database_quality": analyzer.database_quality(
            db_path=config["paths"]["db_path"],
            state_file=output_dir / f"{dataset_id}_state.json"),
    }

    # 6) 输出报告 + JSON
    report_path = report_dir / f"{dataset_id}_quality_report.md"
    build_quality_report(analysis, dataset_id, out_path=report_path)
    result_json = report_dir / f"{dataset_id}_analysis.json"
    result_json.write_text(
        json.dumps({"summary": summary, "analysis": analysis},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    print(f"[完成] 有效样本: {n_valid}")
    print(f"[完成] 预筛拒绝: {n_prefilter_rejected}（库中已记录 {len(rejections)} 条）")
    print(f"[完成] 校验拒绝: {n_validation_rejected}")
    print(f"[完成] 失败: {n_failed}")
    print(f"[完成] 报告: {report_path}")
    print(f"[完成] 分析 JSON: {result_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
