#!/usr/bin/env python
"""Phase 2.5 阶段C1 执行脚本：100 组 LHS 试验数据生成 + 预筛误杀验证 + 质量分析。

用法：
  python scripts/run_c1.py [--n 100] [--seed 42] [--workers 8] [--dataset-id c1_100_lhs]

流程：
  1) 预筛接入后，LHS 采样 100 组 → 预筛 → 真实 OpenVSP/XFOIL/VSPAero 完整分析（多进程 + 断点恢复）
  2) 从预筛拒绝样本中抽取 ≥10 组跑完整分析，做误杀验证（C1.2）
  3) 五类数据质量分析（C1.5）→ 生成 Markdown 报告 + JSON 结果

真实外部工具调用；Windows spawn 安全（worker 均为模块级函数）。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import load_config
from src.core.contracts import MissionSpec
from src.core.logger import setup_logger
from src.core.parallel_runner import classify_failure_str
from src.database.sqlite_manager import SQLiteManager
from src.dataset.generator import DatasetGenerator
from src.sampling.design_sampler import (DesignSampler, default_design_space,
                                         design_to_specs, specs_to_mission_dict)
from src.validation.validator import DataValidator


def build_mission_and_cfg(design: dict) -> tuple[MissionSpec, dict]:
    specs = design_to_specs(design)
    mission = MissionSpec.from_dict(specs_to_mission_dict(specs["mission"]))
    design_cfg = dict(specs["geometry"])
    design_cfg.update(specs["propulsion"])
    return mission, design_cfg


def run_misclassification_check(config: dict, rejections: list[dict],
                                n_check: int = 10) -> dict:
    """C1.2：对预筛拒绝样本抽样跑完整分析，统计误杀率。"""
    from src.core.workflow import DesignPipeline

    if not rejections:
        return {"n_rejected": 0, "n_checked": 0, "note": "无预筛拒绝样本，跳过误杀验证"}

    sample = rejections[:n_check]
    validator = DataValidator()
    # 误杀检查用独立数据目录 + 关闭入库，避免污染正式数据集
    cfg_mc = copy.deepcopy(config)
    cfg_mc["paths"]["data_dir"] = str(
        Path(config["paths"]["data_dir"]) / "_c1_mischeck")
    pipeline = DesignPipeline(cfg_mc, enable_db=False)

    confirmed_infeasible = 0
    confirmed_feasible = 0
    details = []
    for r in sample:
        design_id = r["design_id"]
        pre_reasons = [f.get("name") for f in r.get("failure_reasons") or []]
        try:
            mission, design_cfg = build_mission_and_cfg(r["design_vars"])
        except Exception as e:
            confirmed_infeasible += 1
            details.append({"design_id": design_id,
                            "prefilter_reasons": pre_reasons,
                            "full_analysis": f"设计转换失败: {e}"})
            continue
        try:
            result = pipeline.run(mission, design_cfg=design_cfg)
            val = validator.validate_result(result, mission=mission)
            if val.valid:
                confirmed_feasible += 1
                details.append({"design_id": design_id,
                                "prefilter_reasons": pre_reasons,
                                "full_analysis": "可行（校验通过，误杀）"})
            else:
                confirmed_infeasible += 1
                errs = val.failed_names("error")
                details.append({"design_id": design_id,
                                "prefilter_reasons": pre_reasons,
                                "full_analysis": f"不可行（校验拒绝: {errs}）"})
        except Exception as e:
            confirmed_infeasible += 1
            cat = classify_failure_str(f"{type(e).__name__}: {e}")
            details.append({"design_id": design_id,
                            "prefilter_reasons": pre_reasons,
                            "full_analysis": f"不可行（{cat}: {e}）"})

    n_checked = len(sample)
    return {
        "n_rejected": len(rejections),
        "n_checked": n_checked,
        "n_confirmed_infeasible": confirmed_infeasible,
        "n_misclassified": confirmed_feasible,
        "misclassification_rate": round(confirmed_feasible / n_checked, 4)
        if n_checked else 0.0,
        "details": details,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dataset-id", default="c1_100_lhs")
    ap.add_argument("--config", default=str(ROOT / "config" / "mission.yaml"))
    ap.add_argument("--n-mischeck", type=int, default=10)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--report-dir", default=str(ROOT / "reports" / "c1"))
    args = ap.parse_args()

    setup_logger()
    config = load_config(args.config)
    dataset_id = args.dataset_id
    output_dir = args.output_dir or str(ROOT / "data" / "datasets")
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"Phase 2.5 C1：生成 {args.n} 组 LHS 试验数据（预筛开启，真实工具链路）")
    print("=" * 70)

    # ---- 1) 采样原始设计（供 A 采样质量分析；与 generate 同 seed 确定性一致）----
    space = default_design_space()
    raw_designs = DesignSampler(space, seed=args.seed).lhs_sample(args.n)

    # ---- 2) 正式生成（预筛接入 + 多进程 + 断点恢复）----
    gen = DatasetGenerator(config, max_retries=2)
    t0 = time.time()
    summary = gen.generate(
        args.n, method="lhs", seed=args.seed, dataset_id=dataset_id,
        output_dir=output_dir, max_workers=args.workers,
        resume=True, prefilter_enabled=True)
    print(f"[生成完成] 用时 {time.time() - t0:.1f}s")
    print(f"  stats: {json.dumps(summary['stats'], ensure_ascii=False)}")
    print(f"  prefilter: n_sampled={summary['prefilter']['n_sampled']}, "
          f"n_rejected={summary['prefilter']['n_rejected']}, "
          f"n_passed={summary['prefilter']['n_passed']}")

    # ---- 3) 读回数据 ----
    db = SQLiteManager(config["paths"]["db_path"])
    samples = db.fetch_dataset_samples(dataset_id)
    rejections = db.fetch_prefilter_rejections(dataset_id)
    db.close()

    # 失败日志
    failures = []
    fp = Path(output_dir) / f"{dataset_id}_failures.json"
    if fp.exists():
        failures = json.loads(fp.read_text(encoding="utf-8"))

    # ---- 4) 预筛误杀验证（C1.2）----
    print(f"\n[误杀验证] 预筛拒绝 {len(rejections)} 组，抽样复核 {min(len(rejections), args.n_mischeck)} 组")
    misclassification = run_misclassification_check(
        config, rejections, n_check=args.n_mischeck)
    print(f"  确认不可行={misclassification.get('n_confirmed_infeasible')}, "
          f"确认可行(误杀)={misclassification.get('n_misclassified')}, "
          f"误杀率={misclassification.get('misclassification_rate', 0) * 100:.1f}%")

    # ---- 5) 质量分析（C1.5）----
    from src.analysis.quality import DataQualityAnalyzer, build_quality_report

    analyzer = DataQualityAnalyzer(
        samples=samples, prefilter_rejections=rejections,
        failures=failures, gen_summary=summary,
        misclassification=misclassification)
    analysis = {
        "sampling_quality": analyzer.sampling_quality(raw_designs),
        "screening_quality": analyzer.screening_quality(),
        "physical_reasonableness": analyzer.physical_reasonableness(),
        "label_quality": analyzer.label_quality(),
        "database_quality": analyzer.database_quality(
            db_path=config["paths"]["db_path"],
            state_file=Path(output_dir) / f"{dataset_id}_state.json"),
    }

    # ---- 6) 输出报告 + JSON ----
    report_path = report_dir / f"{dataset_id}_quality_report.md"
    build_quality_report(analysis, dataset_id, out_path=report_path)

    result_json = report_dir / f"{dataset_id}_analysis.json"
    result_json.write_text(
        json.dumps({"summary": summary, "misclassification": misclassification,
                    "analysis": analysis}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    print(f"\n[完成] 报告: {report_path}")
    print(f"[完成] 分析 JSON: {result_json}")
    print(f"[完成] 有效样本: {len(samples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
