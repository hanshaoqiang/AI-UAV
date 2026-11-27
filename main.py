#!/usr/bin/env python
"""AI-UAV 无人机快速概念设计平台 — CLI 入口 (M01)。

用法:
  python main.py run config/mission.yaml          # 完整设计分析
  python main.py show config/mission.yaml         # 查看合并后配置
  python main.py airfoils [关键词]                # 查询 UIUC 翼型库
  python main.py sample [n] [--lhs]               # 采样 n 组设计(默认随机)
  python main.py batch [n] [--lhs] [--seed S]     # 批量生成并分析 n 组
"""
from __future__ import annotations

import sys
from pathlib import Path

# 项目根目录入 sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.airfoil.uiuc_database import AirfoilDatabase
from src.core.config import load_config
from src.core.contracts import MissionSpec
from src.core.logger import setup_logger
from src.core.workflow import DesignPipeline


def cmd_run(config_path: str) -> int:
    cfg = load_config(config_path)
    setup_logger()
    mission = MissionSpec.from_dict(cfg["mission"])
    pipeline = DesignPipeline(cfg)
    result = pipeline.run(mission)

    perf = result["performance"]
    print("\n" + "=" * 62)
    print(f"设计运行完成: {result['run_id']}  (用时 {result['elapsed_s']:.0f}s)")
    print("=" * 62)
    print(f"  MTOW        : {result['aircraft'].mtow_kg:8.2f} kg")
    print(f"  翼展/面积   : {result['aircraft'].wing.span_m:8.2f} m / "
          f"{result['aircraft'].wing.area_m2:.2f} m²")
    print(f"  L/D (max/cr): {perf.LD_max:8.1f} / {perf.LD_cruise:.1f}")
    print(f"  失速速度    : {perf.v_stall_ms:8.1f} m/s")
    print(f"  巡航功率    : {perf.power_cruise_w:8.0f} W")
    print(f"  航程        : {perf.range_km:8.0f} km")
    print(f"  续航        : {perf.endurance_h:8.1f} h")
    print(f"  静稳定裕度  : {perf.static_margin:8.3f}")
    print(f"  爬升率      : {perf.climb_rate_ms:8.1f} m/s")
    print("-" * 62)
    for name, c in perf.constraints_pass.items():
        flag = "通过" if c["pass"] else "未达"
        print(f"  约束[{name}] {flag}: {c['detail']}")
    print("-" * 62)
    print(f"  报告: {result['report']}")
    print(f"  模型: {result['vsp3']}")
    print(f"  目录: {result['run_dir']}")
    return 0


def cmd_show(config_path: str) -> int:
    import json
    cfg = load_config(config_path)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_airfoils(cfg: dict, keyword: str) -> int:
    db = AirfoilDatabase(cfg["tools"]["uiuc_db_dir"])
    names = db.list_names(keyword)
    print(f"共 {len(db._index)} 个翼型, 匹配 {len(names)} 个:")
    for n in names[:50]:
        print(f"  {n}")
    if len(names) > 50:
        print(f"  ... (其余 {len(names) - 50} 个省略)")
    return 0


def cmd_sample(n: int, use_lhs: bool, seed: int | None) -> int:
    """采样 n 组设计并打印预览。"""
    import yaml
    from src.sampling.design_sampler import (DesignSampler, default_design_space,
                                             design_to_specs)
    sampler = DesignSampler(default_design_space(), seed=seed)
    designs = sampler.lhs_sample(n) if use_lhs else sampler.random_sample(n)
    print(f"生成 {len(designs)} 组设计（{'LHS' if use_lhs else '随机'}采样, seed={seed}）:")
    for i, d in enumerate(designs):
        specs = design_to_specs(d)
        print(f"\n--- 设计 {i+1} ---")
        print(yaml.safe_dump(specs, allow_unicode=True, sort_keys=False))
    return 0


def cmd_batch(n: int, use_lhs: bool, seed: int | None, config_path: str) -> int:
    """批量生成并分析 n 组设计。"""
    from src.core.batch_runner import BatchRunner
    from src.database.sqlite_manager import SQLiteManager
    from src.sampling.design_sampler import (DesignSampler, default_design_space,
                                             design_to_specs, specs_to_mission_dict)

    cfg = load_config(config_path)
    setup_logger()
    sampler = DesignSampler(default_design_space(), seed=seed)
    designs = sampler.lhs_sample(n) if use_lhs else sampler.random_sample(n)

    # 组装任务
    tasks = []
    for i, d in enumerate(designs):
        specs = design_to_specs(d)
        mission = MissionSpec.from_dict(specs_to_mission_dict(specs["mission"]))
        design_cfg = specs["geometry"]
        design_cfg.update(specs["propulsion"])
        tasks.append({"design_id": f"sample_{i:04d}",
                      "mission": mission, "design_cfg": design_cfg})

    # 批处理
    pipeline = DesignPipeline(cfg)
    db = SQLiteManager(cfg["paths"]["db_path"])
    state_file = Path(cfg["paths"]["data_dir"]) / "batch_state.json"
    runner = BatchRunner(pipeline, db, max_retries=2, state_file=state_file)
    stats = runner.run(tasks, resume=True)

    print("\n" + "=" * 62)
    print("批量生成完成")
    print("=" * 62)
    print(f"  总计   : {stats['total']}")
    print(f"  成功   : {stats['success']}")
    print(f"  失败   : {stats['failed']}")
    print(f"  跳过   : {stats['skipped']}")
    print(f"  成功率 : {stats['success'] / max(stats['total'], 1) * 100:.1f}%")
    print(f"  用时   : {stats.get('elapsed_s', 0):.0f}s")
    print(f"  入库   : {db.count('run_history')} 条历史, "
          f"{db.count_success()} 成功")
    db.close()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cmd = args[0]
    if cmd == "run" and len(args) >= 2:
        return cmd_run(args[1])
    if cmd == "show" and len(args) >= 2:
        return cmd_show(args[1])
    if cmd == "airfoils":
        keyword = args[1] if len(args) > 1 else ""
        return cmd_airfoils(load_config(ROOT / "config" / "default.yaml"), keyword)
    if cmd == "sample":
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
        use_lhs = "--lhs" in args
        seed = None
        if "--seed" in args:
            seed = int(args[args.index("--seed") + 1])
        return cmd_sample(n, use_lhs, seed)
    if cmd == "batch":
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
        use_lhs = "--lhs" in args
        seed = None
        if "--seed" in args:
            seed = int(args[args.index("--seed") + 1])
        cfg_path = "config/mission.yaml"
        return cmd_batch(n, use_lhs, seed, cfg_path)
    print(f"未知命令: {cmd}\n{__doc__}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
