#!/usr/bin/env python
"""Phase 2.5 阶段C2 执行脚本：万级数据生成 + 质量分析。

用法：
  python scripts/run_c2.py [--n-tasks 10000] [--seed 42] [--workers 16] [--dataset-id c2_10000_lhs]

流程（推进模型已升级为 BEMT，预筛/完整分析均用 BEMT）：
  1) LHS 采样 N_sample 组（按保守预筛通过率 0.58 反推，保证预筛后 ≥ n_tasks）
  2) 预筛（BEMT 双约束可用推力）→ 通过的设计进入完整分析
  3) 完整分析（OpenVSP/XFOIL/VSPAero + BEMT，多进程 + 断点恢复）
  4) 万级数据质量分析（A 采样 / B 筛选 / C 物理合理性 / D 标签 / E 数据库）→ 报告

注意：--n-tasks 控制的是「进入完整分析的任务数」（预筛通过数），
最终有效样本数 = n_tasks × 完整分析有效率（约 72%），通常低于 n_tasks。

真实外部工具调用；Windows spawn 安全。
"""
from __future__ import annotations

import os

# 防 OpenBLAS 多线程在 Windows 上的偶发段错误（scipy-openblas NO_AFFINITY 线程池竞争，
# 已实测导致预筛阶段 SIGSEGV/exit 139）。必须在导入 numpy 之前设置。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import faulthandler

# 崩溃时把 Python 堆栈打到 stderr（配合后台日志定位偶发原生崩溃位置）
faulthandler.enable()

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import load_config
from src.core.logger import setup_logger
from src.database.sqlite_manager import SQLiteManager
from src.dataset.generator import DatasetGenerator
from src.sampling.design_sampler import DesignSampler, default_design_space


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-tasks", "--n-valid", dest="n_tasks", type=int, default=10000,
                    help="目标进入完整分析的任务数（= 预筛通过数）。注意：最终有效样本数"
                         "通常低于此值（受完整分析有效率约 72% 影响）。--n-valid 为历史别名。")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--dataset-id", default="c2_10000_lhs")
    ap.add_argument("--config", default=str(ROOT / "config" / "mission.yaml"))
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--report-dir", default=str(ROOT / "reports" / "c2"))
    args = ap.parse_args()

    setup_logger()
    config = load_config(args.config)
    dataset_id = args.dataset_id
    output_dir = args.output_dir or str(ROOT / "data" / "datasets")
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    # 保守预筛通过率（实测 62%，留缓冲用 0.58），反推采样数
    pass_rate_est = 0.58
    n_sample = int(math.ceil(args.n_tasks / pass_rate_est))

    print("=" * 72)
    print(f"Phase 2.5 C2：生成 ~{args.n_tasks} 组进入完整分析的任务（BEMT 推进，采样 {n_sample} 组）")
    print(f"  prop_efficiency={config['sizing'].get('prop_efficiency')}, "
          f"power_margin_factor={config['sizing'].get('power_margin_factor')}")
    print("=" * 72)

    # ---- 1) 采样原始设计（供 A 采样质量分析；与 generate 同 seed 确定性一致）----
    space = default_design_space()
    raw_designs = DesignSampler(space, seed=args.seed).lhs_sample(n_sample)

    # ---- 2) 正式生成（预筛接入 + 多进程 + 断点恢复）----
    gen = DatasetGenerator(config, max_retries=2)
    t0 = time.time()
    summary = gen.generate(
        n_sample, method="lhs", seed=args.seed, dataset_id=dataset_id,
        output_dir=output_dir, max_workers=args.workers,
        resume=True, prefilter_enabled=True)
    print(f"[生成完成] 用时 {time.time() - t0:.1f}s")
    print(f"  stats: {json.dumps(summary['stats'], ensure_ascii=False)}")
    pf = summary["prefilter"]
    print(f"  prefilter: n_sampled={pf['n_sampled']}, n_rejected={pf['n_rejected']}, "
          f"n_passed={pf['n_passed']}")

    # ---- 3) 读回数据 ----
    db = SQLiteManager(config["paths"]["db_path"])
    samples = db.fetch_dataset_samples(dataset_id)
    rejections = db.fetch_prefilter_rejections(dataset_id)
    db.close()

    failures = []
    fp = Path(output_dir) / f"{dataset_id}_failures.json"
    if fp.exists():
        failures = json.loads(fp.read_text(encoding="utf-8"))

    # ---- 4) 万级质量分析 ----
    from src.analysis.quality import DataQualityAnalyzer, build_quality_report

    analyzer = DataQualityAnalyzer(
        samples=samples, prefilter_rejections=rejections,
        failures=failures, gen_summary=summary,
        misclassification=None)   # C2 不做误杀验证（C1 已通过）
    analysis = {
        "sampling_quality": analyzer.sampling_quality(raw_designs),
        "screening_quality": analyzer.screening_quality(),
        "physical_reasonableness": analyzer.physical_reasonableness(),
        "label_quality": analyzer.label_quality(),
        "database_quality": analyzer.database_quality(
            db_path=config["paths"]["db_path"],
            state_file=Path(output_dir) / f"{dataset_id}_state.json"),
    }

    # ---- 5) 输出报告 + JSON ----
    report_path = report_dir / f"{dataset_id}_quality_report.md"
    build_quality_report(analysis, dataset_id, out_path=report_path)
    result_json = report_dir / f"{dataset_id}_analysis.json"
    result_json.write_text(
        json.dumps({"summary": summary, "analysis": analysis},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    print(f"\n[完成] 报告: {report_path}")
    print(f"[完成] 分析 JSON: {result_json}")
    print(f"[完成] 有效样本: {len(samples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
