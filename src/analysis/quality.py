"""Phase 2.5 C1: 数据集质量分析（DataQualityAnalyzer）。

对生成的数据集做五类质量分析（对应 C1.5 验收项 A~E）：
  A. 采样质量（连续变量分布 / 离散变量频数 / 相关性 / 空间覆盖）
  B. 筛选质量（预筛通过率 / 拒绝原因占比 / 误杀率 / 完整分析有效率）
  C. 物理合理性（MTOW / 翼展面积 / 展弦比锥比 / 桨尖速度 / 桨盘载荷 /
                推重比 / 巡航功率 / 失速速度 / 航程续航 / 静稳定裕度）
  D. 标签质量（缺失 / 重复 / 异常值 / 变量-物理因果 / 任务输入vs预测 / 模型限制记录）
  E. 数据库质量（读写一致 / design_id 唯一 / 失败记录完整 / 元数据完整 / 断点恢复一致）

纯分析模块，只消费样本数据与统计，不修改数据库。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# 样本字段提取
# ============================================================
def _first_prop(sample: dict) -> dict:
    ac = sample.get("aircraft") or {}
    prop = ac.get("propulsion") or {}
    props = prop.get("propellers") or []
    return props[0] if props else {}


def extract_physical_row(sample: dict) -> dict:
    """从单样本提取物理指标行（含预筛对照指标）。"""
    mission = sample.get("mission") or {}
    ac = sample.get("aircraft") or {}
    wing = ac.get("wing") or {}
    prop = ac.get("propulsion") or {}
    motor = prop.get("motor") or {}
    fp = _first_prop(sample)
    perf = sample.get("performance") or {}

    diam = fp.get("diameter_m") or prop.get("prop_diameter_m")
    rpm = fp.get("rpm") or prop.get("prop_rpm")
    # Phase 2.5 C3 D3：桨尖速度区分设计转速 vs 实际工作转速。
    # 优先读入库字段（tip_speed_design_ms / tip_speed_operating_ms）；
    # 旧数据无这些字段时回退计算（design 用设计转速、operating 用工作转速）。
    tip_speed_design = perf.get("tip_speed_design_ms")
    tip_speed_operating = perf.get("tip_speed_operating_ms")
    if tip_speed_design is None:
        tip_speed_design = (math.pi * (rpm / 60.0) * diam) if (diam and rpm) else np.nan
    if tip_speed_operating is None:
        op_rpm = perf.get("prop_operating_rpm")
        tip_speed_operating = (math.pi * (op_rpm / 60.0) * diam) \
            if (diam and op_rpm) else np.nan
    disk_area = math.pi * (diam / 2.0) ** 2 if diam else np.nan
    motor_power_kw = motor.get("max_power_kw") or prop.get("motor_power_kw")
    disk_loading = (motor_power_kw * 1000.0 / disk_area) if (motor_power_kw and disk_area) else np.nan

    return {
        # 任务输入
        "mission_range_km": mission.get("range_km"),
        "cruise_speed_ms": mission.get("cruise_speed_ms"),
        "payload_kg": mission.get("payload_kg"),
        "endurance_h_req": mission.get("endurance_h"),
        "altitude_m": mission.get("altitude_m"),
        # 设计变量
        "aspect_ratio": wing.get("aspect_ratio"),
        "taper_ratio": wing.get("taper_ratio"),
        "wing_sweep_deg": wing.get("sweep_quarter_chord_deg"),
        "wing_dihedral_deg": wing.get("dihedral_deg"),
        "wing_twist_deg": wing.get("twist_tip_deg"),
        "n_props": prop.get("n_props"),
        "prop_diameter_m": diam,
        "prop_rpm": rpm,
        # 物理合理性指标
        "mtow_kg": ac.get("mtow_kg"),
        "span_m": wing.get("span_m"),
        "wing_area_m2": wing.get("area_m2"),
        "tip_speed_ms": tip_speed_operating,       # 工作桨尖速度（物理真实值）
        "tip_speed_design_ms": tip_speed_design,   # 设计转速桨尖速度
        "tip_speed_operating_ms": tip_speed_operating,
        "disk_loading_w_m2": disk_loading,
        "thrust_weight_ratio": perf.get("thrust_weight_ratio"),
        "power_cruise_w": perf.get("power_cruise_w"),
        "v_stall_ms": perf.get("v_stall_ms"),
        # 输出（任务指标）
        "range_km": perf.get("range_km"),
        "range_margin_km": perf.get("range_margin_km"),
        "endurance_h": perf.get("endurance_h"),
        "ld_cruise": perf.get("LD_cruise"),
        "ld_max": perf.get("LD_max"),
        "ld_max_total": perf.get("LD_max_total"),
        "climb_rate_ms": perf.get("climb_rate_ms"),
        # 稳定性标签（实际计算值 vs 人为目标）
        "static_margin": perf.get("static_margin"),
        "static_margin_pre_trim": perf.get("static_margin_pre_trim"),
        "static_margin_target": perf.get("static_margin_target"),
    }


def samples_to_physical_df(samples: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([extract_physical_row(s) for s in samples])
    return df.apply(pd.to_numeric, errors="coerce")


# ============================================================
# 统计工具
# ============================================================
def _describe_series(series: pd.Series) -> dict:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) == 0:
        return {"count": 0, "missing": int(series.isna().sum())}
    return {
        "count": int(len(s)),
        "missing": int(series.isna().sum()),
        "min": float(s.min()),
        "max": float(s.max()),
        "mean": float(s.mean()),
        "median": float(s.median()),
        "std": float(s.std()),
    }


class DataQualityAnalyzer:
    """数据集质量分析器（C1.5）。"""

    def __init__(self, samples: list[dict] | None = None,
                 prefilter_rejections: list[dict] | None = None,
                 failures: list[dict] | None = None,
                 gen_summary: dict | None = None,
                 misclassification: dict | None = None):
        self.samples = samples or []
        self.rejections = prefilter_rejections or []
        self.failures = failures or []
        self.summary = gen_summary or {}
        self.misclassification = misclassification or {}

    # ---------------- A. 采样质量 ----------------
    def sampling_quality(self, raw_designs: list[dict]) -> dict:
        """连续变量分布 + 离散变量频数 + 相关性 + 空间覆盖（基于原始采样）。"""
        if not raw_designs:
            return {"n": 0, "note": "无原始采样设计"}
        cont_names = [k for k, v in raw_designs[0].items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)]
        disc_names = [k for k, v in raw_designs[0].items() if not isinstance(v, (int, float))]

        continuous = {}
        for name in cont_names:
            vals = [d.get(name) for d in raw_designs]
            continuous[name] = _describe_series(pd.Series(vals, dtype=float))

        discrete = {}
        for name in disc_names:
            from collections import Counter
            discrete[name] = dict(Counter(str(d.get(name)) for d in raw_designs))

        # 相关性（连续变量）
        corr = {}
        df = pd.DataFrame([{k: d.get(k) for k in cont_names} for d in raw_designs],
                          dtype=float)
        if df.shape[1] >= 2:
            c = df.corr()
            corr = {name: {k: round(float(c.loc[name, k]), 3) for k in cont_names if k != name}
                    for name in cont_names}
        # 空间覆盖（每维覆盖范围 = (max-min)/(hi-lo)，若提供设计空间）
        return {"n": len(raw_designs),
                "continuous": continuous,
                "discrete": discrete,
                "correlation": corr}

    # ---------------- B. 筛选质量 ----------------
    def screening_quality(self) -> dict:
        s = self.summary or {}
        pre = s.get("prefilter") or {}
        stats = s.get("stats") or {}
        n_sampled = int(pre.get("n_sampled", 0))
        n_rejected = int(pre.get("n_rejected", 0))
        n_passed = int(pre.get("n_passed", 0))
        success = int(stats.get("success", 0))
        rejected_val = int(stats.get("rejected", 0))
        failed = int(stats.get("failed", 0))

        # 拒绝原因占比（Phase 2.5 C3 D5：基于 DB 实际保留的记录统计，
        # 若记录不全则 n_rejections_recorded < n_rejected，报告须标注口径）
        from collections import Counter
        reason_counter = Counter()
        for r in self.rejections:
            for f in r.get("failure_reasons") or []:
                reason_counter[f.get("name", "未知")] += 1

        return {
            "n_sampled": n_sampled,
            "n_prefilter_rejected": n_rejected,
            "n_rejections_recorded": len(self.rejections),
            "n_prefilter_passed": n_passed,
            "prefilter_pass_rate": round(n_passed / n_sampled, 4) if n_sampled else 0.0,
            "n_entered_full_analysis": n_passed,
            "n_valid": success,
            "n_validation_rejected": rejected_val,
            "n_failed": failed,
            "pipeline_validity_rate": round(success / n_passed, 4) if n_passed else 0.0,
            "overall_validity_rate": round(success / n_sampled, 4) if n_sampled else 0.0,
            "rejection_reasons": dict(reason_counter),
            "failed_breakdown": {k: v for k, v in stats.items()
                                 if k.startswith("failed_")},
            "misclassification": self.misclassification,
        }

    # ---------------- C. 物理合理性 ----------------
    def physical_reasonableness(self) -> dict:
        if not self.samples:
            return {"n": 0, "note": "无有效样本"}
        df = samples_to_physical_df(self.samples)
        fields = ["mtow_kg", "span_m", "wing_area_m2", "aspect_ratio", "taper_ratio",
                  "tip_speed_operating_ms", "tip_speed_design_ms",
                  "disk_loading_w_m2", "thrust_weight_ratio",
                  "power_cruise_w", "v_stall_ms", "range_km", "endurance_h",
                  "ld_cruise", "ld_max", "ld_max_total",
                  "static_margin_pre_trim", "static_margin"]
        out = {}
        for f in fields:
            if f in df.columns:
                out[f] = _describe_series(df[f])
        return {"n": len(self.samples), "metrics": out}

    # ---------------- D. 标签质量 ----------------
    def label_quality(self) -> dict:
        df = samples_to_physical_df(self.samples)

        # 缺失值
        missing = {c: int(df[c].isna().sum()) for c in df.columns if df[c].isna().any()}

        # 重复设计（按数值字段四舍五入到 4 位）
        key_cols = [c for c in df.columns
                    if c in ("mission_range_km", "cruise_speed_ms", "payload_kg",
                             "endurance_h_req", "altitude_m", "aspect_ratio",
                             "taper_ratio", "prop_rpm", "prop_diameter_m", "n_props")]
        dup = 0
        if key_cols:
            rounded = df[key_cols].round(4)
            dup = int(rounded.duplicated().sum())

        # 异常值（z-score > 5 或 IQR 之外，仅数值列）
        outliers = {}
        for c in df.columns:
            s = df[c].dropna()
            if len(s) < 4:
                continue
            z = (s - s.mean()).abs() / (s.std() + 1e-12)
            n_out = int((z > 5).sum())
            if n_out:
                outliers[c] = n_out

        # 变量-物理因果（关键输入 vs 输出的 Pearson 相关）
        causal = {}
        pairs = [
            ("aspect_ratio", "ld_max"), ("aspect_ratio", "ld_cruise"),
            ("cruise_speed_ms", "power_cruise_w"), ("payload_kg", "mtow_kg"),
            ("prop_rpm", "tip_speed_design_ms"), ("prop_rpm", "tip_speed_operating_ms"),
            ("prop_diameter_m", "disk_loading_w_m2"),
            ("endurance_h_req", "mtow_kg"), ("taper_ratio", "ld_max"),
        ]
        for a, b in pairs:
            if a in df.columns and b in df.columns:
                sub = df[[a, b]].dropna()
                if len(sub) >= 3 and sub[a].std() > 0 and sub[b].std() > 0:
                    causal[f"{a}→{b}"] = round(float(sub[a].corr(sub[b])), 3)

        # 任务输入 vs 预测输出不混淆
        confound = {}
        if "mission_range_km" in df.columns and "range_km" in df.columns:
            sub = df[["mission_range_km", "range_km", "range_margin_km"]].dropna()
            confound["n_range_margin_consistent"] = int(
                (np.abs(sub["range_km"] - sub["mission_range_km"] - sub["range_margin_km"])
                 < 1.0).sum() if len(sub) else 0)

        # 模型限制记录（metadata.model_limitations 是否注入）
        n_lim = sum(1 for s in self.samples
                    if (s.get("metadata") or {}).get("model_limitations"))
        return {"n_samples": len(self.samples),
                "missing": missing, "n_duplicate_designs": dup,
                "outliers": outliers, "causal_correlation": causal,
                "range_input_output": confound,
                "n_model_limitations_recorded": n_lim}

    # ---------------- E. 数据库质量 ----------------
    def database_quality(self, db_path: str | Path | None = None,
                         state_file: str | Path | None = None) -> dict:
        from src.database.sqlite_manager import SQLiteManager
        s = self.summary or {}
        stats = s.get("stats") or {}
        dataset_id = s.get("dataset_id")
        out = {}

        # 读写一致性 + design_id 唯一性
        if db_path and dataset_id:
            db = SQLiteManager(db_path)
            try:
                n_db = len(db.fetch_dataset_samples(dataset_id))
                out["n_db_samples"] = n_db
                out["n_stats_success"] = int(stats.get("success", 0))
                out["write_read_consistent"] = n_db == int(stats.get("success", 0))
                out["n_db_prefilter_rejections"] = db.count_prefilter_rejections(dataset_id)
                out["n_summary_prefilter_rejected"] = int(
                    (s.get("prefilter") or {}).get("n_rejected", 0))
            finally:
                db.close()

        # design_id 唯一性（从 readback 样本）
        ids = [x.get("design_id") for x in self.samples]
        out["n_design_ids"] = len(ids)
        out["n_unique_design_ids"] = len(set(ids))
        out["design_id_unique"] = len(ids) == len(set(ids))

        # 失败样本记录完整性（Phase 2.5 C3 D8：仅对比真实失败数，不含校验拒绝。
        # 校验拒绝样本按 Decision 012 本就不入库、不记录在 failures 清单，
        # 不应计入「应记录的失败」。）
        n_failed = int(stats.get("failed", 0))
        out["n_failures_recorded"] = len(self.failures)
        out["failure_record_complete"] = len(self.failures) >= n_failed

        # 元数据完整性
        required = ("platform_version", "sample_method", "generated_at")
        n_meta_ok = sum(1 for x in self.samples
                        if all((x.get("metadata") or {}).get(k) for k in required))
        out["n_metadata_complete"] = n_meta_ok

        # 断点恢复一致性（state_file completed 数量 == 库中成功样本）
        if state_file and Path(state_file).exists():
            import json
            try:
                state = json.loads(Path(state_file).read_text(encoding="utf-8"))
                completed = state.get("completed", {})
                out["n_state_completed"] = len(completed)
                out["resume_consistent"] = len(completed) >= out.get("n_db_samples", 0)
            except Exception:
                out["resume_consistent"] = False
        return out


# ============================================================
# 报告生成
# ============================================================
def _fmt_table(rows: list[tuple]) -> str:
    lines = ["| 指标 | 值 |", "|---|---|"]
    for k, v in rows:
        lines.append(f"| {k} | {v} |")
    return "\n".join(lines)


def build_quality_report(analysis: dict, dataset_id: str,
                         out_path: str | Path | None = None) -> str:
    """把分析结果组装为 Markdown 报告；可选写出文件。"""
    L = []
    L.append(f"# 数据质量报告 — {dataset_id}")
    L.append("")
    L.append("> Phase 2.5 数据集质量分析（A~E：采样/筛选/物理合理性/标签/数据库）。")
    L.append("")

    # B 筛选质量
    scr = analysis.get("screening_quality", {})
    L.append("## 筛选质量（B）")
    L.append("")
    L.append(_fmt_table([
        ("原始采样数", scr.get("n_sampled")),
        ("预筛拒绝数", scr.get("n_prefilter_rejected")),
        ("预筛通过数（进入完整分析）", scr.get("n_prefilter_passed")),
        ("预筛通过率", f"{scr.get('prefilter_pass_rate', 0) * 100:.1f}%"),
        ("完整分析有效样本", scr.get("n_valid")),
        ("校验拒绝（性能不满足）", scr.get("n_validation_rejected")),
        ("失败（几何/外部工具/气动）", scr.get("n_failed")),
        ("完整分析有效率（valid/通过）", f"{scr.get('pipeline_validity_rate', 0) * 100:.1f}%"),
        ("整体有效率（valid/采样）", f"{scr.get('overall_validity_rate', 0) * 100:.1f}%"),
    ]))
    L.append("")
    L.append("**预筛拒绝原因占比**：" + ", ".join(
        f"{k}×{v}" for k, v in (scr.get("rejection_reasons") or {}).items()) or "无")
    L.append("")
    fb = scr.get("failed_breakdown") or {}
    if fb:
        L.append("**失败阶段分类**：" + ", ".join(f"{k}×{v}" for k, v in fb.items()))
        L.append("")

    # 误杀
    mc = scr.get("misclassification") or {}
    if mc:
        L.append("### 预筛误杀验证（C1.2）")
        L.append("")
        L.append(_fmt_table([
            ("预筛拒绝总数", mc.get("n_rejected")),
            ("抽样复核数", mc.get("n_checked")),
            ("完整分析确认不可行", mc.get("n_confirmed_infeasible")),
            ("完整分析确认可行（误杀）", mc.get("n_misclassified")),
            ("误杀率", f"{mc.get('misclassification_rate', 0) * 100:.1f}%"),
        ]))
        L.append("")
        for detail in mc.get("details") or []:
            L.append(f"- `{detail.get('design_id')}`：预筛原因 "
                     f"`{detail.get('prefilter_reasons')}` → 完整分析 "
                     f"`{detail.get('full_analysis')}`")
        L.append("")

    # C 物理合理性
    phys = analysis.get("physical_reasonableness", {})
    L.append("## 物理合理性（C）")
    L.append("")
    metrics = phys.get("metrics") or {}
    if metrics:
        L.append("| 指标 | 均值 | 中位数 | 最小 | 最大 | 缺失 |")
        L.append("|---|---|---|---|---|---|")
        for name, d in metrics.items():
            L.append(f"| {name} | {d.get('mean', '-')} | {d.get('median', '-')} | "
                     f"{d.get('min', '-')} | {d.get('max', '-')} | {d.get('missing', 0)} |")
        L.append("")

    # A 采样质量
    smp = analysis.get("sampling_quality", {})
    L.append("## 采样质量（A）")
    L.append("")
    L.append(f"- 原始采样数：{smp.get('n')}")
    disc = smp.get("discrete") or {}
    if disc:
        L.append("- 离散变量频数：")
        for name, cnt in disc.items():
            L.append(f"  - {name}: " + ", ".join(f"{k}×{v}" for k, v in cnt.items()))
    L.append("")

    # D 标签质量
    lab = analysis.get("label_quality", {})
    L.append("## 数据标签质量（D）")
    L.append("")
    L.append(_fmt_table([
        ("样本数", lab.get("n_samples")),
        ("缺失值字段数", len(lab.get("missing") or {})),
        ("重复设计数", lab.get("n_duplicate_designs")),
        ("异常值字段数", len(lab.get("outliers") or {})),
        ("模型限制已记录样本数", lab.get("n_model_limitations_recorded")),
    ]))
    L.append("")
    if lab.get("missing"):
        L.append("**缺失值**：" + ", ".join(f"{k}×{v}" for k, v in lab["missing"].items()))
        L.append("")
    causal = lab.get("causal_correlation") or {}
    if causal:
        L.append("**变量-物理因果（Pearson 相关）**：")
        for k, v in causal.items():
            L.append(f"- {k}: {v}")
        L.append("")
    rng = lab.get("range_input_output") or {}
    if rng:
        L.append(f"**任务输入/预测输出一致性**：{rng}")
        L.append("")

    # E 数据库质量
    dbq = analysis.get("database_quality", {})
    L.append("## 数据库质量（E）")
    L.append("")
    L.append(_fmt_table([
        ("库中样本数", dbq.get("n_db_samples")),
        ("统计成功数", dbq.get("n_stats_success")),
        ("读写一致", dbq.get("write_read_consistent")),
        ("design_id 唯一", dbq.get("design_id_unique")),
        ("失败记录完整", dbq.get("failure_record_complete")),
        ("元数据完整样本数", dbq.get("n_metadata_complete")),
        ("断点恢复一致", dbq.get("resume_consistent")),
    ]))
    L.append("")

    text = "\n".join(L)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text, encoding="utf-8")
    return text
