# Phase 2.5 数据生成质量优化 — 阶段B 完成报告

> 阶段目标：在 Phase 2（大规模数据生成）基础上，审查并修复数据生成链路的物理一致性与标签质量问题，为正式大规模数据集构建与 Phase 3 代理模型训练提供可靠、无系统性偏差的数据。

> 阶段门控：阶段A（现状审查与问题诊断，不改代码）→ 用户确认技术决策 → 阶段B（开发/测试/Benchmark/报告）。本报告为阶段B交付物，完成后停止等待确认，未进入阶段C。

---

## 1. 阶段概览

| 项 | 结果 |
|---|---|
| 阶段A | 现状审查完成，定位 5 类问题（阻力/功率不一致、prop_rpm 未接入、稳定性标签覆盖、range_km 语义模糊、sweep/twist/dihedral 传递未核查） |
| 阶段B | 组合修复推进功率链路 + 设计变量处理 + 稳定性标签处理 + 可行性预筛 |
| 测试 | **133/133 通过**（Phase 2 的 118 + Phase 2.5 新增 15，无回归） |
| 预筛效果 | 毫秒级过滤 62% 明显不可行设计；pipeline 内有效率 50%→83% |
| 工具版本 | OpenVSP 3.55.0 / VSPAero 3.55.0 / XFOIL 6.99 / 平台 0.2.0 |

---

## 2. 阶段A 审查结论（问题诊断）

阶段A 未修改代码，仅审查现状，定位以下问题（均已被用户确认的技术决策覆盖）：

| # | 问题 | 影响 |
|---|---|---|
| A1 | 定尺寸 `sizing.py` 与完整分析 `evaluator.py` 的阻力/功率估算不一致 | 定尺寸结论与最终性能自相矛盾 |
| A2 | VLM 极线拟合 CD0 截距被重复计入巡航阻力（叠加组件叠加法寄生阻力） | 巡航阻力高估约一倍，剩余功率系统性为负 |
| A3 | `prop_rpm` 为设计变量但未真正接入推进模型 | 该变量在数据集中无因果影响，误导代理模型 |
| A4 | `_trim_cg` 重心配平覆盖原始稳定性信号 | static_margin 恒为 0.10，代理模型学不到真实静稳定裕度 |
| A5 | `range_km` 既作任务输入又作预测输出，语义模糊 | 字段混淆，下游训练歧义 |
| A6 | sweep/twist/dihedral 是否真正传递到 OpenVSP 未核查 | 几何变量可能名存实亡 |

---

## 3. 阶段B 修复与实现内容

### B1 — 阻力/功率估算对齐

**文件**：`src/geometry/sizing.py`、`src/performance/evaluator.py`、`src/core/config.py`

- 定尺寸复用 `ParasiteDragModel`，新增 `_cruise_drag_power()` 统一阻力/功率估算；机身/尾翼移入重量迭代环，保证寄生阻力自洽。
- **核心 bug 修复**：`_interp_polar` 改为返回诱导阻力 `k_induced·CL²`（VLM 仅贡献诱导阻力），不再返回含拟合截距 CD0 的原始极线 CD，避免与组件叠加法寄生阻力重复计数。
- `config.py` DEFAULTS 新增 `oswald_efficiency: 0.85`（可配，不放任意放大）。

### B2 — 可行性预筛（Prefilter）

**文件**：`src/validation/prefilter.py`（新增）、`config/prefilter.yaml`（新增）

- 纯 Python 毫秒级，不调用外部工具。
- **硬约束**（不满足 → 提前过滤）：桨尖速度（π·n·D ≤ 220 m/s）、桨盘载荷（轴功率/桨盘面积 ≤ 4000 W/m²）、推力不足（T_avail ≥ T_req）、推重比不足（T/W ≥ 0.08）。
- **软约束**（不满足 → 告警不删除）：航程预估不足、推重比偏低、失速裕度不足。
- 每个过滤样本记录 `hard_failures`（name/detail/value/limit）与 `metrics`（mtow/翼面积/翼展/CL_cr/L/D/推力/桨尖速度/桨盘载荷/失速/航程预估等）。
- 阈值与 `validation.yaml` 联动（推重比下限 0.08 等），避免预筛放行、校验全拒的脱节。
- 采样器 `random_sample`/`lhs_sample`/`sobol_sample` 统一支持 `feasibility_check`/`max_attempts` 回调。

### B3 — prop_rpm 真正接入推进模型

**文件**：`src/propulsion/model.py`

- 设计转速作为推进求解的**上限约束**；`_rpm_scan` 生成转速扫描序列。
- `thrust_available = min(总设计转速推力, 功率受限推力)`。
- 记录三转速：`prop_design_rpm`（设计输入）/ `prop_solve_rpm`（求解值）/ `prop_operating_rpm`（实际工作值）。
- **修正静止推力系数**：`ct_static = 0.10`（标准 UAV 桨经验值；原 `0.10·pitch_ratio` 系统性偏低约一半）。

### B4 — 25 维映射核查 + 模型限制元数据

**文件**：`src/dataset/metadata.py`、`src/openvsp/adapter.py`

- 核查确认：`wing_sweep_deg` / `wing_twist_deg` / `wing_dihedral_deg` 均已真正传递到 OpenVSP。
- **气动模型限制**：主翼上反角在气动模型（VSPAero）中置 0（规避面元退化），`wing_dihedral_deg` 仅作几何展示变量，其气动贡献未计入——通过 `MODEL_LIMITATIONS` 注入 `metadata.extra` 显式记录。

### B5 — range_km 字段语义区分

**文件**：`src/database/sqlite_manager.py`

- 任务输入 `mission_range_km` / 模型预测 `predicted_range_km` / 裕度 `range_margin_km = predicted − mission` 三者区分，入库与读回（`fetch_dataset_samples`）均导出。

### B6 — 稳定性标签保留配平前信号

**文件**：`src/core/workflow.py`、`src/core/contracts.py`

- `_trim_cg` 在力矩线性转移前保留配平前原始信号，不覆盖：`Cm_alpha_pre_trim_per_rad` / `neutral_point_x_pre_trim_m` / `cg_x_pre_trim_m` / `cg_shift_m` / `static_margin_target`。
- `AeroResult` / `PerformanceResult` 新增对应字段；数据库 schema 幂等迁移（`_migrate` + `ALTER TABLE ADD COLUMN`）。
- 区分「实际计算结果」与「人为设定目标裕度（0.10）」，代理模型可学习真实静稳定裕度分布。

---

## 4. 设计变量处理汇总

| 变量 | 处理方式 |
|---|---|
| `range_km` | 保留为 Mission 任务输入；区分 `mission_range_km` / `predicted_range_km` / `range_margin_km` |
| `prop_rpm` | 保留为一级设计变量并真正接入推进模型（design/solve/operating 三转速） |
| `prop_diameter_m` | 保留独立变量，新增桨盘载荷 + 桨尖速度检查 |
| `wing_dihedral_deg` | 保留几何变量；气动模型置 0（模型限制记入元数据） |
| `wing_sweep_deg` / `wing_twist_deg` | 已核查真正传递到 OpenVSP |

---

## 5. 测试结果

完整测试套件：**133/133 通过**（无回归）。

Phase 2.5 新增 15 个测试：

- `tests/test_prefilter.py`（7）：配置加载、可行设计、桨尖速度拒绝、桨盘载荷拒绝、重量发散、原因记录、采样器集成。
- `tests/test_phase2_5.py`（8）：`_trim_cg` 保留配平前信号、性能稳定性字段、prop_rpm 敏感性、定尺寸用 ParasiteDragModel、sweep/twist/dihedral→OpenVSP、模型限制元数据、`_interp_polar` 诱导阻力、静止推力系数。
- 回归修正：`tests/test_performance.py::test_cruise_interpolation`（旧断言基于含 CD0 截距的阻力，已更新为 `CD = k·CL² + CD0_parasitic` 的正确物理分解）。

---

## 6. 小规模真实链路 Benchmark（前后对比）

### 6.1 Phase 2（优化前，模块 6 基准）

| 指标 | 值 |
|---|---|
| 请求样本数 | 8 |
| 结果 | 3 success + 1 failed + 4 rejected |
| 成功率 | 37.5% |
| pipeline 内有效率 | ~50% |

### 6.2 Phase 2.5（优化后，16 组 lhs）

| 指标 | 值 |
|---|---|
| 采样 | 16 组 |
| 预筛通过 | 6 组（38%），耗时 **0.002s** |
| 预筛过滤 | 10 组（62%）：桨尖速度 6、推重比不足 5、推力不足 3、桨盘载荷 1 |
| 真实 pipeline（进入 6 组） | 5 success + 1 rejected + 0 failed |
| **有效率（valid/进入 pipeline）** | **83%**（5/6） |
| 有效率（valid/采样） | 31%（5/16） |
| 几何通过率 | 38%（6/16） |
| 墙钟时间 | 18.1s（4 worker） |

### 6.3 结论

- **预筛在毫秒级过滤 62% 明显不可行设计**，避免浪费 OpenVSP/XFOIL/VSPAero 计算资源。
- **pipeline 内有效率从 ~50% 提升到 83%**，预筛通过的设计绝大多数最终有效。
- 过滤原因可解释（桨尖速度/桨盘载荷/推力/推重比），指标可回溯，支持后续设计空间校准。
- 推进不足不再被「改写为成功样本」；预筛与 DataValidator 阈值联动，杜绝「放行即拒绝」的脱节。

---

## 7. 关键设计决策（新增 ADR 013~016）

| 编号 | 决策 | 要点 |
|---|---|---|
| 013 | 阻力分解不重复计数 | 诱导 = k·CL²，寄生 = 组件叠加法，VLM CD0 截距不入总阻力 |
| 014 | prop_rpm 接入推进模型 | 设计转速为求解上限，min(转速受限, 功率受限)，三转速记录 |
| 015 | 预筛先于外部工具 | FeasibilityPrefilter 毫秒级硬/软约束过滤，阈值与校验联动 |
| 016 | 稳定性标签保留配平前信号 | `*_pre_trim` 字段另存原始信号，区分计算值与目标裕度 |

完整记录见 `docs/DESIGN_DECISIONS.md`（013~016）、`docs/LESSONS_LEARNED.md`（Phase 2.5 段）。

---

## 8. 已知局限与后续建议

1. **推进模型仍为动量理论快速模型**（BEMT 未实现）；`ct_static = 0.10` 为标准 UAV 桨经验值，高精度需求可升级 BEMT。
2. **气动模型主翼上反置 0**（面元退化规避），dihedral 气动贡献未计入，已记入 `MODEL_LIMITATIONS`。
3. **波阻亚声速恒为 0**，跨声速未覆盖。
4. **寄生阻力形状因子为经验值**，可对标实验标定。
5. **预筛尚未在 `DatasetGenerator._sample` 中默认开启**（当前作为独立模块 + 采样器回调提供），阶段C 正式大规模生成时建议接线 `make_prefilter_check`。
6. **正式大规模生成（1~2 万组）留待阶段C**：建议按 `parallel.max_workers`（本机 32 核）配置并行，精简模式（跳过 STL/报告）由 DatasetGenerator 支撑。

---

> 阶段B 完成。按阶段门控要求，此处停止，等待确认后再进入阶段C（正式大规模数据集生成）。
