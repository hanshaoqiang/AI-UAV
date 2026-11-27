#!/usr/bin/env python
"""Phase 2.5 BEMT 校准验证：小规模试点 + 预筛误杀/误放验证。

用法：
  python scripts/verify_bemt_calibration.py [--n 100] [--seed 42] [--n-check 10]

流程（真实外部工具链路，先验证再 C2 的门控步骤）：
  1) LHS 采样 N 组 → 预筛 → 统计通过率 + 拒绝原因分布
  2) 误杀验证：预筛「拒绝」样本抽样跑完整分析，统计误杀率（拒绝但完整分析可行）
  3) 误放验证：预筛「通过」样本抽样跑完整分析，统计误放率（通过但完整分析不可行）
  4) 输出 JSON + 打印摘要

Windows spawn 安全（worker 均为模块级函数）。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import load_config
from src.core.contracts import MissionSpec
from src.core.logger import setup_logger
from src.core.parallel_runner import classify_failure_str
from src.core.workflow import DesignPipeline
from src.sampling.design_sampler import (DesignSampler, default_design_space,
                                         design_to_specs, specs_to_mission_dict)
from src.validation.prefilter import FeasibilityPrefilter
from src.validation.validator import DataValidator


def build_mission_and_cfg(design: dict) -> tuple[MissionSpec, dict]:
    specs = design_to_specs(design)
    mission = MissionSpec.from_dict(specs_to_mission_dict(specs["mission"]))
    design_cfg = dict(specs["geometry"])
    design_cfg.update(specs["propulsion"])
    return mission, design_cfg


def full_analysis(config: dict, design_vars: dict) -> tuple[str, str]:
    """对单个设计跑完整分析，返回 (状态, 说明)。状态 ∈ {feasible, infeasible, error}。"""
    from src.validation.validator import DataValidator
    validator = DataValidator()
    try:
        mission, design_cfg = build_mission_and_cfg(design_vars)
    except Exception as e:   # noqa: BLE001
        return "infeasible", f"设计转换失败: {e}"
    try:
        pipeline = DesignPipeline(config, enable_db=False)
        result = pipeline.run(mission, design_cfg=design_cfg)
        val = validator.validate_result(result, mission=mission)
        if val.valid:
            return "feasible", "可行（校验通过）"
        return "infeasible", f"校验拒绝: {val.failed_names('error')}"
    except Exception as e:   # noqa: BLE001
        cat = classify_failure_str(f"{type(e).__name__}: {e}")
        return "error", f"完整分析失败({cat}): {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-check", type=int, default=10)
    ap.add_argument("--config", default=str(ROOT / "config" / "mission.yaml"))
    ap.add_argument("--out", default=str(ROOT / "reports" / "bemt_calibration_verify.json"))
    args = ap.parse_args()

    setup_logger()
    config = load_config(args.config)
    sizing = config["sizing"]

    print("=" * 72)
    print(f"Phase 2.5 BEMT 校准验证：{args.n} 组 LHS 试点 + 误杀/误放验证")
    print(f"  prop_efficiency={sizing.get('prop_efficiency')}, "
          f"power_margin_factor={sizing.get('power_margin_factor')}")
    print("=" * 72)

    # ---- 1) 采样 + 预筛 ----
    space = default_design_space()
    designs = DesignSampler(space, seed=args.seed).lhs_sample(args.n)
    pre = FeasibilityPrefilter(sizing)
    passed, rejected = [], []
    reason_counter = Counter()
    t0 = time.time()
    for d in designs:
        mission, dc = build_mission_and_cfg(d)
        res = pre.check(mission, dc)
        if res.feasible:
            passed.append(d)
        else:
            rejected.append(d)
            for f in res.hard_failures:
                reason_counter[f["name"]] += 1
    dt = time.time() - t0
    pass_rate = len(passed) / args.n
    print(f"\n[预筛] {len(passed)}/{args.n} 通过 = {pass_rate * 100:.1f}%  "
          f"（单组 {dt / args.n * 1000:.1f}ms）")
    print(f"  拒绝原因分布：{dict(reason_counter)}")

    # ---- 2) 误杀验证（拒绝 → 完整分析）----
    n_rej_check = min(len(rejected), args.n_check)
    print(f"\n[误杀验证] 对 {n_rej_check} 个拒绝样本跑完整分析...")
    misclassified = []
    for d in rejected[:n_rej_check]:
        status, note = full_analysis(config, d)
        if status == "feasible":
            misclassified.append({"design": d, "note": note})
    miskill_rate = len(misclassified) / n_rej_check if n_rej_check else 0.0
    print(f"  误杀（拒绝但完整分析可行）：{len(misclassified)}/{n_rej_check} "
          f"= {miskill_rate * 100:.1f}%")

    # ---- 3) 误放验证（通过 → 完整分析）----
    n_pass_check = min(len(passed), args.n_check)
    print(f"\n[误放验证] 对 {n_pass_check} 个通过样本跑完整分析...")
    miss_neg = []
    for d in passed[:n_pass_check]:
        status, note = full_analysis(config, d)
        if status != "feasible":
            miss_neg.append({"design": d, "note": note})
    miss_rate = len(miss_neg) / n_pass_check if n_pass_check else 0.0
    print(f"  误放（通过但完整分析不可行）：{len(miss_neg)}/{n_pass_check} "
          f"= {miss_rate * 100:.1f}%")

    # ---- 4) 输出 ----
    summary = {
        "prop_efficiency": sizing.get("prop_efficiency"),
        "power_margin_factor": sizing.get("power_margin_factor"),
        "n_sampled": args.n,
        "n_passed": len(passed),
        "n_rejected": len(rejected),
        "prefilter_pass_rate": round(pass_rate, 4),
        "rejection_reasons": dict(reason_counter),
        "prefilter_ms_per_design": round(dt / args.n * 1000, 1),
        "misclassification": {
            "n_checked": n_rej_check,
            "n_misclassified": len(misclassified),
            "misclassification_rate": round(miskill_rate, 4),
        },
        "false_negative": {
            "n_checked": n_pass_check,
            "n_false_negative": len(miss_neg),
            "false_negative_rate": round(miss_rate, 4),
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[输出] {out}")

    print("\n" + "=" * 72)
    print("校准结论")
    print("=" * 72)
    ok = pass_rate >= 0.4 and miskill_rate <= 0.2 and miss_rate <= 0.2
    print(f"  预筛通过率 {pass_rate * 100:.1f}% {'≥40% ✓' if pass_rate >= 0.4 else '<40% ✗'}")
    print(f"  误杀率 {miskill_rate * 100:.1f}% {'≤20% ✓' if miskill_rate <= 0.2 else '>20% ✗'}")
    print(f"  误放率 {miss_rate * 100:.1f}% {'≤20% ✓' if miss_rate <= 0.2 else '>20% ✗'}")
    print(f"  => {'校准通过，可进入 C2' if ok else '需进一步调整校准'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
