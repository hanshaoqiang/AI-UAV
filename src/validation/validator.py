"""Phase 2: DataValidator — 数据质量控制。

对单个设计分析结果做后置校验，过滤异常样本，保证数据库质量。

校验类别（阈值见 config/validation.yaml，禁止硬编码）：
  1. sanity     数值有效性（NaN/inf 预扫描）
  2. geometry   几何可行性
  3. weight     重量合理性
  4. stability  静稳定性
  5. propulsion 推进能力
  6. range      航程约束（依赖任务需求，未达标为 warning）

严重级别：
  error   = 物理异常（NaN/负值/不可行几何/静不稳定/无推力等）→ 过滤
  warning = 未达任务约束（航程/续航/失速/爬升低于目标）→ 标记不过滤

输入：pipeline 结果 dict（含 aircraft/aero/performance/airfoil_polar，可选 mission）
输出：ValidationResult（valid + checks 明细）。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("uavdp.validation")

# 默认校验阈值定义文件（Phase 2 起：阈值由 YAML 驱动，禁止硬编码）
DEFAULT_VALIDATION_PATH = Path(__file__).resolve().parents[2] / "config" / "validation.yaml"

# 校验类别（用于报告分组与统计）
CATEGORIES = ("sanity", "geometry", "weight", "stability", "propulsion", "range")


@dataclass
class CheckResult:
    """单条校验结论。"""
    category: str                 # sanity|geometry|weight|stability|propulsion|range
    name: str                     # 校验项名称
    passed: bool
    severity: str = "error"       # error|warning
    detail: str = ""
    value: Any = None             # 实测值（可选，用于报告）
    limit: str = ""               # 阈值描述（可选）

    def to_dict(self) -> dict:
        return {
            "category": self.category, "name": self.name, "passed": self.passed,
            "severity": self.severity, "detail": self.detail,
            "value": self.value, "limit": self.limit,
        }


@dataclass
class ValidationResult:
    """一次校验的完整结果。valid = 无 error 级失败。"""
    valid: bool
    checks: list = field(default_factory=list)   # list[CheckResult]

    # ---------------- 便捷访问 ----------------
    @property
    def errors(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    @property
    def n_checks(self) -> int:
        return len(self.checks)

    def failed_names(self, severity: str | None = None) -> list[str]:
        """返回未通过校验项的名称列表（可按严重级别过滤）。"""
        items = self.errors if severity == "error" else (
            self.warnings if severity == "warning" else self.checks)
        return [c.name for c in items if not c.passed]

    def summary(self) -> dict:
        """汇总统计（供报告/入库）。"""
        cat = {}
        for name in CATEGORIES:
            items = [c for c in self.checks if c.category == name]
            cat[name] = {
                "errors": sum(1 for c in items if not c.passed and c.severity == "error"),
                "warnings": sum(1 for c in items if not c.passed and c.severity == "warning"),
            }
        return {
            "valid": self.valid,
            "n_checks": self.n_checks,
            "n_errors": len(self.errors),
            "n_warnings": len(self.warnings),
            "categories": cat,
        }

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "checks": [c.to_dict() for c in self.checks],
        }


def _builtin_validation_config() -> dict:
    """内置兜底阈值（仅当 YAML 缺失/损坏时使用，结构与 validation.yaml 一致）。"""
    return {
        "geometry": {
            "span_m_min": 0.5, "aspect_ratio": [4.0, 20.0], "taper_ratio": [0.2, 1.0],
            "htail_area_ratio": [0.10, 0.35], "vtail_area_ratio": [0.03, 0.35],
            "vh": [0.30, 0.80], "vv": [0.02, 0.08], "tail_arm_m_min": 0.2,
            "fuselage_fineness": [4.0, 12.0], "wing_loading_kgm2_min": 3.0,
        },
        "weight": {
            "mtow_kg": [0.5, 5000.0], "empty_mass_fraction": [0.10, 0.80],
            "battery_mass_fraction_max": 0.60, "payload_fraction_max": 0.60,
        },
        "stability": {"static_margin": [0.0, 0.40], "cm_alpha_per_rad_max": 0.0},
        "propulsion": {
            "thrust_weight_ratio_min": 0.08, "excess_power_w_min": 0.0,
            "propeller_efficiency": [0.1, 1.0], "motor_efficiency": [0.5, 1.0],
        },
        "aerodynamics": {
            "ld_cruise_min": 1.0, "ld_max": [3.0, 60.0],
            "cl_max": [0.4, 1.8], "cl_alpha_per_rad_min": 1.0,
        },
        "range": {"range_ratio_min": 1.0, "endurance_ratio_min": 1.0},
    }


def load_validation_config(path: str | Path) -> dict:
    """从 YAML 文件加载校验阈值。"""
    import yaml
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"校验阈值文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"校验阈值顶层必须是映射: {p}")
    return data


def default_validation_config() -> dict:
    """默认阈值：从 config/validation.yaml 加载；失败时回退内置阈值。"""
    if DEFAULT_VALIDATION_PATH.exists():
        try:
            return load_validation_config(DEFAULT_VALIDATION_PATH)
        except Exception as e:
            logger.warning("校验阈值 YAML 加载失败(%s)，回退内置阈值", e)
    return _builtin_validation_config()


def _is_bad(v: Any) -> bool:
    """判断数值是否为 None/NaN/inf。"""
    if v is None:
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        try:
            f = float(v)
            return math.isnan(f) or math.isinf(f)
        except (TypeError, ValueError):
            return False
    return False


def _flatten_numeric(obj: Any, prefix: str = "") -> dict:
    """递归拍平对象，仅保留数值标量（跳过 list/bool/str）。"""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                out.update(_flatten_numeric(v, f"{prefix}{k}."))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out[f"{prefix}{k}"] = v
    return out


class DataValidator:
    """数据质量控制校验器。

    用法：
      validator = DataValidator()                      # 使用默认阈值 YAML
      result = validator.validate(mission, aircraft, aero, performance)
      或
      result = validator.validate_result(pipeline_result_dict)
      valid, rejected = validator.filter(list_of_results)
    """

    def __init__(self, config: dict | None = None):
        self.cfg = config if config is not None else default_validation_config()
        self._checks: list[CheckResult] = []

    # ---------------- 入口 ----------------
    def validate(self, mission=None, aircraft=None, aero=None,
                 performance=None, polar=None) -> ValidationResult:
        """执行全部校验。对象为 None 时跳过对应类别。"""
        self._checks = []

        self._check_sanity(aircraft, aero, performance)
        if aircraft is not None:
            self._check_geometry(aircraft)
            self._check_weight(aircraft)
        if performance is not None:
            self._check_stability(performance, aero)
            self._check_propulsion(performance)
            self._check_aero(performance, aero)
            self._check_range(performance, mission)

        res = ValidationResult(valid=True, checks=list(self._checks))
        res.valid = len(res.errors) == 0
        logger.info("数据校验: valid=%s, %d 项检查, %d error, %d warning",
                    res.valid, res.n_checks, len(res.errors), len(res.warnings))
        return res

    def validate_result(self, result: dict, mission=None) -> ValidationResult:
        """从 pipeline 结果 dict 校验（键：aircraft/aero/performance/airfoil_polar/mission）。"""
        return self.validate(
            mission=mission if mission is not None else result.get("mission"),
            aircraft=result.get("aircraft"),
            aero=result.get("aero"),
            performance=result.get("performance"),
            polar=result.get("airfoil_polar") or result.get("polar"),
        )

    def is_valid(self, result: dict, mission=None, strict: bool = False) -> bool:
        """判断单组结果是否有效。strict=True 时连 warning 也视为无效。"""
        res = self.validate_result(result, mission=mission)
        if strict:
            return res.valid and len(res.warnings) == 0
        return res.valid

    def filter(self, results: list[dict], mission=None, strict: bool = False) -> tuple:
        """过滤结果列表。返回 (有效结果, 被过滤结果) 两个列表。"""
        valid, rejected = [], []
        for r in results:
            (valid if self.is_valid(r, mission=mission, strict=strict)
             else rejected).append(r)
        return valid, rejected

    # ---------------- 校验实现 ----------------
    def _check(self, category: str, name: str, passed: bool, severity: str,
               detail: str, value: Any = None, limit: str = "") -> None:
        self._checks.append(CheckResult(
            category=category, name=name, passed=passed, severity=severity,
            detail=detail, value=value, limit=limit))

    def _check_sanity(self, aircraft, aero, performance) -> None:
        """NaN/inf 预扫描（数值有效性）。"""
        bad_fields = []
        for label, obj in (("aircraft", aircraft), ("aero", aero), ("performance", performance)):
            if obj is None:
                continue
            try:
                flat = _flatten_numeric(obj.to_dict())
            except Exception:
                flat = {}
            for k, v in flat.items():
                if _is_bad(v):
                    bad_fields.append(f"{label}.{k}")
        if bad_fields:
            self._check("sanity", "数值有效性", False, "error",
                        f"存在 NaN/inf 字段: {', '.join(bad_fields[:8])}"
                        f"{'...' if len(bad_fields) > 8 else ''}",
                        value=len(bad_fields))
        else:
            self._check("sanity", "数值有效性", True, "error", "全部数值有效")

    def _check_geometry(self, ac) -> None:
        g = self.cfg["geometry"]
        w, h, v, f = ac.wing, ac.htail, ac.vtail, ac.fuselage

        self._check("geometry", "翼展>下限", w.span_m > g["span_m_min"], "error",
                    f"b={w.span_m:.2f} m", w.span_m, f"> {g['span_m_min']} m")
        self._in_range("geometry", "展弦比", w.aspect_ratio, g["aspect_ratio"], "error")
        self._in_range("geometry", "锥比", w.taper_ratio, g["taper_ratio"], "error")
        # 根/梢弦为派生量，须为正且根≥梢（锥比≤1）
        root_ok = not _is_bad(w.root_chord_m) and w.root_chord_m > 0
        tip_ok = not _is_bad(w.tip_chord_m) and w.tip_chord_m > 0
        self._check("geometry", "根梢弦为正且根≥梢",
                    root_ok and tip_ok and w.root_chord_m >= w.tip_chord_m - 1e-9, "error",
                    f"cr={w.root_chord_m:.3f} ct={w.tip_chord_m:.3f}",
                    (w.root_chord_m, w.tip_chord_m), "cr ≥ ct > 0")
        self._in_range("geometry", "平尾面积比", h.area_m2 / max(w.area_m2, 1e-9),
                       g["htail_area_ratio"], "error")
        self._in_range("geometry", "垂尾面积比", v.area_m2 / max(w.area_m2, 1e-9),
                       g["vtail_area_ratio"], "error")
        self._in_range("geometry", "平尾容量Vh", ac.htail_volume_coeff, g["vh"], "error")
        self._in_range("geometry", "垂尾容量Vv", ac.vtail_volume_coeff, g["vv"], "error")
        self._check("geometry", "尾臂为正", h.arm_m > g["tail_arm_m_min"]
                    and v.arm_m > g["tail_arm_m_min"], "error",
                    f"lh={h.arm_m:.2f} lv={v.arm_m:.2f}", (h.arm_m, v.arm_m),
                    f"> {g['tail_arm_m_min']} m")
        self._in_range("geometry", "机身长径比",
                       f.length_m / max(f.max_diameter_m, 1e-9),
                       g["fuselage_fineness"], "error")
        self._check("geometry", "翼载为正", ac.wing_loading_kg_m2 > g["wing_loading_kgm2_min"],
                    "error", f"W/S={ac.wing_loading_kg_m2:.2f} kg/m²",
                    ac.wing_loading_kg_m2, f"> {g['wing_loading_kgm2_min']} kg/m²")

    def _check_weight(self, ac) -> None:
        wgt = self.cfg["weight"]
        mtow = ac.mtow_kg
        self._in_range("weight", "最大起飞重量", mtow, wgt["mtow_kg"], "error")
        empty_frac = ac.empty_mass_kg / mtow if mtow > 0 else float("nan")
        self._in_range("weight", "空重比", empty_frac, wgt["empty_mass_fraction"], "error")
        batt_frac = ac.battery_mass_kg / mtow if mtow > 0 else float("nan")
        self._check("weight", "电池质量占比", batt_frac <= wgt["battery_mass_fraction_max"],
                    "error", f"{batt_frac:.2f}", batt_frac,
                    f"≤ {wgt['battery_mass_fraction_max']}")
        pay_frac = ac.payload_kg / mtow if mtow > 0 else float("nan")
        self._check("weight", "载荷占比", pay_frac <= wgt["payload_fraction_max"],
                    "error", f"{pay_frac:.2f}", pay_frac,
                    f"≤ {wgt['payload_fraction_max']}")

    def _check_stability(self, perf, aero) -> None:
        st = self.cfg["stability"]
        self._in_range("stability", "静稳定裕度", perf.static_margin,
                       st["static_margin"], "error")
        if aero is not None:
            cm_a = aero.Cm_alpha_per_rad
            self._check("stability", "俯仰力矩斜率(负)", not _is_bad(cm_a)
                        and cm_a < st["cm_alpha_per_rad_max"], "error",
                        f"Cmα={cm_a:.3f}/rad", cm_a, "< 0")
        np_ok = not _is_bad(perf.neutral_point_x_m) and \
            not _is_bad(perf.cg_x_m) and perf.neutral_point_x_m > perf.cg_x_m
        self._check("stability", "中性点在重心之后", np_ok, "error",
                    f"NP={perf.neutral_point_x_m:.3f} CG={perf.cg_x_m:.3f}",
                    (perf.neutral_point_x_m, perf.cg_x_m), "NP > CG")

    def _check_propulsion(self, perf) -> None:
        pr = self.cfg["propulsion"]
        self._check("propulsion", "推重比", perf.thrust_weight_ratio >= pr["thrust_weight_ratio_min"],
                    "error", f"T/W={perf.thrust_weight_ratio:.3f}",
                    perf.thrust_weight_ratio, f"≥ {pr['thrust_weight_ratio_min']}")
        self._check("propulsion", "剩余功率", perf.excess_power_w >= pr["excess_power_w_min"],
                    "error", f"ΔP={perf.excess_power_w:.0f} W",
                    perf.excess_power_w, f"≥ {pr['excess_power_w_min']} W")
        self._in_range("propulsion", "螺旋桨效率", perf.propeller_efficiency,
                       pr["propeller_efficiency"], "error")
        self._in_range("propulsion", "电机效率", perf.motor_efficiency,
                       pr["motor_efficiency"], "error")

    def _check_aero(self, perf, aero) -> None:
        a = self.cfg["aerodynamics"]
        self._check("aerodynamics", "巡航升阻比", perf.LD_cruise >= a["ld_cruise_min"],
                    "error", f"L/D_cr={perf.LD_cruise:.1f}", perf.LD_cruise,
                    f"≥ {a['ld_cruise_min']}")
        self._in_range("aerodynamics", "最大升阻比", perf.LD_max, a["ld_max"], "error")
        if aero is not None:
            self._in_range("aerodynamics", "最大升力系数", aero.CL_max, a["cl_max"], "error")
            self._check("aerodynamics", "升力线斜率", aero.CL_alpha_per_rad >= a["cl_alpha_per_rad_min"],
                        "error", f"CLα={aero.CL_alpha_per_rad:.2f}/rad",
                        aero.CL_alpha_per_rad, f"≥ {a['cl_alpha_per_rad_min']}")

    def _check_range(self, perf, mission) -> None:
        """航程约束。负/零航程为异常(error)；未达任务目标为 warning（仅当有任务）。"""
        r = self.cfg["range"]
        self._check("range", "航程为正", not _is_bad(perf.range_km) and perf.range_km > 0,
                    "error", f"R={perf.range_km:.1f} km", perf.range_km, "> 0")
        self._check("range", "续航为正", not _is_bad(perf.endurance_h) and perf.endurance_h > 0,
                    "error", f"E={perf.endurance_h:.2f} h", perf.endurance_h, "> 0")

        if mission is None:
            return
        if mission.range_km:
            ratio = perf.range_km / mission.range_km
            self._check("range", "航程达标", ratio >= r["range_ratio_min"], "warning",
                        f"R={perf.range_km:.1f} km (目标{mission.range_km:.0f} km)",
                        ratio, f"≥ {r['range_ratio_min']}")
        if mission.endurance_h:
            ratio = perf.endurance_h / mission.endurance_h
            self._check("range", "续航达标", ratio >= r["endurance_ratio_min"], "warning",
                        f"E={perf.endurance_h:.2f} h (目标{mission.endurance_h:.1f} h)",
                        ratio, f"≥ {r['endurance_ratio_min']}")
        if mission.stall_speed_max_ms:
            self._check("range", "失速速度约束",
                        perf.v_stall_ms <= mission.stall_speed_max_ms, "warning",
                        f"v_stall={perf.v_stall_ms:.1f} m/s (限{mission.stall_speed_max_ms:.1f})",
                        perf.v_stall_ms, f"≤ {mission.stall_speed_max_ms}")
        if mission.climb_rate_min_ms:
            self._check("range", "爬升率约束",
                        perf.climb_rate_ms >= mission.climb_rate_min_ms, "warning",
                        f"ROC={perf.climb_rate_ms:.1f} m/s (目标{mission.climb_rate_min_ms:.1f})",
                        perf.climb_rate_ms, f"≥ {mission.climb_rate_min_ms}")

    # ---------------- 工具 ----------------
    def _in_range(self, category: str, name: str, value: float,
                  bounds: list, severity: str) -> None:
        lo, hi = float(bounds[0]), float(bounds[1])
        ok = (not _is_bad(value)) and lo <= value <= hi
        self._check(category, name, ok, severity,
                    f"{value if _is_bad(value) else round(value, 4)} ∈ [{lo}, {hi}]",
                    value, f"[{lo}, {hi}]")
