"""Phase 2: DesignSampler — 设计变量采样器。

支持：
  - Random Sampling（随机采样）
  - Latin Hypercube Sampling（拉丁超立方采样）
  - Sobol Sampling（低差异序列，scipy）

输入：设计变量范围（连续区间 + 离散集合），可由 YAML 加载（禁止硬编码）。
输出：标准化 Mission / Geometry / Propulsion 参数字典（可序列化为 YAML）。

设计变量（Phase 2 覆盖，见 config/design_space.yaml）：
  Mission:    range_km, cruise_speed_ms, payload_kg, endurance_h, altitude_m
  Wing:       aspect_ratio, taper_ratio, sweep, dihedral, twist, 安装位置, airfoil
  Tail:       体积系数, 展弦比, 锥比, 后掠
  Propulsion: n_props(离散), prop_layout(离散), prop_diameter_m, prop_rpm
  根弦/梢弦/前后缘后掠为派生量（由面积+展弦比+锥比+后掠唯一确定）。
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("uavdp.sampling")

# 默认设计空间定义文件（Phase 2 起：设计空间由 YAML 驱动，禁止硬编码）
DEFAULT_SPACE_PATH = Path(__file__).resolve().parents[2] / "config" / "design_space.yaml"


@dataclass
class ContinuousVar:
    """连续设计变量（区间）。"""
    name: str
    lo: float
    hi: float
    log_scale: bool = False    # True → 对数均匀采样


@dataclass
class DiscreteVar:
    """离散设计变量（取值集合）。"""
    name: str
    choices: list


@dataclass
class DesignSpace:
    """设计空间定义。"""
    continuous: list = field(default_factory=list)   # list[ContinuousVar]
    discrete: list = field(default_factory=list)     # list[DiscreteVar]

    def n_dims(self) -> int:
        return len(self.continuous) + len(self.discrete)

    def names(self) -> list[str]:
        return [v.name for v in self.continuous] + [v.name for v in self.discrete]

    # ---------------- 序列化（YAML 驱动，禁止硬编码） ----------------
    def to_dict(self) -> dict:
        """序列化为 {continuous: {name: {lo,hi,[log_scale]}}, discrete: {name: [...]}}。"""
        out: dict = {"continuous": {}, "discrete": {}}
        for v in self.continuous:
            spec: dict = {"lo": v.lo, "hi": v.hi}
            if v.log_scale:
                spec["log_scale"] = True
            out["continuous"][v.name] = spec
        for v in self.discrete:
            out["discrete"][v.name] = list(v.choices)
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "DesignSpace":
        """从字典解析设计空间（与 to_dict 互逆）。"""
        continuous = [
            ContinuousVar(name=name, lo=float(spec["lo"]), hi=float(spec["hi"]),
                          log_scale=bool(spec.get("log_scale", False)))
            for name, spec in (d.get("continuous") or {}).items()
        ]
        discrete = [
            DiscreteVar(name=name, choices=list(choices))
            for name, choices in (d.get("discrete") or {}).items()
        ]
        return cls(continuous=continuous, discrete=discrete)


def load_design_space(path: str | Path) -> DesignSpace:
    """从 YAML 文件加载设计空间定义。"""
    import yaml
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"设计空间定义文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"设计空间定义顶层必须是映射: {p}")
    return DesignSpace.from_dict(data)


def default_design_space() -> DesignSpace:
    """默认设计空间：从 config/design_space.yaml 加载；缺失/解析失败时回退内置空间。

    Phase 2 起设计空间由 YAML 驱动（禁止硬编码）；内置空间仅作兜底。
    """
    if DEFAULT_SPACE_PATH.exists():
        try:
            return load_design_space(DEFAULT_SPACE_PATH)
        except Exception as e:  # 配置损坏时降级，不阻塞
            logger.warning("设计空间 YAML 加载失败(%s)，回退内置空间", e)
    return _builtin_design_space()


def _builtin_design_space() -> DesignSpace:
    """内置兜底设计空间（Phase 1.5 范围，仅当 YAML 缺失时使用）。"""
    return DesignSpace(
        continuous=[
            ContinuousVar("range_km", 50.0, 300.0),
            ContinuousVar("cruise_speed_ms", 18.0, 40.0),
            ContinuousVar("payload_kg", 0.5, 10.0),
            ContinuousVar("endurance_h", 1.0, 2.5),       # 电推进(220Wh/kg)物理上限约2.5h
            ContinuousVar("altitude_m", 100.0, 2000.0),
            ContinuousVar("aspect_ratio", 6.5, 14.0),
            ContinuousVar("taper_ratio", 0.4, 0.8),
            ContinuousVar("htail_volume_coeff", 0.40, 0.65),
            ContinuousVar("vtail_volume_coeff", 0.030, 0.045),
        ],
        discrete=[
            DiscreteVar("n_props", [1, 2, 3, 4]),
            DiscreteVar("prop_layout", ["nose", "tail", "wing", "distributed"]),
        ],
    )


class DesignSampler:
    """从设计空间采样，产出标准化设计字典。"""

    def __init__(self, space: DesignSpace, seed: int | None = None):
        self.space = space
        self.rng = random.Random(seed)

    # ---------------- 采样方法 ----------------
    def random_sample(self, n: int, feasibility_check=None,
                      max_attempts: int = 10) -> list[dict]:
        """纯随机采样 n 组。

        feasibility_check: 可选回调 design_dict -> bool（Phase 2.5 预筛）；
        不可行的样本会被替换（重采样），保证输出 n 组可行设计。
        max_attempts: 每个槽位最多重采样次数（防死循环）。
        """
        samples = []
        for _ in range(n):
            sample = self._sample_one_random()
            if feasibility_check:
                attempts = 0
                while not feasibility_check(sample) and attempts < max_attempts:
                    sample = self._sample_one_random()
                    attempts += 1
            samples.append(sample)
        return samples

    def lhs_sample(self, n: int, feasibility_check=None,
                   max_attempts: int = 10) -> list[dict]:
        """拉丁超立方采样 n 组（连续维度分层，离散维度随机）。

        feasibility_check: 可选回调 design_dict -> bool，用于预筛可行设计；
        不可行的样本会被替换（重采样），保证输出 n 组可行设计。
        max_attempts: 每个槽位最多重采样次数（防死循环）。
        """
        cont = self.space.continuous
        d = len(cont)
        if d == 0:
            return self.random_sample(n)

        strata = []
        for v in cont:
            edges = [v.lo + (v.hi - v.lo) * i / n for i in range(n + 1)]
            levels = []
            for i in range(n):
                a, b = edges[i], edges[i + 1]
                levels.append(self.rng.uniform(a, b))
            self.rng.shuffle(levels)
            strata.append(levels)

        samples = []
        for i in range(n):
            cont_vals = [strata[j][i] for j in range(d)]
            sample = self._assemble(cont_vals)
            # 可行性预筛：不可行则重采样该槽位
            if feasibility_check:
                attempts = 0
                while not feasibility_check(sample) and attempts < max_attempts:
                    cont_vals = [self._sample_cont(v) for v in cont]
                    sample = self._assemble(cont_vals)
                    attempts += 1
            samples.append(sample)
        return samples

    def sobol_sample(self, n: int, feasibility_check=None,
                     max_attempts: int = 10) -> list[dict]:
        """Sobol 低差异序列采样 n 组（连续维度 Sobol，离散维度随机）。

        依赖 scipy.stats.qmc.Sobol（requirements.txt 已含 scipy）。
        scramble=True 保证每次按 self.rng 播种可复现。
        feasibility_check: 同 lhs_sample。
        """
        cont = self.space.continuous
        d = len(cont)
        if d == 0:
            return self.random_sample(n)
        from scipy.stats.qmc import Sobol

        seed = self.rng.randint(0, 2 ** 31 - 1)
        engine = Sobol(d=d, scramble=True, seed=seed)
        # n 非 2 的幂时 scipy 仅提示平衡性下降（点仍有效），此处对采样用途可忽略
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            unit = engine.random(n)  # (n, d) ∈ [0,1)

        samples = []
        for i in range(n):
            cont_vals = [self._scale_cont(v, float(unit[i][j]))
                         for j, v in enumerate(cont)]
            sample = self._assemble(cont_vals)
            if feasibility_check:
                attempts = 0
                while not feasibility_check(sample) and attempts < max_attempts:
                    cont_vals = [self._sample_cont(v) for v in cont]
                    sample = self._assemble(cont_vals)
                    attempts += 1
            samples.append(sample)
        return samples

    # ---------------- 内部 ----------------
    def _sample_one_random(self) -> dict:
        cont_vals = [self._sample_cont(v) for v in self.space.continuous]
        return self._assemble(cont_vals)

    def _scale_cont(self, v: ContinuousVar, u: float) -> float:
        """把 [0,1) 均匀数 u 映射到变量取值（线性 / 对数）。"""
        if v.log_scale:
            lo, hi = math.log(v.lo), math.log(v.hi)
            return math.exp(lo + u * (hi - lo))
        return v.lo + u * (v.hi - v.lo)

    def _sample_cont(self, v: ContinuousVar) -> float:
        return self._scale_cont(v, self.rng.random())

    def _assemble(self, cont_vals: list[float]) -> dict:
        """把连续取值 + 离散取值装配成标准设计字典。"""
        out = {}
        for v, val in zip(self.space.continuous, cont_vals):
            out[v.name] = val
        for v in self.space.discrete:
            out[v.name] = self.rng.choice(v.choices)
        return out


def design_to_specs(design: dict, base: dict | None = None) -> dict:
    """把采样得到的设计字典拆分为标准三段：
    Mission YAML + Geometry YAML + Propulsion YAML（均为 dict，可直接 yaml.dump）。

    base: 可选，缺省字段的兜底值。
    """
    base = base or {}

    mission = {
        "mission_name": design.get("mission_name", "sampled"),
        "range_km": design.get("range_km", base.get("range_km", 100.0)),
        "cruise_speed_ms": design.get("cruise_speed_ms", base.get("cruise_speed_ms", 30.0)),
        "payload_kg": design.get("payload_kg", base.get("payload_kg", 2.0)),
        "endurance_h": design.get("endurance_h", base.get("endurance_h", 2.0)),
        "altitude_m": design.get("altitude_m", base.get("altitude_m", 500.0)),
        "mtow_kg": design.get("mtow_kg"),
        "stall_speed_max_ms": design.get("stall_speed_max_ms"),
        "static_margin_min": design.get("static_margin_min", 0.05),
        "static_margin_max": design.get("static_margin_max", 0.15),
        "climb_rate_min_ms": design.get("climb_rate_min_ms"),
    }

    geometry = {
        "aspect_ratio": design.get("aspect_ratio", base.get("aspect_ratio", 8.0)),
        "taper_ratio": design.get("taper_ratio", base.get("taper_ratio", 0.5)),
        "airfoil_root": design.get("airfoil_root", base.get("airfoil_root", "clarky")),
        "airfoil_tip": design.get("airfoil_tip", base.get("airfoil_tip", "clarky")),
        "wing_sweep_deg": design.get("wing_sweep_deg", base.get("wing_sweep_deg", 2.0)),
        "wing_dihedral_deg": design.get("wing_dihedral_deg", base.get("wing_dihedral_deg", 3.0)),
        "wing_twist_deg": design.get("wing_twist_deg", base.get("wing_twist_deg", -2.0)),
        "wing_x_fraction": design.get("wing_x_fraction", base.get("wing_x_fraction", 0.30)),
        "htail_volume_coeff": design.get("htail_volume_coeff", base.get("htail_volume_coeff", 0.50)),
        "vtail_volume_coeff": design.get("vtail_volume_coeff", base.get("vtail_volume_coeff", 0.040)),
        "htail_aspect_ratio": design.get("htail_aspect_ratio", base.get("htail_aspect_ratio", 4.0)),
        "htail_taper_ratio": design.get("htail_taper_ratio", base.get("htail_taper_ratio", 0.6)),
        "htail_sweep_deg": design.get("htail_sweep_deg", base.get("htail_sweep_deg", 5.0)),
        "vtail_aspect_ratio": design.get("vtail_aspect_ratio", base.get("vtail_aspect_ratio", 1.8)),
        "vtail_taper_ratio": design.get("vtail_taper_ratio", base.get("vtail_taper_ratio", 0.6)),
        "vtail_sweep_deg": design.get("vtail_sweep_deg", base.get("vtail_sweep_deg", 12.0)),
    }

    propulsion = {
        "n_props": design.get("n_props", base.get("n_props", 1)),
        "prop_layout": design.get("prop_layout", base.get("prop_layout", "nose")),
        # Phase 2.5 BEMT：prop_diameter_factor 为直径缩放系数（相对功率反推直径）。
        # prop_diameter_m（绝对直径）保留为手动/demo 覆盖项，采样不产出。
        "prop_diameter_factor": design.get("prop_diameter_factor",
                                           base.get("prop_diameter_factor")),
        "prop_diameter_m": design.get("prop_diameter_m", base.get("prop_diameter_m")),
        "prop_rpm": design.get("prop_rpm", base.get("prop_rpm", 6000.0)),
    }

    return {"mission": mission, "geometry": geometry, "propulsion": propulsion}


def specs_to_mission_dict(mission: dict) -> dict:
    """把 mission 段转为 MissionSpec 可接受的 dict（过滤未知键）。"""
    from src.core.contracts import MissionSpec
    valid = {f.name for f in MissionSpec.__dataclass_fields__.values()}
    return {k: v for k, v in mission.items() if k in valid}
