"""Phase 2.5 回归测试：变量映射、稳定性标签、转速敏感性、阻力一致性。"""
import pytest

from src.core.contracts import (AeroResult, Aircraft, BatteryParams, MissionSpec,
                                MotorParams, PerformanceResult, PropellerParams,
                                PropulsionParams, WingParams)


# ============================================================
# 1. 稳定性原始标签保存（_trim_cg 不覆盖配平前信号）
# ============================================================
def test_trim_cg_preserves_pre_trim_signals():
    from src.core.workflow import _trim_cg
    aero = AeroResult(CL_alpha_per_rad=5.0, Cm_alpha_per_rad=-1.0,
                      CL=[0.5], Cm=[-0.1])
    ac = Aircraft(wing=WingParams(span_m=3.0, area_m2=1.2, aspect_ratio=7.5),
                  cg_x_m=0.7)
    mission = MissionSpec(static_margin_min=0.05, static_margin_max=0.15)
    _trim_cg(aero, ac, mission)

    cbar = ac.wing.mean_chord_m  # 1.2/3.0 = 0.4
    sm_pre = -(-1.0) / 5.0       # 0.2
    np_x = 0.7 + sm_pre * cbar   # 0.78
    sm_target = 0.10
    cg_new = np_x - sm_target * cbar  # 0.74

    # 配平前原始信号被保留
    assert aero.Cm_alpha_pre_trim_per_rad == pytest.approx(-1.0)
    assert aero.neutral_point_x_pre_trim_m == pytest.approx(np_x)
    assert aero.cg_x_pre_trim_m == pytest.approx(0.7)
    assert aero.static_margin_target == pytest.approx(sm_target)
    # 重心被调整，配平后 Cmα 为线性转移结果
    assert ac.cg_x_m == pytest.approx(cg_new)
    assert aero.Cm_alpha_per_rad == pytest.approx(-1.0 + 5.0 * (cg_new - 0.7) / cbar)


def test_performance_result_has_stability_fields():
    """PerformanceResult 应含配平前/后稳定性标签字段。"""
    p = PerformanceResult(static_margin=0.10, static_margin_pre_trim=0.25,
                          cg_shift_m=0.03, static_margin_target=0.10)
    d = p.to_dict()
    for key in ("static_margin", "static_margin_pre_trim", "neutral_point_x_pre_trim_m",
                "cg_x_pre_trim_m", "cg_shift_m", "static_margin_target"):
        assert key in d
    assert p.static_margin_pre_trim == 0.25


# ============================================================
# 2. 推进模型转速敏感性（prop_rpm 真正参与模型）
# ============================================================
def _prop_ac(rpm: float) -> Aircraft:
    return Aircraft(propulsion=PropulsionParams(
        n_props=1,
        propellers=[PropellerParams(diameter_m=0.5, pitch_m=0.3, rpm=rpm)],
        motor=MotorParams(max_power_kw=1.5, efficiency=0.85),
        battery=BatteryParams()))


def test_prop_rpm_sensitivity():
    """不同设计转速应产生不同可用推力与转速记录。"""
    from src.propulsion.model import PropulsionModel
    model = PropulsionModel()
    res = {}
    for rpm in (4000.0, 6000.0, 9000.0):
        r = model.evaluate(_prop_ac(rpm), velocity_ms=30.0, rho=1.225,
                           required_thrust_n=10.0)
        res[rpm] = r
    # 转速更高 → 转速受限可用推力更大
    assert res[9000.0].thrust_available_n >= res[6000.0].thrust_available_n
    assert res[6000.0].thrust_available_n >= res[4000.0].thrust_available_n
    # 三转速记录一致：design_rpm == 采样值，operating_rpm <= design_rpm
    for rpm, r in res.items():
        pd = r.propellers[0]
        assert pd["design_rpm"] == pytest.approx(rpm)
        assert pd["operating_rpm"] <= pd["design_rpm"] + 1e-6
        assert "solve_rpm" in pd


# ============================================================
# 3. 定尺寸与完整性能分析的阻力/功率估算一致性
# ============================================================
def test_sizing_uses_parasite_drag_model():
    """定尺寸的寄生阻力应复用 ParasiteDragModel（与 evaluator 一致），而非硬编码。"""
    from src.geometry.sizing import _cruise_drag_power
    from src.performance.parasite_drag import ParasiteDragModel
    from src.geometry.tail import size_htail, size_vtail
    from src.geometry.wing import size_wing
    from src.geometry.propeller import size_fuselage
    from src.core.config import load_config

    sizing = load_config("config/mission.yaml")["sizing"]
    parasite = ParasiteDragModel(sizing.get("parasite_drag"))
    oswald = sizing.get("oswald_efficiency", 0.85)

    wing = size_wing(10.0, 8.0, 8.0, 0.5, "clarky", "clarky", 2.0, 3.0, -2.0)
    fus = size_fuselage(3.0, 1.0, 0.5, wing.span_m, wing.mean_chord_m)
    htail = size_htail(wing, 0.5, 1.0, 4.0)
    vtail = size_vtail(wing, 0.04, 1.0, 1.8)

    est = _cruise_drag_power(10.0, wing, htail, vtail, fus, 1.225, 25.0,
                             sizing, parasite, oswald)
    # 与 ParasiteDragModel 直接输出一致（统一接口）
    ac_min = Aircraft(wing=wing, htail=htail, vtail=vtail, fuselage=fus)
    re = 1.225 * 25.0 * wing.mean_chord_m / 1.7894e-5
    cd0_direct = parasite.evaluate(ac_min, re, 25.0 / 340.3, wing.area_m2).cd0
    assert est["cd0"] == pytest.approx(cd0_direct, rel=1e-9)
    assert est["cd"] > est["cd0"]  # 含诱导阻力
    assert est["drag_n"] > 0 and est["p_cruise_w"] > 0


# ============================================================
# 4. 25维变量 → OpenVSP 几何模型映射
# ============================================================
def test_wing_sweep_twist_dihedral_pass_to_openvsp():
    """机翼后掠/扭转/上反应传递到 OpenVSP 渲染参数（sweep/twist 参与分析）。"""
    from src.openvsp.adapter import OpenVSPAdapter

    class FakeDB:
        def find_or_default(self, name, default=None):
            return f"uiuc/{name}.dat"

    ac = Aircraft(
        wing=WingParams(span_m=3.0, area_m2=1.2, aspect_ratio=7.5,
                        sweep_quarter_chord_deg=12.0, dihedral_deg=4.0,
                        twist_tip_deg=-1.5, airfoil_root="clarky",
                        airfoil_tip="clarky"),
    )
    adapter = OpenVSPAdapter("dummy_vspscript.exe", FakeDB())
    params = adapter._render_params(ac)
    assert params["wing_sweep"] == pytest.approx(12.0)
    assert params["wing_dihedral"] == pytest.approx(4.0)   # 完整模型保留上反
    assert params["wing_twist"] == pytest.approx(-1.5)


# ============================================================
# 6. 阻力重复计数修复（Phase 2.5 关键 bug）
# ============================================================
def test_interp_polar_returns_induced_drag_only():
    """_interp_polar 应返回诱导阻力 k·CL²，而非含拟合截距 CD0 的原始极线 CD。"""
    from src.performance.evaluator import _interp_polar
    aero = AeroResult(CL=[0.0, 0.5, 1.0], CD=[0.014, 0.025, 0.06],
                      alpha_deg=[0.0, 3.0, 6.0], k_induced=0.043,
                      CL_alpha_per_rad=5.0, CL_alpha_0deg=0.0)
    cl, cd_ind, alpha = _interp_polar(aero, 0.5)
    # 诱导阻力 = k·CL²（不含 CD0 截距），避免与寄生阻力重复计数
    assert cd_ind == pytest.approx(0.043 * 0.5 ** 2)
    assert cd_ind < 0.02
    assert 2.0 < alpha < 4.0


def test_static_thrust_coefficient_realistic():
    """静止推力系数应接近标准 UAV 桨经验值 0.10（原 0.10*p_d 系统性偏低）。"""
    from src.propulsion.model import propeller_thrust_power
    # d=0.5, 6000rpm, 静止: T ≈ 0.10·ρ·n²·d⁴ ≈ 76.6 N
    t, p, eta = propeller_thrust_power(0.5, 0.3, 6000.0, 0.0, 1.225)
    assert t > 60.0
    assert eta == 0.0  # 静止时推进效率为 0


# ============================================================
# 5. 模型限制元数据
# ============================================================
def test_model_limitations_in_metadata():
    from src.dataset.metadata import MODEL_LIMITATIONS, build_metadata
    meta = build_metadata("run_x", dataset_id="ds", random_seed=1)
    assert "wing_dihedral_deg" in meta.extra
    assert "CL_max" in meta.extra
    assert "propulsion_model" in meta.extra
    assert meta.extra["wing_dihedral_deg"]
