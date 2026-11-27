#!/usr/bin/env python
"""构建桨叶截面翼型（clarky）的 XFOIL 极线查找表，供 BEMT 求解器使用。

用法：
  python scripts/build_prop_polar.py [--airfoil clarky] [--out data/polars/clarky_polar.json]

流程：
  1) 对一组雷诺数（对数分布），用 XFOIL 计算稳定攻角段（-6°~14°）的 2D 极线；
  2) 对 XFOIL 未覆盖的攻角段（< -6° 线性外推、> 14° 失速后外推）补全到 -10°~25°；
  3) 某个雷诺数 XFOIL 失败时，用相邻雷诺数在 log10(Re) 空间插值降级；
  4) 输出规则网格查找表 JSON（reynolds × alpha_deg → CL/CD）。

说明：
  - 只跑一次（离线预计算），BEMT 运行时仅做插值，不再调用 XFOIL；
  - XFOIL 在低雷诺数/失速后不收敛属正常，脚本做稳健补全，保证网格完整。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.xfoil.adapter import XFOILAdapter

# 查找表网格（完整覆盖，含失速后）
ALPHA_START = -10.0
ALPHA_END = 25.0
ALPHA_STEP = 1.0
# XFOIL 实际扫描的稳定攻角段（太宽易在低 Re/失速后不收敛超时）
XFOIL_ALPHA_START = -6.0
XFOIL_ALPHA_END = 14.0
# 雷诺数（对数分布，覆盖 UAV 桨叶截面典型范围）
REYNOLDS = [5e4, 1e5, 2e5, 3e5, 5e5]


def _find_airfoil_dat(airfoil_name: str, uiuc_dir: str | Path) -> Path:
    d = Path(uiuc_dir)
    for cand in [d / f"{airfoil_name}.dat", d / f"{airfoil_name.lower()}.dat"]:
        if cand.exists():
            return cand
    hits = sorted(d.glob(f"{airfoil_name}*.dat"))
    if hits:
        return hits[0]
    raise FileNotFoundError(f"UIUC 库中找不到翼型: {airfoil_name}（目录 {d}）")


def _clean(alpha: list[float], cl: list[float], cd: list[float]):
    """排序去重 + 截断到失速点（CL 最大）。负攻角区的真实分离波动保留。"""
    dedup: dict[float, tuple[float, float, float]] = {}
    for a, c, d in zip(alpha, cl, cd):
        key = round(a, 2)
        dedup[key] = (a, c, d)
    items = sorted(dedup.values())
    a = [x[0] for x in items]
    cl_ = [x[1] for x in items]
    cd_ = [x[2] for x in items]
    if not a:
        return [], [], []
    i_max = max(range(len(cl_)), key=lambda i: cl_[i])
    return a[:i_max + 1], cl_[:i_max + 1], cd_[:i_max + 1]


def _resample(alpha_src, val_src, alpha_grid) -> list[float]:
    """在源攻角序列上线性插值（含边界外推）到规则网格。"""
    return [_lerp(alpha_src, val_src, a) for a in alpha_grid]


def _lerp(xs, ys, x) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    if n == 1:
        return float(ys[0])
    if x <= xs[0]:
        return float(ys[0] + (ys[1] - ys[0]) * (x - xs[0]) / (xs[1] - xs[0]))
    if x >= xs[-1]:
        return float(ys[-1] + (ys[-1] - ys[-2]) * (x - xs[-1]) / (xs[-1] - xs[-2]))
    for i in range(n - 1):
        if xs[i] <= x <= xs[i + 1]:
            return float(ys[i] + (ys[i + 1] - ys[i]) * (x - xs[i]) / (xs[i + 1] - xs[i]))
    return float(ys[-1])


def _extend_to_grid(alpha, cl, cd, alpha_grid) -> tuple[list[float], list[float]]:
    """把截断到失速点的序列重采样到完整网格，失速后外推 Cl 缓降 / Cd 缓升。"""
    if len(alpha) < 2:
        return [0.0] * len(alpha_grid), [0.02] * len(alpha_grid)
    # 失速后外推（经验斜率，失速点之后 Cl 缓降、Cd 缓升）
    slope_cl_post = -0.03    # /deg
    slope_cd_post = 0.02     # /deg
    a_ext = list(alpha)
    cl_ext = list(cl)
    cd_ext = list(cd)
    while a_ext[-1] < ALPHA_END - 1e-6:
        a_ext.append(a_ext[-1] + ALPHA_STEP)
        cl_ext.append(max(cl_ext[-1] + slope_cl_post * ALPHA_STEP, 0.05))
        cd_ext.append(max(cd_ext[-1] + slope_cd_post * ALPHA_STEP, 0.02))
    cl_grid = [max(_lerp(a_ext, cl_ext, ag), -2.0) for ag in alpha_grid]
    cd_grid = [max(_lerp(a_ext, cd_ext, ag), 1e-4) for ag in alpha_grid]
    return cl_grid, cd_grid


def build_polar(airfoil_name, xfoil_exe, uiuc_dir, work_dir,
                reynolds) -> dict:
    dat = _find_airfoil_dat(airfoil_name, uiuc_dir)
    adapter = XFOILAdapter(xfoil_exe, timeout_s=60)
    alpha_grid = [round(ALPHA_START + i * ALPHA_STEP, 4)
                  for i in range(int(round((ALPHA_END - ALPHA_START) / ALPHA_STEP)) + 1)]

    # 逐 Re 计算；失败记录，之后用相邻 Re 插值
    rows = {}   # re -> (cl_row, cd_row)
    for re in reynolds:
        polar = adapter.analyze(dat, work_dir / f"re_{int(re)}", re,
                                alpha_start=XFOIL_ALPHA_START,
                                alpha_step=ALPHA_STEP, alpha_end=XFOIL_ALPHA_END,
                                max_iter=100)
        a, cl, cd = _clean(polar.alpha_deg, polar.CL, polar.CD)
        if len(a) >= 3:
            cl_row, cd_row = _extend_to_grid(a, cl, cd, alpha_grid)
            rows[re] = (cl_row, cd_row)
            print(f"  Re={re:.0e}: XFOIL {len(polar.alpha_deg)} 点(截断后 {len(a)}), "
                  f"CL_max≈{max(cl):.2f}")
        else:
            print(f"  Re={re:.0e}: XFOIL 失败/点不足，稍后用相邻 Re 插值")

    # 失败 Re 用 log10(Re) 线性插值补全（保证网格完整）
    if len(rows) < len(reynolds):
        have = sorted(rows.keys())
        for re in reynolds:
            if re in rows:
                continue
            if not have:
                # 全部失败：用零升力平板近似
                cl_row = [2 * math.pi * math.radians(a) for a in alpha_grid]
                cd_row = [0.02] * len(alpha_grid)
                rows[re] = (cl_row, cd_row)
                print(f"  Re={re:.0e}: 无可用 Re，用薄翼平板近似")
                continue
            # 相邻 Re 插值（log 空间）
            log_re = math.log10(re)
            lo = max([r for r in have if r <= re], default=have[0])
            hi = min([r for r in have if r >= re], default=have[-1])
            if lo == hi:
                cl_row, cd_row = rows[lo]
            else:
                f = (log_re - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
                cl_lo, cd_lo = rows[lo]
                cl_hi, cd_hi = rows[hi]
                cl_row = [c0 * (1 - f) + c1 * f for c0, c1 in zip(cl_lo, cl_hi)]
                cd_row = [d0 * (1 - f) + d1 * f for d0, d1 in zip(cd_lo, cd_hi)]
            rows[re] = (cl_row, cd_row)
            print(f"  Re={re:.0e}: 由相邻 Re [{lo:.0e},{hi:.0e}] 插值补全")

    return {
        "airfoil": airfoil_name,
        "reynolds": reynolds,
        "alpha_deg": alpha_grid,
        "CL": [rows[re][0] for re in reynolds],
        "CD": [rows[re][1] for re in reynolds],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airfoil", default="clarky")
    ap.add_argument("--xfoil", default="C:/Xfoil/xfoil.exe")
    ap.add_argument("--uiuc-dir", default=str(ROOT / "uiuc" / "coord_seligFmt"))
    ap.add_argument("--out", default=str(ROOT / "data" / "polars" / "clarky_polar.json"))
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / "_xfoil_work"
    work.mkdir(parents=True, exist_ok=True)

    print(f"构建桨叶极线查找表: 翼型={args.airfoil}, Re={REYNOLDS}")
    data = build_polar(args.airfoil, args.xfoil, args.uiuc_dir, work, REYNOLDS)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
