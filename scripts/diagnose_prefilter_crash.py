#!/usr/bin/env python
"""诊断预筛阶段段错误：faulthandler 捕获崩溃堆栈 + 每 500 组打印进度。

用法：python scripts/diagnose_prefilter_crash.py [n_sample] [seed]
崩溃时 faulthandler 会把 Python 堆栈打到 stderr，进度行定位到具体设计组。
"""
from __future__ import annotations

import faulthandler
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

faulthandler.enable()

from src.core.config import load_config
from src.core.contracts import MissionSpec
from src.sampling.design_sampler import (DesignSampler, default_design_space,
                                         design_to_specs, specs_to_mission_dict)
from src.validation.prefilter import FeasibilityPrefilter


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 17242
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42

    sizing = load_config("config/mission.yaml")["sizing"]
    pre = FeasibilityPrefilter(sizing)
    space = default_design_space()
    sampler = DesignSampler(space, seed=seed)
    designs = sampler.lhs_sample(n)

    t0 = time.time()
    passed = 0
    for i, d in enumerate(designs):
        if i % 500 == 0:
            print(f"[progress] {i}/{n} (用时 {time.time()-t0:.0f}s)", flush=True)
        specs = design_to_specs(d)
        mission = MissionSpec.from_dict(specs_to_mission_dict(specs["mission"]))
        dc = dict(specs["geometry"])
        dc.update(specs["propulsion"])
        res = pre.check(mission, dc)
        if res.feasible:
            passed += 1

    print(f"[done] {n} 组预筛完成，通过 {passed}，总用时 {time.time()-t0:.0f}s",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
