"""M01: YAML 配置加载与合并。"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"

DEFAULTS: dict[str, Any] = {
    "mission": {
        "mission_name": "unnamed_mission",
        "range_km": 100.0,
        "cruise_speed_ms": 30.0,
        "payload_kg": 2.0,
        "mtow_kg": None,
        "endurance_h": 2.0,
        "altitude_m": 500.0,
    },
    "design": {
        "aspect_ratio": 8.0,
        "taper_ratio": 0.5,
        "airfoil_root": "clarky",
        "airfoil_tip": "clarky",
        "wing_sweep_deg": 2.0,              # 机翼 1/4 弦后掠
        "wing_dihedral_deg": 3.0,           # 机翼上反
        "wing_twist_deg": -2.0,             # 机翼扭转（负=洗出）
        "wing_x_fraction": 0.30,            # 机翼根前缘位置比例
        "htail_volume_coeff": 0.50,
        "vtail_volume_coeff": 0.040,
        "htail_aspect_ratio": 4.0,
        "htail_taper_ratio": 0.6,
        "htail_sweep_deg": 5.0,
        "vtail_aspect_ratio": 1.8,
        "vtail_taper_ratio": 0.6,
        "vtail_sweep_deg": 12.0,
        "n_props": 1,                       # 螺旋桨数量 1~4
        "prop_layout": "nose",              # nose|tail|wing|distributed
        "prop_diameter_m": None,            # None → 功率反推；数值 → 强制直径
        "prop_rpm": 6000.0,                 # 设计转速
    },
    "sizing": {
        "empty_mass_fraction": 0.30,      # 结构+机载设备 / MTOW
        "battery_specific_energy_wh_kg": 220.0,
        "prop_efficiency": 0.60,           # 定尺寸螺旋桨效率假设（Phase 2.5 BEMT 校准：BEMT 实际巡航效率 0.4~0.7，0.6 为中位代表值；旧 0.70 高估 → 电机功率不足）
        "motor_efficiency": 0.85,
        "esc_efficiency": 0.95,
        "oswald_efficiency": 0.85,        # 诱导阻力 Oswald 效率（定尺寸与完整分析一致）
        "battery_usable_fraction": 0.85,
        "cl_max_2d": 1.3,
        "cl_max_3d_factor": 0.90,
        "power_margin_factor": 3.0,       # 最大功率 = 巡航需求 × 系数（配合 prop_efficiency=0.6，有效裕度 3.0/0.6=5.0；BEMT 校准后通过率≈63%，与 C1 持平）
        "motor_specific_power_kw_kg": 8.0,
        "cl_cruise_target": 0.60,
        "cg_mac_fraction": 0.25,          # 重心在平均气动弦上的位置
        "relaxation": 0.5,
        "max_iterations": 300,
        "tolerance": 0.01,
        # BEMT 螺旋桨（Phase 2.5）：桨叶几何 + 求解参数
        "prop_polar_table": None,          # 桨叶极线查找表路径（None → data/polars/clarky_polar.json）
        "blade_design_alpha_deg": 5.0,     # 0.75R 截面巡航攻角（桨距匹配巡航设计点）
        "blade_chord_ratio_75": 0.12,      # c_0.75 / D（弦长基准）
        "blade_chord_taper": 0.5,          # c_tip / c_root（线性 taper）
        "blade_twist_deg": -10.0,          # 根→梢扭转角（负 = 洗出）
        "bemt_n_sections": 12,             # 径向离散截面数
        "bemt_max_iter": 60,               # 诱导因子迭代上限
        "bemt_tolerance": 1e-2,            # 收敛容差（概念设计精度，1% 诱导因子）
        "bemt_relaxation": 0.35,           # 松弛因子
        "bemt_hub_ratio": 0.15,            # 桨毂半径 / 桨半径
        "bemt_a_max": 0.8,                 # 轴向诱导因子上限（数值防护）
        # 寄生阻力模型（ParasiteDragModel）
        "parasite_drag": {
            "ff_wing": 1.35,              # 机翼形状因子
            "ff_tail": 1.30,              # 尾翼形状因子
            "ff_fuselage": 1.15,          # 机身形状因子
            "q_wing": 1.0,                # 干扰因子
            "q_tail": 1.03,
            "q_fuselage": 1.0,
            "t_c_wing": 0.12,             # 机翼相对厚度
            "t_c_tail": 0.08,             # 尾翼相对厚度
            "misc_drag_area_m2": 0.006,   # 设备/外露部件当量平板面积
        },
    },
    "analysis": {
        "alpha_start_deg": -4.0,
        "alpha_end_deg": 14.0,
        "alpha_npts": 7,
        "mach": 0.10,
        "ncpu": 4,
        "wake_iterations": 3,
        "xfoil_alpha_start": -4.0,
        "xfoil_alpha_step": 1.0,
        "xfoil_alpha_end": 12.0,
        "xfoil_max_iter": 100,
    },
    "tools": {
        "openvsp_script": "C:/OpenVSP-3.55.0-win64/vspscript.exe",
        "xfoil": "C:/Xfoil/xfoil.exe",
        "uiuc_db_dir": "E:/workbuddy/AI-UAV/uiuc/coord_seligFmt",
    },
    "paths": {
        "data_dir": "E:/workbuddy/AI-UAV/data/results",
        "report_dir": "E:/workbuddy/AI-UAV/reports",
        "db_path": "E:/workbuddy/AI-UAV/data/uavdp.sqlite",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load_config(config_path: str | Path) -> dict:
    """加载 YAML 配置并与默认值深合并。"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    if not isinstance(user_cfg, dict):
        raise ValueError(f"配置文件顶层必须是映射: {path}")
    return _deep_merge(DEFAULTS, user_cfg)
