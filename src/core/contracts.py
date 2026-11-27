"""统一数据契约：模块间只通过这些对象通信。

Input → Process → Output 模式：
  Mission (YAML) → Geometry → Aircraft → OpenVSPAdapter → vsp3
  vsp3 → VSPAeroAdapter → AeroResult
  翼型 .dat → XFOILAdapter → AirfoilPolar
  AeroResult + AirfoilPolar + Mission → PerformanceEvaluator → PerformanceResult
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ============================================================
# 任务需求（M01/M10 输出）
# ============================================================
@dataclass
class MissionSpec:
    """任务需求。所有单位：SI（km, m/s, kg, h, m）。"""
    mission_name: str = "unnamed_mission"
    range_km: float = 100.0
    cruise_speed_ms: float = 30.0
    payload_kg: float = 2.0
    mtow_kg: Optional[float] = None        # 可选，None 时由重量迭代估算
    endurance_h: float = 2.0
    altitude_m: float = 500.0
    # 约束
    stall_speed_max_ms: Optional[float] = None
    static_margin_min: float = 0.05
    static_margin_max: float = 0.15
    climb_rate_min_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MissionSpec":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})


# ============================================================
# 几何参数（M02 输出）
# ============================================================
@dataclass
class WingParams:
    """机翼。面积/展长为全机值（左右合计）。"""
    span_m: float = 3.0
    area_m2: float = 1.2
    aspect_ratio: float = 7.5
    taper_ratio: float = 0.5
    sweep_quarter_chord_deg: float = 2.0
    dihedral_deg: float = 3.0
    twist_tip_deg: float = -2.0
    airfoil_root: str = "clarky"
    airfoil_tip: str = "clarky"

    @property
    def mean_chord_m(self) -> float:
        return self.area_m2 / self.span_m

    @property
    def root_chord_m(self) -> float:
        """梯形翼几何平均弦 c̄=(cr+ct)/2 → cr = 2c̄/(1+λ)。"""
        return 2.0 * self.mean_chord_m / (1.0 + self.taper_ratio)

    @property
    def tip_chord_m(self) -> float:
        return self.root_chord_m * self.taper_ratio


@dataclass
class TailParams:
    """尾翼（平尾/垂尾）。area 为全平面积（平尾左右合计；垂尾单片）。"""
    area_m2: float = 0.24
    aspect_ratio: float = 4.0
    taper_ratio: float = 0.6
    sweep_quarter_chord_deg: float = 5.0
    arm_m: float = 1.4            # 重心到尾翼压心距离
    airfoil: str = "naca0008"     # 尾翼常对称翼型；若库中无则回退根翼型

    @property
    def span_m(self) -> float:
        return (self.area_m2 * self.aspect_ratio) ** 0.5

    @property
    def mean_chord_m(self) -> float:
        return self.area_m2 / self.span_m


@dataclass
class FuselageParams:
    length_m: float = 2.0
    max_diameter_m: float = 0.35


@dataclass
class PropellerParams:
    """单个螺旋桨设计变量（一级设计变量，独立于机身几何）。"""
    diameter_m: float = 0.40            # 直径
    pitch_m: float = 0.28               # 桨距（几何螺距）
    rpm: float = 6000.0                 # 设计转速
    ct: float = 0.0                     # 推力系数（0 → 用快速模型估算）
    cp: float = 0.0                     # 功率系数（0 → 用快速模型估算）
    # 安装位置（相对机身头部，X 向后 / Y 右展 / Z 上）
    x_m: float = 0.0
    y_m: float = 0.0
    z_m: float = 0.0
    # 安装形式
    location: str = "nose"              # nose | tail | wing | distributed
    n_blades: int = 2


@dataclass
class MotorParams:
    """电机。"""
    max_power_kw: float = 1.5
    efficiency: float = 0.85
    specific_power_kw_kg: float = 8.0
    kv: float = 500.0                   # 转速常数（RPM/V）


@dataclass
class BatteryParams:
    """电池。"""
    voltage_v: float = 22.2
    capacity_ah: float = 20.0
    specific_energy_wh_kg: float = 220.0
    mass_kg: float = 5.0
    usable_fraction: float = 0.85

    @property
    def energy_wh(self) -> float:
        return self.voltage_v * self.capacity_ah

    @property
    def energy_j(self) -> float:
        return self.energy_wh * 3600.0


@dataclass
class PropulsionParams:
    """推进系统（1~4 桨 + 电机 + 电池）。"""
    n_props: int = 1
    propellers: list = field(default_factory=list)   # list[PropellerParams]
    motor: MotorParams = field(default_factory=MotorParams)
    battery: BatteryParams = field(default_factory=BatteryParams)

    # ---- 兼容 Phase 1 旧字段（保持向后兼容）----
    prop_diameter_m: float = 0.4
    motor_power_kw: float = 1.5          # 单电机最大功率
    battery_energy_wh: float = 1000.0
    battery_mass_kg: float = 5.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Aircraft:
    """标准几何对象（M02 输出，M04/M06 消费）。"""
    wing: WingParams = field(default_factory=WingParams)
    htail: TailParams = field(default_factory=TailParams)
    vtail: TailParams = field(default_factory=TailParams)
    fuselage: FuselageParams = field(default_factory=FuselageParams)
    propulsion: PropulsionParams = field(default_factory=PropulsionParams)
    # 重量
    mtow_kg: float = 15.0
    empty_mass_kg: float = 8.0
    payload_kg: float = 2.0
    battery_mass_kg: float = 5.0
    # 位置（VSP 坐标系: X 向后, Y 右展, Z 上）
    wing_x_m: float = 0.4               # 机翼根前缘距机身头部
    htail_x_m: float = 1.8
    vtail_x_m: float = 1.8
    htail_z_m: float = 0.05
    cg_x_m: float = 0.7                 # 重心位置（距机身头部）
    # 派生
    @property
    def wing_loading_kg_m2(self) -> float:
        return self.mtow_kg / self.wing.area_m2

    @property
    def power_loading_w_kg(self) -> float:
        return self.propulsion.n_props * self.propulsion.motor_power_kw * 1000.0 / self.mtow_kg

    @property
    def htail_volume_coeff(self) -> float:
        return (self.htail.area_m2 * self.htail.arm_m) / (self.wing.area_m2 * self.wing.mean_chord_m)

    @property
    def vtail_volume_coeff(self) -> float:
        return (self.vtail.area_m2 * self.vtail.arm_m) / (self.wing.area_m2 * self.wing.span_m)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# 气动结果（M05 输出）
# ============================================================
@dataclass
class AirfoilPolar:
    """XFOIL 2D 极线。"""
    airfoil_name: str
    reynolds: float
    alpha_deg: list = field(default_factory=list)
    CL: list = field(default_factory=list)
    CD: list = field(default_factory=list)
    CM: list = field(default_factory=list)
    cl_max: float = 0.0
    alpha_stall_deg: float = 0.0
    converged: bool = True


@dataclass
class AeroResult:
    """VSPAero 整机气动数据库（极曲线）。"""
    alpha_deg: list = field(default_factory=list)
    CL: list = field(default_factory=list)
    CD: list = field(default_factory=list)
    Cm: list = field(default_factory=list)
    # 拟合参数: CD = CD0 + k*CL^2
    CD0: float = 0.03
    k_induced: float = 0.07
    # 阻力分解（Phase 1.5: CD_total = CD_induced + CD_parasitic + CD_wave）
    CD0_parasitic: float = 0.0          # 寄生阻力零升分量（ParasiteDragModel）
    CD0_vlm: float = 0.03               # VLM 升力面拟合零升阻力
    CD0_wave: float = 0.0               # 波阻（跨声速才显著，亚声速≈0）
    CL_alpha_per_rad: float = 4.5
    CL_alpha_0deg: float = 0.0
    CL_max: float = 1.1
    LD_max: float = 12.0
    alpha_LDmax_deg: float = 5.0
    Cm_alpha_per_rad: float = -1.0   # 俯仰力矩对攻角导数（关于设定参考点）
    reference_x_m: float = 0.0       # 力矩参考点（距机身头部）
    # 配平前原始稳定性信号（Phase 2.5：重心配平前保留，不被 _trim_cg 覆盖）
    Cm_alpha_pre_trim_per_rad: float = -1.0   # 配平前俯仰力矩斜率
    neutral_point_x_pre_trim_m: float = 0.0   # 中性点位置（物理不变量）
    cg_x_pre_trim_m: float = 0.0              # 配平前重心位置
    cg_shift_m: float = 0.0                   # 重心配平调整量
    static_margin_target: float = 0.10        # 配平目标静稳定裕度
    raw_polar_file: str = ""

    @property
    def oswald_efficiency(self) -> float:
        # 需配合机翼 AR 使用: e = 1/(k*pi*AR)
        return 0.85

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# 性能结果（M06 输出）
# ============================================================
@dataclass
class PerformanceResult:
    """飞行性能。"""
    # 失速
    v_stall_ms: float = 16.0
    v_cruise_ms: float = 30.0
    # 巡航状态
    alpha_cruise_deg: float = 3.0
    CL_cruise: float = 0.55
    CD_cruise: float = 0.05
    LD_cruise: float = 11.0
    LD_max: float = 12.0
    LD_max_total: float = 12.0   # 含寄生阻力的最大升阻比（Phase 2.5 C3 D4；VLM 的 LD_max 仅含诱导阻力）
    drag_cruise_n: float = 15.0
    power_cruise_w: float = 800.0
    # 阻力分解（Phase 1.5）
    cd_induced_cruise: float = 0.0
    cd_parasitic_cruise: float = 0.0
    cd_wave_cruise: float = 0.0
    # 推进（Phase 1.5: PropulsionModel）
    thrust_required_n: float = 0.0
    thrust_available_n: float = 0.0
    thrust_weight_ratio: float = 0.35
    power_available_w: float = 1500.0
    excess_power_w: float = 700.0
    motor_efficiency: float = 0.85
    propeller_efficiency: float = 0.70
    # 推进转速（Phase 2.5：设计指定/求解/实际工作转速，保证记录与物理计算一致）
    prop_design_rpm: float = 0.0
    prop_solve_rpm: float = 0.0
    prop_operating_rpm: float = 0.0
    # 桨尖速度（Phase 2.5 C3 D3：区分设计转速 vs 实际工作转速，避免标签误导）
    tip_speed_design_ms: float = 0.0     # π·prop_design_rpm/60·D（设计转速下的桨尖速度）
    tip_speed_operating_ms: float = 0.0  # π·prop_operating_rpm/60·D（实际工作桨尖速度）
    # 航程/续航
    range_km: float = 100.0
    range_margin_km: float = 0.0     # 预测航程 - 任务要求航程（Phase 2.5）
    endurance_h: float = 2.0
    # 爬升
    climb_rate_ms: float = 3.0
    # 稳定性（Phase 2.5：配平前原始信号 + 配平后 + 调整量，均保留）
    static_margin: float = 0.10
    neutral_point_x_m: float = 0.75
    cg_x_m: float = 0.70
    static_margin_pre_trim: float = 0.10   # 配平前静稳定裕度
    neutral_point_x_pre_trim_m: float = 0.0
    cg_x_pre_trim_m: float = 0.0
    cg_shift_m: float = 0.0                # 重心配平调整量
    static_margin_target: float = 0.10     # 配平目标裕度（人为设定，非物理结果）
    htail_volume_coeff: float = 0.50
    vtail_volume_coeff: float = 0.040
    # 达标判定
    constraints_pass: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
