"""Phase 2: Benchmark — 数据集生成基准与质量报告。

功能：
  - 复用 DatasetGenerator 生成 N 组基准数据集（采样 → 并行批分析 → 校验过滤 → 导出）
  - 统计：成功率、平均计算时间、CPU 利用率、并行加速比、数据库大小
  - 绘图：参数分布、性能分布、相关性矩阵（matplotlib, Agg 无 GUI 后端）
  - 生成 Benchmark Report（Markdown）

CPU 利用率定义（无 psutil 依赖，可复现）：
  total_cpu_time = Σ(成功任务 elapsed_s)          # 串行总 CPU 时间
  cpu_utilization = total_cpu_time / (wall_time × max_workers)   # 槽位平均占用率
  parallel_speedup = total_cpu_time / wall_time                 # 等效加速比

配置来自 config/benchmark.yaml（可在 run() 参数覆盖，禁止硬编码）。

用法：
  runner = BenchmarkRunner(config)
  summary = runner.run(n=1000, method="lhs", seed=42)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # 无 GUI 后端，避免 Windows 交互依赖
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

from src.dataset.generator import DatasetGenerator
from src.database.sqlite_manager import SQLiteManager

logger = logging.getLogger("uavdp.benchmark")

# 默认 Benchmark 配置定义文件
DEFAULT_BENCHMARK_PATH = Path(__file__).resolve().parents[2] / "config" / "benchmark.yaml"

# 参数分布字段（设计变量）
PARAM_FIELDS = [
    "range_km_req", "cruise_speed_ms", "payload_kg", "endurance_h_req", "altitude_m",
    "aspect_ratio", "taper_ratio", "wing_sweep_deg", "wing_dihedral_deg",
    "wing_twist_deg", "n_props", "prop_diameter_m", "prop_rpm",
]

# 性能分布字段
PERF_FIELDS = [
    "range_km", "endurance_h", "LD_max", "LD_cruise", "static_margin",
    "climb_rate_ms", "power_cruise_w", "v_stall_ms", "v_cruise_ms",
    "propeller_efficiency",
]

# 相关性矩阵字段（设计变量 + 性能）
CORR_FIELDS = [
    "range_km_req", "cruise_speed_ms", "payload_kg", "endurance_h_req", "altitude_m",
    "aspect_ratio", "taper_ratio", "wing_sweep_deg", "wing_dihedral_deg",
    "wing_twist_deg", "n_props", "prop_diameter_m", "prop_rpm",
    "range_km", "endurance_h", "LD_max", "LD_cruise", "static_margin",
    "climb_rate_ms", "power_cruise_w", "v_stall_ms", "v_cruise_ms",
    "propeller_efficiency",
]


# ============================================================
# 配置加载（YAML 驱动，禁止硬编码）
# ============================================================
def load_benchmark_config(path: str | Path = DEFAULT_BENCHMARK_PATH) -> dict:
    """从 YAML 加载 Benchmark 配置（返回顶层 benchmark 段）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Benchmark 配置不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Benchmark 配置顶层必须是映射: {p}")
    return data.get("benchmark") or {}


def _builtin_benchmark_config() -> dict:
    """内置兜底配置（仅当 YAML 缺失/损坏时使用）。"""
    return {"n": 1000, "method": "lhs", "seed": 42, "max_workers": 8,
            "figure_dpi": 110, "min_corr_samples": 30}


def default_benchmark_config() -> dict:
    """默认 Benchmark 配置：从 config/benchmark.yaml 加载，失败回退内置。"""
    if DEFAULT_BENCHMARK_PATH.exists():
        try:
            return load_benchmark_config(DEFAULT_BENCHMARK_PATH)
        except Exception as e:
            logger.warning("Benchmark 配置 YAML 加载失败(%s)，回退内置配置", e)
    return _builtin_benchmark_config()


# ============================================================
# 样本 → DataFrame
# ============================================================
def _extract_row(sample: dict) -> dict:
    """从单个样本（fetch_dataset_samples 返回结构）提取扁平字段。"""
    mission = sample.get("mission") or {}
    ac = sample.get("aircraft") or {}
    wing = ac.get("wing") or {}
    prop = ac.get("propulsion") or {}
    props = prop.get("propellers") or []
    first_prop = props[0] if props else {}
    perf = sample.get("performance") or {}
    return {
        # 设计变量（mission）
        "range_km_req": mission.get("range_km"),
        "cruise_speed_ms": mission.get("cruise_speed_ms"),
        "payload_kg": mission.get("payload_kg"),
        "endurance_h_req": mission.get("endurance_h"),
        "altitude_m": mission.get("altitude_m"),
        # 设计变量（wing）
        "aspect_ratio": wing.get("aspect_ratio"),
        "taper_ratio": wing.get("taper_ratio"),
        "wing_sweep_deg": wing.get("sweep_quarter_chord_deg"),
        "wing_dihedral_deg": wing.get("dihedral_deg"),
        "wing_twist_deg": wing.get("twist_tip_deg"),
        # 设计变量（propulsion）
        "n_props": prop.get("n_props"),
        "prop_diameter_m": first_prop.get("diameter_m") or prop.get("prop_diameter_m"),
        "prop_rpm": first_prop.get("rpm"),
        # 性能
        "range_km": perf.get("range_km"),
        "endurance_h": perf.get("endurance_h"),
        "LD_max": perf.get("LD_max"),
        "LD_cruise": perf.get("LD_cruise"),
        "static_margin": perf.get("static_margin"),
        "climb_rate_ms": perf.get("climb_rate_ms"),
        "power_cruise_w": perf.get("power_cruise_w"),
        "v_stall_ms": perf.get("v_stall_ms"),
        "v_cruise_ms": perf.get("v_cruise_ms"),
        "propeller_efficiency": perf.get("propeller_efficiency"),
        "htail_volume_coeff": perf.get("htail_volume_coeff"),
        "vtail_volume_coeff": perf.get("vtail_volume_coeff"),
    }


def samples_to_dataframe(samples: list[dict]) -> pd.DataFrame:
    """把样本列表转为 DataFrame（数值列，供统计/绘图/训练）。"""
    if not samples:
        return pd.DataFrame()
    df = pd.DataFrame([_extract_row(s) for s in samples])
    return df.apply(pd.to_numeric, errors="coerce")


# ============================================================
# BenchmarkRunner
# ============================================================
class BenchmarkRunner:
    """数据集生成基准：生成 N 组 + 统计 + 绘图 + 报告。"""

    def __init__(self, config: dict, space=None, validator=None,
                 benchmark_cfg: dict | None = None, max_retries: int = 2,
                 log_level: int = logging.WARNING, worker_fn=None):
        self.config = config
        self.space = space
        self.validator = validator
        self.bcfg = benchmark_cfg if benchmark_cfg is not None else default_benchmark_config()
        self.max_retries = max_retries
        self.log_level = log_level
        self.worker_fn = worker_fn        # 可注入（测试），默认真实 pipeline

    # ---------------- 主入口 ----------------
    def run(self, n: int | None = None, method: str | None = None,
            seed: int | None = None, max_workers: int | None = None,
            dataset_id: str | None = None, output_dir: str | Path | None = None,
            report_dir: str | Path | None = None, resume: bool = True,
            prefilter_enabled: bool | None = None) -> dict:
        """生成 N 组基准并产出统计 + 图 + 报告。返回 summary。"""
        t0 = time.time()
        n = int(n if n is not None else self.bcfg.get("n", 1000))
        method = (method or self.bcfg.get("method") or "lhs").lower()
        seed = seed if seed is not None else self.bcfg.get("seed")
        if max_workers is None:
            max_workers = self.bcfg.get("max_workers")
        output_dir = Path(output_dir or self.bcfg.get("output_dir")
                          or Path(self.config["paths"]["data_dir"]).parent / "datasets")
        report_dir = Path(report_dir or self.bcfg.get("report_dir")
                          or Path(self.config["paths"]["report_dir"]) / "benchmark")
        figure_dpi = int(self.bcfg.get("figure_dpi", 110))
        min_corr = int(self.bcfg.get("min_corr_samples", 30))
        dataset_id = dataset_id or f"bench_{time.strftime('%Y%m%d_%H%M%S')}"

        # 1) 生成数据集（复用 DatasetGenerator）
        gen = DatasetGenerator(self.config, space=self.space,
                               validator=self.validator, log_level=self.log_level,
                               max_retries=self.max_retries, worker_fn=self.worker_fn)
        gen_summary = gen.generate(n, method=method, seed=seed,
                                   dataset_id=dataset_id, output_dir=output_dir,
                                   max_workers=max_workers, resume=resume,
                                   prefilter_enabled=prefilter_enabled)

        # 2) 读回样本 → DataFrame
        samples = self._load_samples(dataset_id)
        df = samples_to_dataframe(samples)

        # 3) 统计
        n_workers = max_workers or gen_summary["stats"].get("max_workers") or 1
        benchmark = self._collect_stats(gen_summary, n_workers, n, len(samples))

        # 4) 绘图
        fig_dir = report_dir / dataset_id / "figures"
        figures = self._plot_all(df, fig_dir, figure_dpi, min_corr)

        # 5) 报告
        report_path = self._write_report(
            report_dir / dataset_id, dataset_id, method, seed, benchmark,
            gen_summary["stats"], figures, len(df))

        summary = {
            "dataset_id": dataset_id,
            "method": method,
            "seed": seed,
            "benchmark": benchmark,
            "generation": gen_summary["stats"],
            "figures": figures,
            "report": str(report_path),
            "elapsed_s": time.time() - t0,
        }
        logger.info("Benchmark 完成: %s", summary)
        return summary

    # ---------------- 读回 ----------------
    def _load_samples(self, dataset_id: str) -> list[dict]:
        db = SQLiteManager(self.config["paths"]["db_path"])
        try:
            return db.fetch_dataset_samples(dataset_id)
        finally:
            db.close()

    # ---------------- 统计 ----------------
    def _collect_stats(self, gen_summary: dict, max_workers: int,
                       n_requested: int, n_valid: int) -> dict:
        db_path = self.config["paths"]["db_path"]
        stats = gen_summary["stats"]
        total = stats.get("total", n_requested)
        success = stats.get("success", 0)
        failed = stats.get("failed", 0)
        rejected = stats.get("rejected", 0)

        db = SQLiteManager(db_path)
        try:
            elapsed = db.fetch_dataset_elapsed(gen_summary["dataset_id"])
        finally:
            db.close()

        total_cpu = float(np.sum(elapsed)) if elapsed else 0.0
        avg_elapsed = float(np.mean(elapsed)) if elapsed else 0.0
        wall = float(stats.get("elapsed_s", 0.0) or 0.0)

        n_workers = max(1, int(max_workers or 1))
        cpu_util = (total_cpu / (wall * n_workers)) if wall > 0 else 0.0
        speedup = (total_cpu / wall) if wall > 0 else 0.0
        db_size_mb = (os.path.getsize(db_path) / (1024 * 1024)
                      if os.path.exists(db_path) else 0.0)

        return {
            "n_requested": n_requested,
            "n_valid_samples": n_valid,
            "success_rate": success / max(total, 1),
            "failed": failed,
            "rejected": rejected,
            "avg_elapsed_s": round(avg_elapsed, 3),
            "total_cpu_time_s": round(total_cpu, 1),
            "wall_time_s": round(wall, 1),
            "max_workers": n_workers,
            "cpu_utilization": round(cpu_util, 3),
            "parallel_speedup": round(speedup, 2),
            "db_size_mb": round(db_size_mb, 2),
        }

    # ---------------- 绘图 ----------------
    def _plot_all(self, df: pd.DataFrame, fig_dir: Path, dpi: int,
                  min_corr: int) -> dict:
        fig_dir.mkdir(parents=True, exist_ok=True)
        figures: dict = {}
        if df.empty:
            logger.warning("无有效样本，跳过绘图")
            return figures

        figures["param_dist"] = self._plot_hist(
            df, PARAM_FIELDS, "Design Parameter Distribution",
            fig_dir / "param_distribution.png", dpi)
        figures["perf_dist"] = self._plot_hist(
            df, PERF_FIELDS, "Performance Distribution",
            fig_dir / "performance_distribution.png", dpi)
        if len(df) >= min_corr:
            corr_path = self._plot_correlation(
                df, CORR_FIELDS, fig_dir / "correlation_matrix.png", dpi)
            if corr_path:
                figures["correlation"] = corr_path
        else:
            logger.info("有效样本 %d < %d，跳过相关性矩阵", len(df), min_corr)
        return figures

    def _plot_hist(self, df: pd.DataFrame, fields: list, title: str,
                   path: Path, dpi: int) -> str:
        """多子图直方图（对常量/窄范围列稳健）。"""
        ncols = 3
        n = len(fields)
        nrows = (n + ncols - 1) // ncols
        fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.0 * nrows))
        axes = np.array(axes).reshape(-1)
        for i, f in enumerate(fields):
            ax = axes[i]
            col = (df[f].dropna().astype(float) if f in df.columns
                   else pd.Series(dtype=float))
            if len(col) == 0:
                ax.axis("off")
                continue
            lo, hi = float(col.min()), float(col.max())
            if hi - lo <= 0 or np.isnan(hi - lo):
                # 常量/窄范围列：仅标注数值，不画直方图
                ax.text(0.5, 0.5, f"{f} = {lo:.3g}", transform=ax.transAxes,
                        ha="center", va="center", fontsize=8)
                ax.set_xticks([])
                ax.set_yticks([])
                continue
            n_bins = min(30, max(2, int(col.nunique())))
            ax.hist(col, bins=n_bins, color="#4C9AFF", alpha=0.75,
                    edgecolor="white")
            ax.set_title(f, fontsize=9)
            ax.tick_params(labelsize=7)
        for j in range(n, len(axes)):
            axes[j].axis("off")
        fig.suptitle(title, fontsize=12)
        fig.tight_layout(rect=[0, 0, 1, 0.97])
        fig.savefig(path, dpi=dpi)
        plt.close(fig)
        return str(path)

    def _plot_correlation(self, df: pd.DataFrame, fields: list,
                          path: Path, dpi: int) -> str:
        """Pearson 相关性矩阵热力图（过滤常量/缺失列）。"""
        sub = df[fields].apply(pd.to_numeric, errors="coerce")
        # 排除常量列（std=0，相关系数无意义）与全缺失列
        keep = [c for c in fields if c in sub.columns
                and sub[c].notna().any() and sub[c].nunique() > 1]
        sub = sub[keep]
        if sub.shape[1] < 2:
            logger.warning("有效数值列不足(%d)，跳过相关性矩阵", sub.shape[1])
            return ""
        cols = list(sub.columns)
        corr = sub.corr()
        n = len(cols)
        fig, ax = plt.subplots(figsize=(max(10, 0.62 * n), max(8, 0.52 * n)))
        im = ax.imshow(corr, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
        ax.set_xticks(range(n))
        ax.set_xticklabels(cols, rotation=90, fontsize=7)
        ax.set_yticks(range(n))
        ax.set_yticklabels(cols, fontsize=7)
        for i in range(n):
            for j in range(n):
                ax.text(j, i, f"{corr.iloc[i, j]:.2f}", ha="center", va="center",
                        fontsize=5.5)
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        ax.set_title("Design-Performance Correlation Matrix", fontsize=11)
        fig.tight_layout()
        fig.savefig(path, dpi=dpi)
        plt.close(fig)
        return str(path)

    # ---------------- 报告 ----------------
    def _write_report(self, report_dir: Path, dataset_id: str, method: str,
                      seed, benchmark: dict, gen_stats: dict,
                      figures: dict, n_samples: int) -> str:
        report_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Benchmark Report",
            "",
            f"- 数据集: `{dataset_id}`",
            f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
            f"- 采样方法: `{method}`, 随机种子: `{seed}`",
            "",
            "## 统计摘要",
            "",
            "| 指标 | 值 |",
            "|---|---|",
            f"| 请求样本数 | {benchmark['n_requested']} |",
            f"| 有效样本数（入库） | {benchmark['n_valid_samples']} |",
            f"| 成功率 | {benchmark['success_rate'] * 100:.1f}% |",
            f"| 失败（几何/运行异常） | {benchmark['failed']} |",
            f"| 校验拒绝（数据质量） | {benchmark['rejected']} |",
            f"| 平均计算时间 | {benchmark['avg_elapsed_s']} s |",
            f"| 总 CPU 时间 | {benchmark['total_cpu_time_s']} s |",
            f"| 墙钟时间 | {benchmark['wall_time_s']} s |",
            f"| 最大并行 worker | {benchmark['max_workers']} |",
            f"| CPU 利用率 | {benchmark['cpu_utilization'] * 100:.1f}% |",
            f"| 并行加速比 | {benchmark['parallel_speedup']}× |",
            f"| 数据库大小 | {benchmark['db_size_mb']} MB |",
            "",
            "## 附图",
            "",
        ]
        if figures.get("param_dist"):
            lines.append(f"- 参数分布: `{figures['param_dist']}`")
        if figures.get("perf_dist"):
            lines.append(f"- 性能分布: `{figures['perf_dist']}`")
        if figures.get("correlation"):
            lines.append(f"- 相关性矩阵: `{figures['correlation']}`")
        lines.append("")
        lines.append("> 说明：CPU 利用率 = 总 CPU 时间 / (墙钟时间 × worker 数)；"
                     "相关性矩阵为设计变量与性能指标的 Pearson 相关系数。")
        lines.append("")

        report_path = report_dir / "benchmark_report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return str(report_path)
