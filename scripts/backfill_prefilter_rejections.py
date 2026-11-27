#!/usr/bin/env python
"""Phase 2.5 C3 D5：重放预筛补录缺失的预筛拒绝记录。

背景：C2 生产事故导致 7078 条预筛拒绝记录中仅 3612 条入库（缺 3466 条）。
预筛是纯 Python 确定性计算，可从原始采样（LHS seed=42）+ 预筛逻辑重放恢复，
无需调用外部工具。本脚本补录缺失记录（INSERT OR REPLACE 幂等，不覆盖既有记录）。

用法：python scripts/backfill_prefilter_rejections.py
"""
from __future__ import annotations

import os

# 防 OpenBLAS 多线程段错误（与 run_c2.py 一致）
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import load_config
from src.database.sqlite_manager import SQLiteManager
from src.dataset.generator import DatasetGenerator
from src.sampling.design_sampler import DesignSampler, default_design_space


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", default="c2_10000_lhs")
    ap.add_argument("--n-sample", type=int, default=17242)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--config", default=str(ROOT / "config" / "mission.yaml"))
    args = ap.parse_args()

    config = load_config(args.config)
    gen = DatasetGenerator(config, max_retries=2)

    # 1) 重放采样（与 C2 生成时 seed/n 完全一致）
    space = default_design_space()
    designs = DesignSampler(space, seed=args.seed).lhs_sample(args.n_sample)
    print(f"重放采样：{args.n_sample} 组（seed={args.seed}, lhs）")

    # 2) 重放预筛，收集拒绝记录
    generated_at = datetime.now().isoformat(timespec="seconds")
    rejections = []
    for i, d in enumerate(designs):
        design_id = f"{args.dataset_id}_{i:05d}"
        mission, design_cfg = gen._design_to_mission_cfg(d)
        res = gen.prefilter.check(mission, design_cfg)
        if not res.feasible:
            rejections.append({
                "design_id": design_id, "dataset_id": args.dataset_id,
                "generated_at": generated_at, "design_vars": d,
                "failure_reasons": res.hard_failures, "warnings": res.warnings,
                "metrics": res.metrics, "thresholds": gen.prefilter.cfg,
            })
    print(f"重放预筛：拒绝 {len(rejections)} 组，通过 {args.n_sample - len(rejections)} 组")

    # 3) 补录（INSERT OR REPLACE 幂等，批量单事务）
    db = SQLiteManager(config["paths"]["db_path"])
    before = db.count_prefilter_rejections(args.dataset_id)
    try:
        for r in rejections:
            db.record_prefilter_rejection(
                r["design_id"], r["dataset_id"], r["generated_at"],
                r["design_vars"], r["failure_reasons"], r["metrics"],
                r["thresholds"], r.get("warnings"), commit=False)
        db.commit()
        after = db.count_prefilter_rejections(args.dataset_id)
        print(f"补录完成：库中预筛拒绝记录 {before} → {after} 条")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
