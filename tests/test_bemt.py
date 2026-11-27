"""Phase 2.5 BEMT（叶素动量理论）单元测试 + 动量理论对比验证。

覆盖：
  1. PolarTable 极线查找表：二维插值正确性、越界钳制、JSON 往返
  2. BladeGeometry 标准桨叶几何：0.75R 桨距角换算、弦长/扭转线性
  3. BEMTSolver 物理合理性：巡航推力 > 0、推力随转速单调、效率 ∈ [0, 0.95]
  4. 风车状态（前进比过高）：返回 0 推力（不产生无物理意义的正扭矩）
  5. 动量理论对比：BEMT 效率 ≤ 理想作动盘效率（物理上界），且趋势一致
"""
from __future__ import annotations

import math

import pytest

from src.propulsion.bemt import (BEMTSolver, BladeGeometry, PolarTable,
                                 load_polar_table)


# ============================================================
# 1. PolarTable 极线查找表
# ============================================================
def _simple_polar() -> PolarTable:
    # 构造一个理想线性翼型极线：CL = 0.1·α° + 0.2，CD = 0.01 + 0.01·α°²（便于手算）
    alphas = [-10.0, -5.0, 0.0, 5.0, 10.0, 15.0]
    re_list = [1e4, 1e5, 1e6]
    cl = [[0.1 * a + 0.2 for a in alphas] for _ in re_list]
    cd = [[0.01 + 0.01 * (a / 10.0) ** 2 for a in alphas] for _ in re_list]
    return PolarTable(reynolds=re_list, alpha_deg=alphas, cl=cl, cd=cd,
                      airfoil_name="test")


def test_polar_interp_alpha():
    """攻角插值：α=2.5° 处 CL 应为 0.1·2.5+0.2 = 0.45。"""
    polar = _simple_polar()
    cl, cd = polar.interp(math.radians(2.5), 1e5)
    assert cl == pytest.approx(0.45, rel=1e-6)


def test_polar_interp_reynolds_log_scale():
    """雷诺数按 log10 插值：log10(1e5) 是 log10(1e4)~log10(1e6) 的中点 → f=0.5。"""
    polar = _simple_polar()
    # 构造仅在 Re 轴变化的极线（α 固定）
    cl_lo = polar.interp(0.0, 1e4)[0]
    cl_hi = polar.interp(0.0, 1e6)[0]
    cl_mid = polar.interp(0.0, 1e5)[0]
    assert cl_mid == pytest.approx((cl_lo + cl_hi) / 2.0, rel=1e-6)


def test_polar_out_of_range_clamps():
    """越界雷诺数钳制到端点（不产生外推震荡）。"""
    polar = _simple_polar()
    cl_at_min = polar.interp(0.0, 1.0)[0]       # Re 远低于最小
    cl_at_max = polar.interp(0.0, 1e9)[0]       # Re 远高于最大
    assert cl_at_min == pytest.approx(polar.interp(0.0, 1e4)[0], rel=1e-9)
    assert cl_at_max == pytest.approx(polar.interp(0.0, 1e6)[0], rel=1e-9)


def test_polar_json_roundtrip(tmp_path):
    """to_json / from_json 往返一致。"""
    polar = _simple_polar()
    p = tmp_path / "polar.json"
    polar.to_json(p)
    loaded = PolarTable.from_json(p)
    assert loaded.airfoil_name == "test"
    assert loaded.cl.shape == polar.cl.shape
    assert (loaded.cl == polar.cl).all()


def test_default_polar_loads():
    """默认 clarky 查找表可加载且形状合法（5 Re × 36 α）。"""
    polar = load_polar_table()
    assert polar.reynolds.shape[0] >= 2
    assert polar.alpha_deg.shape[0] >= 3
    assert polar.cl.shape == polar.cd.shape
    assert polar.cl.shape == (len(polar.reynolds), len(polar.alpha_deg))


# ============================================================
# 2. BladeGeometry 标准桨叶几何
# ============================================================
def test_blade_geometry_beta_75_from_pitch():
    """0.75R 桨距角由几何螺距定义：pitch = π·0.75·D·tan(β_75)。"""
    D, pitch = 0.5, 0.3
    geo = BladeGeometry(D, pitch, n_blades=2)
    beta_75_expected = math.atan(pitch / (0.75 * math.pi * D))
    # 截面 0.75R 对应 r ≈ 0.75R（中点积分略有偏移，用最近截面验证换算公式）
    i_75 = int(round((0.75 - geo.hub_ratio) / (1.0 - geo.hub_ratio) * (geo.n_sections - 1)))
    # 扭转使 β(r) = β_75 + twist·(r/R - 0.75)，故 β_75 应接近截面角减去扭转项
    rR = geo.r[i_75] / geo.R
    beta_75 = geo.beta[i_75] - math.radians(geo.twist_deg) * (rR - 0.75)
    assert beta_75 == pytest.approx(beta_75_expected, rel=1e-3)


def test_blade_geometry_chord_linear_taper():
    """弦长线性 taper：c_tip / c_root = chord_taper（根/梢为构造值，非截面中心）。"""
    geo = BladeGeometry(0.5, 0.3, n_blades=2, chord_taper=0.5)
    assert geo.c_root > geo.c_tip
    assert geo.c_tip / geo.c_root == pytest.approx(0.5, rel=1e-9)
    # 截面弦长在 [c_root, c_tip] 之间线性变化
    assert geo.chord[0] < geo.c_root
    assert geo.chord[-1] > geo.c_tip
    assert geo.chord[0] > geo.chord[-1]


# ============================================================
# 3. BEMTSolver 物理合理性
# ============================================================
def test_bemt_cruise_thrust_positive_and_efficiency_bounded():
    """巡航工况（匹配桨距）应产生正推力，效率 ∈ (0, 0.95]。"""
    solver = BEMTSolver(load_polar_table(), {})
    # D=0.4、pitch=0.5（约巡航匹配）、6000rpm、30m/s
    t, p, eta = solver.thrust_power(0.4, 0.5, 6000.0, 30.0, 1.225, 2)
    assert t > 0
    assert p > 0
    assert 0.0 < eta <= 0.95


def test_bemt_thrust_increases_with_rpm():
    """正常推进区间内推力随转速单调增。"""
    solver = BEMTSolver(load_polar_table(), {})
    t_prev = -1.0
    for rpm in (3000.0, 4000.0, 5000.0, 6000.0):
        t, _, _ = solver.thrust_power(0.4, 0.5, rpm, 30.0, 1.225, 2)
        assert t >= t_prev - 1e-6
        t_prev = t


def test_bemt_windmill_returns_zero():
    """前进比过高（风车状态）应返回 0 推力/0 功率，不产生无物理意义的正扭矩。"""
    solver = BEMTSolver(load_polar_table(), {})
    # D=1.2、高前进比（低转速 + 高巡航速度）→ 风车
    t, p, eta = solver.thrust_power(1.2, 1.0, 500.0, 35.0, 1.225, 2)
    assert t == 0.0
    assert p == 0.0
    assert eta == 0.0


# ============================================================
# 5. 动量理论对比（BEMT 效率 ≤ 理想作动盘上界）
# ============================================================
def test_bemt_efficiency_below_ideal_actuator_disk():
    """BEMT 效率不得超过理想作动盘效率（含叶型损失/尾流旋转损失，物理上界）。"""
    solver = BEMTSolver(load_polar_table(), {})
    for D, pitch, rpm, v in [
        (0.3, 0.4, 6000.0, 25.0),
        (0.4, 0.5, 6000.0, 30.0),
        (0.5, 0.6, 5000.0, 25.0),
    ]:
        t, _, eta_bemt = solver.thrust_power(D, pitch, rpm, v, 1.225, 2)
        if t <= 0:
            continue
        area = math.pi * (D / 2.0) ** 2
        ct = t / (0.5 * 1.225 * area * v ** 2)
        eta_ideal = 2.0 / (1.0 + math.sqrt(1.0 + ct))   # 理想作动盘
        assert eta_bemt <= eta_ideal + 1e-6, \
            f"BEMT 效率 {eta_bemt:.3f} 超过理想上限 {eta_ideal:.3f} (D={D},v={v})"


def test_bemt_vs_momentum_thrust_same_order():
    """BEMT 推力与理想动量理论同量级（作动盘近似，非精确相等）。"""
    solver = BEMTSolver(load_polar_table(), {})
    D, pitch, rpm, v = 0.4, 0.5, 6000.0, 30.0
    t_bemt, _, _ = solver.thrust_power(D, pitch, rpm, v, 1.225, 2)
    area = math.pi * (D / 2.0) ** 2
    # 动量理论：T = 2ρA·v²·a(1+a)，用前进比估计 a 较小（巡航 a≈0.05~0.15）
    # 仅验证同量级：T 在 0.5ρAv² 的 [0.05, 1.0] 倍范围内
    ref = 0.5 * 1.225 * area * v ** 2
    assert 0.05 * ref < t_bemt < 1.0 * ref, f"BEMT 推力 {t_bemt:.2f} N 偏离动量理论量级 {ref:.2f} N"
