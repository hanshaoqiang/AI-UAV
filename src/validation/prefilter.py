"""Phase 2.5: 可行性预筛（Prefilter）— 在外部工具分析之前快速过滤明显不可行设计。

目标：
  1. 提高设计空间内有效样本比例；
  2. 减少明显不可行设计对 OpenVSP/XFOIL/VSPAero 计算资源的浪费。

纯 Python 实现，不调用任何外部工具（~毫秒级）。区分硬约束与软约束：
  硬约束不满足 → 提前过滤，记录原因与具体指标；
  软约束不满足 → 记录 warning，不删除样本。

检查内容（阈值见 config/prefilter.yaml，禁止硬编码）：
  1. 几何/重量收敛（复用 size_aircraft：重量迭代发散 + 几何校验）
  2. 桨尖速度（π·n·D，跨声速损失）
  3. 桨盘载荷（单桨轴功率 / 桨盘面积，小直径+高功率组合）
  4. 推进推力可行性（转速受限 ∪ 功率受限 ≥ 巡航所需推力）
  5. 航程初估 / 推重比 / 失速裕度（软约束，仅告警）

输出：PrefilterResult（feasible + hard_failures + warnings + metrics）。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from src.core.contracts import Aircraft, MissionSpec
from src.geometry.aircraft import build_aircraft
from src.geometry.sizing import air_density
from src.performance.parasite_drag import ParasiteDragModel
from src.propulsion.bemt import BEMTSolver, load_polar_table
from src.propulsion.model import propeller_thrust_power

logger = logging.getLogger("uavdp.validation.prefilter")

G = 9.81
DEFAULT_PREFILTER_PATH = Path(__file__).resolve().parents[2] / "config" / "prefilter.yaml"


@dataclass
class PrefilterResult:
    """一次预筛的完整结果。"""
    feasible: bool
    hard_failures: list = field(default_factory=list)   # list[dict{name, detail, value, limit}]
    warnings: list = field(default_factory=list)        # list[dict{name, detail, value, limit}]
    metrics: dict = field(default_factory=dict)         # 关键指标（供记录/对照）

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "hard_failures": self.hard_failures,
            "warnings": self.warnings,
            "metrics": self.metrics,
        }


def _builtin_prefilter_config() -> dict:
    return {
        # 硬约束仅保留「可靠预测完整分析失败」的规则（C1.2 误杀验证）：
        #   推力不足 / 推重比不足 / 几何重量不可行
        "hard": {
            "thrust_margin_min": 1.0,
            "thrust_weight_ratio_min": 0.08,
        },
        "soft": {
            # 桨尖速度降级为软约束（C1.2：设计转速是推进求解上限，工作转速被节流下调，
            # π·n_design·D 高估实际桨尖速度，硬过滤会误杀）
            "tip_speed_warn_ms": 220.0,
            # 桨盘载荷降级为软约束（C1.2：按电机最大功率计，实际巡航功率远低于最大功率，
            # 且完整分析验证不检查盘载荷，单独用它硬过滤会误杀）
            "disk_loading_warn_w_m2": 4000.0,
            "range_ratio_warn": 1.0,
            "thrust_weight_ratio_warn": 0.12,
            "v_stall_to_cruise_warn": 0.60,
        },
    }


def load_prefilter_config(path: str | Path) -> dict:
    """从 YAML 加载预筛阈值。"""
    import yaml
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"预筛阈值文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"预筛阈值顶层必须是映射: {p}")
    return data


def default_prefilter_config() -> dict:
    """默认预筛阈值：从 config/prefilter.yaml 加载；失败时回退内置。"""
    if DEFAULT_PREFILTER_PATH.exists():
        try:
            return load_prefilter_config(DEFAULT_PREFILTER_PATH)
        except Exception as e:
            logger.warning("预筛阈值 YAML 加载失败(%s)，回退内置", e)
    return _builtin_prefilter_config()


class FeasibilityPrefilter:
    """快速可行性预筛器。

    用法：
      prefilter = FeasibilityPrefilter(sizing)
      res = prefilter.check(mission, design)     # design 为几何+推进设计变量 dict
      res.feasible / res.hard_failures / res.warnings / res.metrics
    """

    def __init__(self, sizing: dict, config: dict | None = None):
        self.sizing = sizing
        self.cfg = config if config is not None else default_prefilter_config()
        self.parasite = ParasiteDragModel(sizing.get("parasite_drag"))
        self.oswald = float(sizing.get("oswald_efficiency", 0.85))
        # BEMT 求解器（Phase 2.5）：与完整分析 PropulsionModel 一致；加载失败回退动量理论
        self.bemt = None
        try:
            polar_path = sizing.get("prop_polar_table")
            self.bemt = BEMTSolver(load_polar_table(polar_path), sizing)
        except Exception as e:   # noqa: BLE001
            logger.warning("预筛 BEMT 初始化失败(%s)，回退动量理论", e)

    def _thrust_power(self, diameter_m: float, pitch_m: float, rpm: float,
                      velocity_ms: float, rho: float, n_blades: int = 2):
        """单桨推力/轴功率/效率。优先 BEMT，未启用则用 legacy 动量理论。"""
        if self.bemt is not None:
            return self.bemt.thrust_power(
                diameter_m, pitch_m, rpm, velocity_ms, rho, n_blades)
        return propeller_thrust_power(diameter_m, pitch_m, rpm, velocity_ms, rho, n_blades)

    def _available_thrust(self, p, velocity_ms: float, rho: float,
                          p_max_shaft: float) -> tuple[float, float]:
        """在转速 ≤ 设计转速与轴功率 ≤ p_max_shaft 双约束下求最大推力。

        Phase 2.5 性能优化：功率随转速单调非减（风车区为 0，之后单调增），
        用二分查找功率超限边界（36 点 → ~14 次），取超限前转速的最大推力。
        """
        design_rpm = float(p.rpm) if p.rpm and p.rpm > 0 else 6000.0
        t_hi, pw_hi, _ = self._thrust_power(p.diameter_m, p.pitch_m, design_rpm,
                                            velocity_ms, rho, p.n_blades)
        if pw_hi <= p_max_shaft:
            return t_hi, design_rpm               # 设计转速也不超限
        lo, hi = 250.0, design_rpm
        for _ in range(12):
            mid = 0.5 * (lo + hi)
            _, pw_mid, _ = self._thrust_power(p.diameter_m, p.pitch_m, mid,
                                              velocity_ms, rho, p.n_blades)
            if pw_mid > p_max_shaft:
                hi = mid
            else:
                lo = mid
        t_lo, _, _ = self._thrust_power(p.diameter_m, p.pitch_m, lo,
                                        velocity_ms, rho, p.n_blades)
        return t_lo, lo

    def check(self, mission: MissionSpec, design: dict) -> PrefilterResult:
        """执行预筛。mission 为任务需求，design 为几何+推进设计变量 dict。"""
        hard = self.cfg.get("hard", {})
        soft = self.cfg.get("soft", {})

        # 1) 几何 / 重量收敛（复用 size_aircraft，纯 Python）
        try:
            ac = build_aircraft(mission, design, self.sizing)
        except ValueError as e:
            return PrefilterResult(
                feasible=False,
                hard_failures=[{"name": "几何/重量", "detail": str(e)[:160]}],
            )

        rho = air_density(mission.altitude_m)
        v = mission.cruise_speed_ms
        w = ac.mtow_kg * G

        # 2) 巡航阻力（与 sizing/evaluator 一致的寄生+诱导模型）
        re_cr = rho * v * ac.wing.mean_chord_m / 1.7894e-5
        cd0 = self.parasite.evaluate(ac, re_cr, v / 340.3, ac.wing.area_m2).cd0
        cl_cr = w / (0.5 * rho * v ** 2 * ac.wing.area_m2)
        cd = cd0 + cl_cr ** 2 / (math.pi * ac.wing.aspect_ratio * self.oswald)
        drag = 0.5 * rho * v ** 2 * ac.wing.area_m2 * cd
        thrust_required = drag
        ld_cr = cl_cr / cd if cd > 0 else 0.0

        # 3) 推进可行性（转速 ≤ 设计转速 ∪ 轴功率 ≤ 电机最大功率，扫转速求最大推力）
        n = max(ac.propulsion.n_props, 1)
        motor = ac.propulsion.motor
        tip_speeds, disk_loadings, etas = [], [], []
        p_max_shaft_each = motor.max_power_kw * 1000.0 * motor.efficiency \
            * self.sizing["esc_efficiency"]
        thrust_available = 0.0
        for p in ac.propulsion.propellers:
            d, rpm = p.diameter_m, p.rpm
            tip_speeds.append(math.pi * (rpm / 60.0) * d)
            disk_area = math.pi * (d / 2.0) ** 2
            p_single = motor.max_power_kw * 1000.0
            disk_loadings.append(p_single / disk_area if disk_area > 0 else float("inf"))
            t_avail, _ = self._available_thrust(p, v, rho, p_max_shaft_each)
            thrust_available += t_avail
            _, _, eta_d = self._thrust_power(d, p.pitch_m, rpm, v, rho, p.n_blades)
            etas.append(eta_d)

        eta_avg = sum(etas) / len(etas) if etas else self.sizing["prop_efficiency"]

        tip_speed_max = max(tip_speeds) if tip_speeds else 0.0
        disk_loading_max = max(disk_loadings) if disk_loadings else 0.0

        failures, warnings = [], []

        # 软约束：桨尖速度（C1.2 起为软约束——设计转速节流下调后高估实际桨尖速度）
        tip_warn = float(soft.get("tip_speed_warn_ms", 220.0))
        if tip_speed_max > tip_warn:
            warnings.append({"name": "桨尖速度偏高", "detail": f"{tip_speed_max:.0f} m/s",
                             "value": tip_speed_max, "limit": f"≤ {tip_warn} m/s"})

        # 软约束：桨盘载荷（C1.2 起为软约束——按电机最大功率计，实际巡航功率低得多，
        # 且完整分析验证不检查盘载荷，单独硬过滤会误杀）
        dl_warn = float(soft.get("disk_loading_warn_w_m2", 4000.0))
        if disk_loading_max > dl_warn:
            warnings.append({"name": "桨盘载荷偏高", "detail": f"{disk_loading_max:.0f} W/m²",
                             "value": disk_loading_max, "limit": f"≤ {dl_warn} W/m²"})

        # 硬约束：推进推力可行性（与 DataValidator 的推重比/剩余功率校验一致）
        t_margin = float(hard.get("thrust_margin_min", 1.0))
        if thrust_available < thrust_required * t_margin:
            failures.append({
                "name": "推力不足", "detail": f"T_avail={thrust_available:.1f} N < 需{thrust_required:.1f} N",
                "value": thrust_available / max(thrust_required, 1e-6),
                "limit": f"≥ {t_margin}"})

        # 硬约束：推重比（与 validation.yaml thrust_weight_ratio_min 一致）
        tw = thrust_available / w
        tw_min = float(hard.get("thrust_weight_ratio_min", 0.08))
        if tw < tw_min:
            failures.append({"name": "推重比不足", "detail": f"T/W={tw:.3f}",
                             "value": tw, "limit": f"≥ {tw_min}"})

        # 软约束：航程初估
        range_est_km = self._estimate_range_km(ac, w, ld_cr, eta_avg, rho, v)
        if mission.range_km and range_est_km < mission.range_km * float(soft.get("range_ratio_warn", 1.0)):
            warnings.append({"name": "航程预估不足",
                             "detail": f"R≈{range_est_km:.0f} km < 目标{mission.range_km:.0f} km",
                             "value": range_est_km, "limit": f"≥ {mission.range_km} km"})

        # 软约束：推重比
        if tw < float(soft.get("thrust_weight_ratio_warn", 0.12)):
            warnings.append({"name": "推重比偏低", "detail": f"T/W={tw:.3f}",
                             "value": tw, "limit": f"≥ {soft.get('thrust_weight_ratio_warn', 0.12)}"})

        # 软约束：失速裕度
        cl_max_3d = self.sizing["cl_max_2d"] * self.sizing["cl_max_3d_factor"]
        v_stall = (2.0 * w / (1.225 * ac.wing.area_m2 * cl_max_3d)) ** 0.5
        if v_stall / v > float(soft.get("v_stall_to_cruise_warn", 0.60)):
            warnings.append({"name": "失速裕度不足",
                             "detail": f"v_stall={v_stall:.1f} m/s (巡航{v:.1f})",
                             "value": v_stall / v, "limit": f"≤ {soft.get('v_stall_to_cruise_warn', 0.60)}"})

        metrics = {
            "mtow_kg": round(ac.mtow_kg, 2),
            "wing_area_m2": round(ac.wing.area_m2, 3),
            "span_m": round(ac.wing.span_m, 3),
            "cl_cruise": round(cl_cr, 3),
            "ld_cruise": round(ld_cr, 1),
            "thrust_required_n": round(thrust_required, 2),
            "thrust_available_n": round(thrust_available, 2),
            "tip_speed_max_ms": round(tip_speed_max, 1),
            "disk_loading_max_w_m2": round(disk_loading_max, 1),
            "v_stall_ms": round(v_stall, 1),
            "range_est_km": round(range_est_km, 1),
            "n_props": n,
            "prop_diameter_m": round(ac.propulsion.propellers[0].diameter_m, 3) if ac.propulsion.propellers else None,
            "prop_rpm": ac.propulsion.propellers[0].rpm if ac.propulsion.propellers else None,
        }

        return PrefilterResult(feasible=len(failures) == 0,
                               hard_failures=failures, warnings=warnings,
                               metrics=metrics)

    def _estimate_range_km(self, ac: Aircraft, w: float, ld_cr: float,
                           eta_prop: float, rho: float, v: float) -> float:
        """电推进 Breguet 航程初估（纯估算，供软约束告警）。"""
        battery = ac.propulsion.battery
        e_batt_j = battery.energy_j * battery.usable_fraction
        eta_total = eta_prop * self.sizing["motor_efficiency"] * self.sizing["esc_efficiency"]
        range_m = e_batt_j * eta_total * max(ld_cr, 1.0) / w if w > 0 else 0.0
        return range_m / 1000.0


def make_prefilter_check(sizing: dict, config: dict | None = None):
    """构建采样用可行性预筛回调 design_dict -> bool（供 DesignSampler 的 feasibility_check）。

    采样器产出的是混合 design dict（含 mission + geometry + propulsion 字段），
    此处拆分为 mission 与 design_cfg 后调用预筛。
    """
    from src.sampling.design_sampler import design_to_specs, specs_to_mission_dict

    prefilter = FeasibilityPrefilter(sizing, config)

    def check(design: dict) -> bool:
        specs = design_to_specs(design)
        mission = MissionSpec.from_dict(specs_to_mission_dict(specs["mission"]))
        design_cfg = dict(specs["geometry"])
        design_cfg.update(specs["propulsion"])
        return prefilter.check(mission, design_cfg).feasible

    return check
