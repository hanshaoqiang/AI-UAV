"""Phase 1.5: PropulsionModel — 电推进系统快速模型。

独立接口，不修改 Geometry 模块。Propeller 作为一级设计变量。

模型范围：
  1. 螺旋桨布局（数量 1~4，机头/机尾/翼上/分布式）
  2. 推力模型（Phase 2.5 起：BEMT 叶素动量理论，截面翼型极线来自 XFOIL 查找表；
     遗留 `propeller_thrust_power` 动量理论仅作 fallback/对比）
  3. 功率模型（轴功率 → 电功率）
  4. 电机效率
  5. 电池能量模型

坐标约定：X 向后，Y 右展，Z 上，原点=机身头部。
"""
from __future__ import annotations

import logging
import math

from src.core.contracts import (Aircraft, BatteryParams, MotorParams,
                                PropellerParams)
from src.propulsion.bemt import BEMTSolver, load_polar_table

logger = logging.getLogger("uavdp.propulsion")

RHO0 = 1.225        # 海平面空气密度 kg/m³


# ============================================================
# 螺旋桨快速模型（动量理论 + 经验修正）
# ============================================================
def propeller_thrust_power(
    diameter_m: float, pitch_m: float, rpm: float,
    velocity_ms: float, rho: float, n_blades: int = 2,
    ct: float = 0.0, cp: float = 0.0,
) -> tuple[float, float, float]:
    """返回 (推力 N, 轴功率 W, 螺旋桨效率)。

    若给定 CT/CP（>0）则直接用系数法；否则用动量理论快速估算。
    """
    area = math.pi * (diameter_m / 2.0) ** 2
    n_rps = rpm / 60.0
    d = diameter_m
    if n_rps <= 0 or d <= 0:
        return 0.0, 0.0, 0.0

    advance_ratio_j = velocity_ms / (n_rps * d) if n_rps * d > 0 else 0.0

    if ct > 0 and cp > 0:
        # 系数法：T = CT·ρ·n²·D⁴, P = CP·ρ·n³·D⁵
        thrust = ct * rho * n_rps ** 2 * d ** 4
        power = cp * rho * n_rps ** 3 * d ** 5
    else:
        # 动量理论：理想桨盘效率 η_ideal = 2/(1+sqrt(1+T/(0.5ρAV²)))
        # 用经验桨距估计：先按螺距几何关系估前进比→推力系数
        # 简化：理想推力 T = 2ρA·v·(v_w - v)，配合经验螺旋桨效率
        p_d = pitch_m / d if d > 0 else 0.6   # 桨距比
        # 经验 CT（静止推力近似，前进比修正）
        # Phase 2.5 校准：静止推力系数取标准 UAV 桨经验值 0.10（原 0.10*p_d 随螺距比
        # 线性缩放，系统性偏低约一半，导致设计转速约束下"推力不足"被过度判定）。
        ct_static = 0.10                      # 静止推力系数（经验，非随 p_d 线性缩放）
        # 前进比衰减
        decay = max(0.0, 1.0 - 0.9 * min(advance_ratio_j / (p_d + 1e-6), 1.0))
        ct_eff = ct_static * decay
        thrust = ct_eff * rho * n_rps ** 2 * d ** 4
        # 功率系数经验（与 CT 和前进比相关）
        cp_est = 0.012 * p_d + 0.004 * advance_ratio_j
        power = cp_est * rho * n_rps ** 3 * d ** 5

    # 螺旋桨效率（推进效率）
    if power > 0 and velocity_ms > 0:
        eta_prop = thrust * velocity_ms / power
        eta_prop = max(0.2, min(eta_prop, 0.92))   # 物理限幅
    else:
        eta_prop = 0.0
    return thrust, power, eta_prop


def propeller_required_power(thrust_n: float, velocity_ms: float,
                             eta_prop: float) -> float:
    """给定所需推力和速度，反推轴功率：P_shaft = T·v/η_prop。"""
    if eta_prop <= 0:
        return float("inf")
    return thrust_n * velocity_ms / eta_prop


def _rpm_scan(design_rpm: float, lo: int = 250, step: int = 250):
    """转速扫描序列（从低到高，止于设计转速）。

    Phase 2.5 C3：已被 `_solve_propeller` 的二分求解取代（线性扫描在 BEMT
    风车区推力陡变处造成 overshoot）。保留仅作历史参考，不再被调用。
    """
    hi = max(int(design_rpm), lo)
    for rpm in range(lo, hi + 1, step):
        yield rpm


# ============================================================
# 电机模型
# ============================================================
def motor_efficiency(load_ratio: float, peak_efficiency: float = 0.85) -> float:
    """电机效率随负载比的经验曲线。

    load_ratio = 当前功率 / 最大功率。
    低负载时效率下降，峰值约在 60%~80% 负载。
    """
    lr = max(0.0, min(load_ratio, 1.5))
    # 二次经验：峰值在 lr=0.7，端点回落
    eta = peak_efficiency * (1.0 - 0.15 * (lr - 0.7) ** 2 / 0.49)
    return max(0.3, min(eta, peak_efficiency))


# ============================================================
# 电池模型
# ============================================================
def battery_energy_j(battery: BatteryParams) -> float:
    """可用总能量（焦耳），含可用比例。"""
    return battery.energy_j * battery.usable_fraction


# ============================================================
# 主模型
# ============================================================
class PropulsionModel:
    """电推进系统模型。

    输入：
        aircraft / propellers / motor / battery（推进系统参数）
        velocity_ms, rho（飞行状态）
    输出：
        PropulsionResult（推力/功率/效率/能耗）
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.esc_efficiency = float(self.cfg.get("esc_efficiency", 0.95))
        # BEMT 求解器（Phase 2.5）：极线查找表加载失败则回退 legacy 动量理论
        self.bemt = None
        try:
            polar_path = self.cfg.get("prop_polar_table")
            self.bemt = BEMTSolver(load_polar_table(polar_path), self.cfg)
        except Exception as e:   # noqa: BLE001
            logger.warning("BEMT 初始化失败(%s)，回退动量理论快速模型", e)

    def _thrust_power(self, p: PropellerParams, rpm: float,
                      velocity_ms: float, rho: float) -> tuple[float, float, float]:
        """单桨推力/轴功率/效率。优先 BEMT，未启用则用 legacy 动量理论。"""
        if self.bemt is not None:
            return self.bemt.thrust_power(
                p.diameter_m, p.pitch_m, rpm, velocity_ms, rho, p.n_blades)
        return propeller_thrust_power(p.diameter_m, p.pitch_m, rpm, velocity_ms, rho,
                                      p.n_blades, p.ct, p.cp)

    def evaluate(self, aircraft: Aircraft, velocity_ms: float,
                 rho: float, required_thrust_n: float) -> "PropulsionResult":
        """给定飞行状态与所需推力，评估推进系统。"""
        props = self._get_propellers(aircraft)

        # 1) 每桨推力分配（均分）
        n = max(len(props), 1)
        thrust_per = required_thrust_n / n
        motor = aircraft.propulsion.motor
        # 单桨电机额定轴功率（供工作点求解的功率约束；Phase 2.5 C3 D2）
        p_max_shaft_each = motor.max_power_kw * 1000.0 * motor.efficiency * self.esc_efficiency

        # 2) 逐桨评估
        total_thrust = 0.0
        total_shaft_power = 0.0
        total_elec_power = 0.0
        prop_details = []
        total_thrust_at_design_rpm = 0.0
        for p in props:
            # 螺旋桨在给定状态下的推力/功率曲线 → 在转速/功率双约束下求解
            t_i, p_i, eta_i, rpm_info = self._solve_propeller(
                p, thrust_per, velocity_ms, rho, p_max_shaft_each)
            total_thrust += t_i
            total_shaft_power += p_i
            # 设计转速下的最大推力（转速受限，供可用推力取 min）
            t_design, _, _ = self._thrust_power(
                p, rpm_info["design_rpm"], velocity_ms, rho)
            total_thrust_at_design_rpm += t_design
            prop_details.append({
                "thrust_n": t_i, "shaft_power_w": p_i, "efficiency": eta_i,
                "x_m": p.x_m, "y_m": p.y_m, "z_m": p.z_m,
                "location": p.location,
                "design_rpm": rpm_info["design_rpm"],
                "solve_rpm": rpm_info["solve_rpm"],
                "operating_rpm": rpm_info["operating_rpm"],
                "thrust_at_design_rpm_n": t_design,
            })

        # 3) 电机效率
        # 电机总功率需求 = 轴功率 / 电机效率；单电机功率
        motor_power_total = total_shaft_power / motor.efficiency
        motor_power_each = motor_power_total / n if n > 0 else 0.0
        load_ratio = motor_power_each / (motor.max_power_kw * 1000.0) \
            if motor.max_power_kw > 0 else 0.0
        eta_motor_eff = motor_efficiency(load_ratio, motor.efficiency)

        # 4) 电功率（含 ESC 损耗）
        total_elec_power = total_shaft_power / (eta_motor_eff * self.esc_efficiency)

        # 5) 可用推力：在「转速 ≤ 设计转速」与「轴功率 ≤ 电机最大轴功率」双约束下
        #    二分求最大推力（Phase 2.5 BEMT：替代原 p_max·η/v 线性近似，与 BEMT 物理一致）
        eta_avg = sum(d["efficiency"] for d in prop_details) / n if n else 0.7
        thrust_available = 0.0
        for p in props:
            t_avail, _ = self._available_thrust(p, velocity_ms, rho, p_max_shaft_each)
            thrust_available += t_avail

        result = PropulsionResult(
            thrust_required_n=required_thrust_n,
            thrust_available_n=thrust_available,
            total_thrust_n=total_thrust,
            shaft_power_w=total_shaft_power,
            electric_power_w=total_elec_power,
            motor_efficiency=eta_motor_eff,
            propeller_efficiency=eta_avg,
            esc_efficiency=self.esc_efficiency,
            motor_load_ratio=load_ratio,
            n_propellers=n,
            propellers=prop_details,
        )
        logger.info("推进: T_req=%.1f N, T_avail=%.1f N, P_shaft=%.0f W, "
                    "P_elec=%.0f W, η_prop=%.2f, η_motor=%.2f",
                    required_thrust_n, thrust_available, total_shaft_power,
                    total_elec_power, eta_avg, eta_motor_eff)
        return result

    def _get_propellers(self, aircraft: Aircraft) -> list[PropellerParams]:
        """从 Aircraft.propulsion 提取螺旋桨列表（含兼容处理）。"""
        props = list(aircraft.propulsion.propellers)
        if not props:
            # 兼容 Phase 1：根据 n_props + prop_diameter 生成默认布局
            n = max(aircraft.propulsion.n_props, 1)
            d = aircraft.propulsion.prop_diameter_m or 0.4
            for i in range(n):
                props.append(PropellerParams(
                    diameter_m=d, pitch_m=0.6 * d,
                    x_m=0.0, y_m=0.0, z_m=0.0,
                    location="nose" if i == 0 else "wing",
                ))
        return props

    def _solve_propeller(self, p: PropellerParams, thrust_target_n: float,
                         velocity_ms: float, rho: float,
                         p_max_shaft: float | None = None
                         ) -> tuple[float, float, float, dict]:
        """在「转速 ≤ 设计转速」与「轴功率 ≤ p_max_shaft」双约束下求解单桨工作点。

        Phase 2.5 C3 修复（诊断 D1/D2）：
          - 原实现用 `_rpm_scan(lo=250, step=250)` 线性扫描求「产生所需推力的
            最小转速」，但 BEMT 推力在脱离风车区（前进比过高）时陡变
            （0 → 几十/几百 N），250rpm 步进跨过陡变区导致推力/功率
            overshoot 数倍，功率虚高、续航系统性缩水。
          - 改为二分求解（BEMT 推力脱离风车区后随转速单调非减），消除
            离散化 overshoot。
          - 增加轴功率约束：若所需推力对应转速的轴功率超电机额定轴功率，
            降到功率允许的最大转速（推力不足由下游校验捕获），避免电机超载。

        区分三种转速：
          design_rpm    设计指定转速 p.rpm（上限）
          solve_rpm     为满足所需推力需达到的转速（二分，≤ design_rpm）
          operating_rpm 实际工作转速（功率受限时 = 功率允许最大转速）

        返回 (推力 N, 轴功率 W, 效率, rpm_info)。
        """
        design_rpm = float(p.rpm) if p.rpm and p.rpm > 0 else 6000.0
        rpm_info = {"design_rpm": design_rpm, "solve_rpm": 0.0, "operating_rpm": 0.0}
        if thrust_target_n <= 0:
            return 0.0, 0.0, 0.0, rpm_info

        # 二分求产生所需推力的最小转速
        lo, hi = 250.0, design_rpm
        t_hi, pw_hi, eta_hi = self._thrust_power(p, hi, velocity_ms, rho)
        if t_hi < thrust_target_n:
            # 设计转速也达不到所需推力：返回设计转速工作点（推力不足）
            rpm_info = {"design_rpm": design_rpm, "solve_rpm": design_rpm,
                        "operating_rpm": design_rpm}
            return t_hi, pw_hi, eta_hi, rpm_info
        for _ in range(20):
            mid = 0.5 * (lo + hi)
            t_mid, _, _ = self._thrust_power(p, mid, velocity_ms, rho)
            if t_mid >= thrust_target_n:
                hi = mid
            else:
                lo = mid
        op_rpm = hi
        thrust, power, eta = self._thrust_power(p, op_rpm, velocity_ms, rho)

        # 轴功率约束：不超过电机额定轴功率
        if p_max_shaft is not None and power > p_max_shaft:
            t_lim, rpm_lim = self._available_thrust(p, velocity_ms, rho, p_max_shaft)
            thrust, power, eta = self._thrust_power(p, rpm_lim, velocity_ms, rho)
            op_rpm = rpm_lim

        rpm_info = {"design_rpm": design_rpm, "solve_rpm": op_rpm,
                    "operating_rpm": op_rpm}
        return thrust, power, eta, rpm_info

    def _available_thrust(self, p: PropellerParams, velocity_ms: float,
                          rho: float, p_max_shaft: float) -> tuple[float, float]:
        """在「转速 ≤ 设计转速」与「轴功率 ≤ p_max_shaft」双约束下求最大推力。

        Phase 2.5 性能优化：功率随转速单调非减（风车区为 0，之后单调增），
        用二分查找功率超限边界（36 点 → ~14 次），取超限前转速的最大推力。
        返回 (最大推力 N, 对应转速 rpm)。
        """
        design_rpm = float(p.rpm) if p.rpm and p.rpm > 0 else 6000.0
        t_hi, pw_hi, _ = self._thrust_power(p, design_rpm, velocity_ms, rho)
        if pw_hi <= p_max_shaft:
            return t_hi, design_rpm               # 设计转速也不超限
        lo, hi = 250.0, design_rpm
        for _ in range(12):
            mid = 0.5 * (lo + hi)
            _, pw_mid, _ = self._thrust_power(p, mid, velocity_ms, rho)
            if pw_mid > p_max_shaft:
                hi = mid
            else:
                lo = mid
        t_lo, _, _ = self._thrust_power(p, lo, velocity_ms, rho)
        return t_lo, lo


class PropulsionResult:
    """推进系统评估结果（数据契约）。"""

    def __init__(self, thrust_required_n: float, thrust_available_n: float,
                 total_thrust_n: float, shaft_power_w: float, electric_power_w: float,
                 motor_efficiency: float, propeller_efficiency: float,
                 esc_efficiency: float, motor_load_ratio: float,
                 n_propellers: int, propellers: list):
        self.thrust_required_n = thrust_required_n
        self.thrust_available_n = thrust_available_n
        self.total_thrust_n = total_thrust_n
        self.shaft_power_w = shaft_power_w
        self.electric_power_w = electric_power_w
        self.motor_efficiency = motor_efficiency
        self.propeller_efficiency = propeller_efficiency
        self.esc_efficiency = esc_efficiency
        self.motor_load_ratio = motor_load_ratio
        self.n_propellers = n_propellers
        self.propellers = propellers

    def to_dict(self) -> dict:
        return {
            "thrust_required_n": self.thrust_required_n,
            "thrust_available_n": self.thrust_available_n,
            "total_thrust_n": self.total_thrust_n,
            "shaft_power_w": self.shaft_power_w,
            "electric_power_w": self.electric_power_w,
            "motor_efficiency": self.motor_efficiency,
            "propeller_efficiency": self.propeller_efficiency,
            "esc_efficiency": self.esc_efficiency,
            "motor_load_ratio": self.motor_load_ratio,
            "n_propellers": self.n_propellers,
            "propellers": self.propellers,
        }
