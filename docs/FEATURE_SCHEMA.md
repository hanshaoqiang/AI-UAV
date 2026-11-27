# FEATURE_SCHEMA — AI 训练特征冻结清单

> **schema_version: 1.0.0**
> 冻结日期：2026-11-26（Phase 2.5 C3 Stage B）
> 适用范围：Phase 3 代理模型训练（XGBoost / LightGBM / MLP）与多目标优化（NSGA-II / 贝叶斯）

---

## 0. 使用约定

1. **本文件一经确认，Phase 3 之前不得修改字段定义**。任何字段新增、删除、改名、改单位、改类型，都必须升级 `schema_version`（语义化版本：改名/删除为 Major，新增可选字段为 Minor，仅文档澄清为 Patch）。
2. 命名遵循 `snake_case` + 单位后缀：`_ms`(m/s)、`_m`(m)、`_kg`(kg)、`_deg`(度)、`_km`(km)、`_h`(小时)、`_w`(W)、`_kw`(kW)、`_rpm`、`_n`(N)、`_m2`(m²)。
3. 无量纲系数不加单位后缀。
4. 离散变量（字符串）用 one-hot / 类别编码，不参与 Pearson 相关分析。

### 0.1 三种语义状态（强制区分，禁止混用）

| 语义 | 定义 | 训练用途 |
|---|---|---|
| `analysis_valid` | 数值计算成功且通过 DataValidator error 级校验（物理/数值合理） | 可作为**回归训练样本** |
| `mission_feasible` | 满足给定任务约束（航程/续航/失速/爬升/静稳定裕度达标） | 可作为**分类标签**（可行/不可行） |
| `analysis_failed` | 计算失败或结果不可用（几何不可行/气动失败/外部工具失败） | **排除**出训练集 |

> **任务不可行 ≠ 计算失败**。`analysis_valid=True` 且 `mission_feasible=False` 的样本仍是有效的回归训练样本（保留性能包络信息），仅不满足任务目标。

### 0.2 列属性说明

- **是否输入**：采样/任务给定的自变量（设计变量 + 任务需求）。
- **是否输出**：由模型计算得到的因变量（派生几何 + 气动 + 性能 + 约束）。
- **是否可训练**：是否可作为代理模型的输入特征(X) 或输出标签(Y)。
- **是否参与优化**：是否作为 NSGA-II / 贝叶斯优化的决策变量。

---

## 1. Mission Features（任务输入）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `mission_range_km` | km | float | ✅ | | X（任务条件） | |
| `cruise_speed_ms` | m/s | float | ✅ | | X | |
| `payload_kg` | kg | float | ✅ | | X | |
| `mtow_kg` | kg | float | ✅(可空) | | X（约束，可空） | |
| `endurance_h` | h | float | ✅ | | X | |
| `altitude_m` | m | float | ✅ | | X | |

### 1.1 Mission 约束（Constraint 输入，见 §7）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `stall_speed_max_ms` | m/s | float | ✅(可空) | | X | |
| `static_margin_min` | — | float | ✅ | | X | |
| `static_margin_max` | — | float | ✅ | | X | |
| `climb_rate_min_ms` | m/s | float | ✅(可空) | | X | |

---

## 2. Geometry Features（几何）

### 2.1 设计变量（输入，参与优化）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `aspect_ratio` | — | float | ✅ | | X | ✅ |
| `taper_ratio` | — | float | ✅ | | X | ✅ |
| `wing_sweep_deg` | deg | float | ✅ | | X | ✅ |
| `wing_dihedral_deg` | deg | float | ✅ | | X | ✅ |
| `wing_twist_deg` | deg | float | ✅ | | X | ✅ |
| `wing_x_fraction` | — | float | ✅ | | X | ✅ |
| `htail_volume_coeff` | — | float | ✅ | | X | ✅ |
| `htail_aspect_ratio` | — | float | ✅ | | X | ✅ |
| `htail_taper_ratio` | — | float | ✅ | | X | ✅ |
| `htail_sweep_deg` | deg | float | ✅ | | X | ✅ |
| `vtail_volume_coeff` | — | float | ✅ | | X | ✅ |
| `vtail_aspect_ratio` | — | float | ✅ | | X | ✅ |
| `vtail_taper_ratio` | — | float | ✅ | | X | ✅ |
| `vtail_sweep_deg` | deg | float | ✅ | | X | ✅ |
| `airfoil_root` | — | str(离散) | ✅ | | X（类别编码） | ✅ |
| `airfoil_tip` | — | str(离散) | ✅ | | X（类别编码） | ✅ |

### 2.2 派生几何（输出）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `span_m` | m | float | | ✅ | Y（可选）/X | |
| `wing_area_m2` | m² | float | | ✅ | Y（可选）/X | |
| `root_chord_m` | m | float | | ✅ | Y（可选） | |
| `tip_chord_m` | m | float | | ✅ | Y（可选） | |
| `htail_area_m2` | m² | float | | ✅ | Y（可选） | |
| `vtail_area_m2` | m² | float | | ✅ | Y（可选） | |
| `fuselage_length_m` | m | float | | ✅ | Y（可选） | |
| `fuselage_diameter_m` | m | float | | ✅ | Y（可选） | |
| `empty_mass_kg` | kg | float | | ✅ | Y（可选） | |
| `battery_mass_kg` | kg | float | | ✅ | Y（可选）/X | |
| `wing_loading_kg_m2` | kg/m² | float | | ✅ | Y（可选） | |
| `cg_x_m` | m | float | | ✅ | Y（可选） | |

---

## 3. Propulsion Features（推进）

### 3.1 设计变量（输入，参与优化）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `n_props` | — | int(离散) | ✅ | | X（类别编码） | ✅ |
| `prop_layout` | — | str(离散) | ✅ | | X（类别编码） | ✅ |
| `prop_diameter_factor` | — | float | ✅ | | X | ✅ |
| `prop_rpm` | RPM | float | ✅ | | X | ✅ |

### 3.2 派生推进（输出）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `prop_diameter_m` | m | float | | ✅ | Y（可选）/X | |
| `prop_pitch_m` | m | float | | ✅ | Y（可选） | |
| `motor_power_kw` | kW | float | | ✅ | Y（可选）/X | |
| `battery_capacity_ah` | Ah | float | | ✅ | Y（可选） | |
| `battery_energy_wh` | Wh | float | | ✅ | Y（可选） | |

---

## 4. Aerodynamics Features（气动，输出）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `cl_alpha_per_rad` | 1/rad | float | | ✅ | Y | |
| `cl_alpha_0deg` | — | float | | ✅ | Y | |
| `cl_max` | — | float | | ✅ | Y | |
| `cd0_parasitic` | — | float | | ✅ | Y | |
| `cd0_vlm` | — | float | | ✅ | Y（不用于训练，数值伪零升阻力） | |
| `k_induced` | — | float | | ✅ | Y | |
| `ld_max` | — | float | | ✅ | Y（VLM 仅诱导阻力，见注） | |
| `ld_max_total` | — | float | | ✅ | Y（含寄生阻力，C3 D4 新增） | |
| `cm_alpha_per_rad` | 1/rad | float | | ✅ | Y | |
| `cm_alpha_pre_trim_per_rad` | 1/rad | float | | ✅ | Y（配平前原始信号） | |

> **注**：`ld_max`（VLM 纯诱导阻力，偏乐观）与 `ld_max_total`（含寄生阻力）口径不同，**不可混用**。代理模型训练以 `ld_max_total` 为物理真实标签，`ld_max` 仅作 inviscid 参考。

---

## 5. Performance Features（性能，输出）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `v_stall_ms` | m/s | float | | ✅ | Y | |
| `v_cruise_ms` | m/s | float | | ✅ | Y | |
| `CL_cruise` | — | float | | ✅ | Y | |
| `CD_cruise` | — | float | | ✅ | Y | |
| `LD_cruise` | — | float | | ✅ | Y | |
| `drag_cruise_n` | N | float | | ✅ | Y | |
| `power_cruise_w` | W | float | | ✅ | Y | |
| `power_available_w` | W | float | | ✅ | Y | |
| `excess_power_w` | W | float | | ✅ | Y | |
| `thrust_required_n` | N | float | | ✅ | Y | |
| `thrust_available_n` | N | float | | ✅ | Y | |
| `thrust_weight_ratio` | — | float | | ✅ | Y | |
| `propeller_efficiency` | — | float | | ✅ | Y | |
| `motor_efficiency` | — | float | | ✅ | Y | |
| `prop_design_rpm` | RPM | float | | ✅ | Y | |
| `prop_solve_rpm` | RPM | float | | ✅ | Y | |
| `prop_operating_rpm` | RPM | float | | ✅ | Y | |
| `tip_speed_design_ms` | m/s | float | | ✅ | Y（设计转速，见注） | |
| `tip_speed_operating_ms` | m/s | float | | ✅ | Y（工作转速，物理真实值） | |
| `range_km` | km | float | | ✅ | Y（预测航程） | |
| `range_margin_km` | km | float | | ✅ | Y（预测−任务） | |
| `endurance_h` | h | float | | ✅ | Y（预测续航） | |
| `climb_rate_ms` | m/s | float | | ✅ | Y | |
| `static_margin` | — | float | | ✅ | Y（配平后，恒=0.10，常量列） | |
| `static_margin_pre_trim` | — | float | | ✅ | Y（配平前原始信号） | |
| `neutral_point_x_m` | m | float | | ✅ | Y | |
| `neutral_point_x_pre_trim_m` | m | float | | ✅ | Y | |
| `cg_x_m` | m | float | | ✅ | Y | |
| `cg_shift_m` | m | float | | ✅ | Y | |
| `static_margin_target` | — | float | | ✅ | Y（人为目标，常量） | |

> **注**：`tip_speed_design_ms`（设计转速）是设计约束上限，不代表真实工作状态；`tip_speed_operating_ms`（工作转速）是物理真实值。训练以 `tip_speed_operating_ms` 为主，`tip_speed_design_ms` 仅作约束参考。
> **注**：`static_margin`（配平后恒 0.10）是人为目标常量，**不可作为训练标签**（std=0）；训练用 `static_margin_pre_trim`。

---

## 6. 不可训练的元数据/标识符

| Feature | 说明 |
|---|---|
| `design_id` | 样本唯一标识（str），不入特征 |
| `run_id` | 运行标识（str），不入特征 |
| `dataset_id` | 数据集标识（str），不入特征 |
| `mission_name` | 任务名（str），不入特征 |

---

## 7. Constraint Features（约束判定，输出）

| Feature | 单位 | 类型 | 输入 | 输出 | 可训练 | 参与优化 |
|---|---|---|---|---|---|---|
| `constraints_pass.航程` | — | bool | | ✅ | Y（分类） | |
| `constraints_pass.续航` | — | bool | | ✅ | Y（分类） | |
| `constraints_pass.静稳定裕度` | — | bool | | ✅ | Y（分类） | |
| `constraints_pass.失速速度` | — | bool | | ✅ | Y（分类） | |
| `constraints_pass.爬升率` | — | bool | | ✅ | Y（分类） | |
| `mission_feasible` | — | bool | | ✅ | Y（分类标签：所有达标） | |
| `analysis_valid` | — | bool | | ✅ | Y（样本筛选标记） | |
| `analysis_failed` | — | bool | | ✅ | 排除（样本筛选标记） | |

> `mission_feasible` = `constraints_pass` 中所有"存在"的约束项均 pass（可空约束项不判定）。
> `analysis_valid` = DataValidator error 级校验通过（入库存留）。`analysis_failed` = 计算失败。

---

## 8. 优化目标（Phase 3 决策变量与目标映射）

- **决策变量**：§2.1 与 §3.1 中标注「参与优化 ✅」的字段（16 连续 + 4 离散）。
- **优化目标**（多目标，优先于单独优化 CL/CD）：
  | 目标 | 对应 Feature | 方向 |
  |---|---|---|
  | 航程 | `range_km` | max |
  | 续航 | `endurance_h` | max |
  | 重量 | `mtow_kg`（派生） | min |
  | 升阻比 | `ld_max_total` | max |
  | 功耗 | `power_cruise_w` | min |
  | 稳定性 | `static_margin_pre_trim` | 趋近目标区间 |

---

## 9. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0.0 | 2026-11-26 | 初始冻结。新增 `tip_speed_design_ms`/`tip_speed_operating_ms`（C3 D3）、`ld_max_total`（C3 D4）；明确 `analysis_valid`/`mission_feasible`/`analysis_failed` 语义；`--n-valid`→`--n-tasks` 语义澄清（C3 D7） |
