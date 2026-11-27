# Phase 1.5 数据质量增强阶段 — 完成报告

> 阶段目标：完善气动-推进-性能闭环，提高数据库质量，为后续 AI 代理模型与多目标优化提供可靠数据。

---

## 1. 模块完成情况

### 1.1 寄生阻力模型（ParasiteDragModel）

**文件**：`src/performance/parasite_drag.py`

- 独立接口，不修改 VSPAero 模块。
- 组件叠加法（component buildup）：`CD0 = Σ(C_f · FF · Q · S_wet) / S_ref`。
- 组成：机翼摩阻、机身摩阻、平尾摩阻、垂尾摩阻、设备/外露部件阻力。
- 摩擦系数：Prandtl-Schlichting 湍流公式（含可压缩修正），低雷诺数回退层流 Blasius。
- 输出 `ParasiteDragResult`：CD0 + 分项明细 + 浸润面积。
- 融合方式：PerformanceEvaluator 中 `CD_total = CD_induced(VLM) + CD_parasitic + CD_wave`。

### 1.2 推进系统闭环（PropulsionModel）

**文件**：`src/propulsion/model.py`

- 独立接口，不修改 Geometry 模块；Propeller 作为一级设计变量。
- 螺旋桨布局：数量 1~4，安装形式 nose/tail/wing/distributed。
- 推力/功率模型：动量理论 + 经验修正（CT/CP 系数法或螺距经验法）。
- 电机效率模型：负载比经验曲线（峰值 60~80% 负载）。
- 电池能量模型：`E = U·C·usable_fraction`。
- 输出 `PropulsionResult`：推力/轴功率/电功率/效率/能耗。

### 1.3 Mission-Performance 接口完善

数据库每条设计含完整链路：

- **Mission**：Range / CruiseSpeed / Payload / Altitude / Endurance
- **Geometry**：Wing / HTail / VTail / Fuselage / Propeller layout 参数
- **Propulsion**：Propeller / Motor / Battery 参数
- **Aerodynamics**：CL / CD / Cm / L/D
- **Performance**：MTOW / CruisePower / RequiredThrust / AvailableThrust / Range / Endurance / StallSpeed / StaticMargin / ClimbRate

### 1.4 数据生成框架（DesignSampler）

**文件**：`src/sampling/design_sampler.py`

- 随机采样（Random）与拉丁超立方采样（LHS）。
- 设计空间：9 连续变量 + 2 离散变量（n_props / prop_layout）。
- 输出标准化 Mission/Geometry/Propulsion YAML。

### 1.5 批处理框架（BatchRunner）

**文件**：`src/core/batch_runner.py`

- 多任务运行、失败自动重试（默认 2 次）、日志、中断恢复（断点续跑）、结果自动入库。
- 不追求 GPU 并行，优先稳定性。

### 1.6 数据库结构优化

**文件**：`src/database/sqlite_manager.py`

六表结构：`missions` / `geometries` / `propulsions` / `aerodynamics` / `performances` / `run_history`，均含扁平化常用标量列 + full_json，支持代理模型训练与多目标优化。

---

## 2. 数据库结构

（六表字段详表，见下）

| 表 | 关键字段 |
|---|---|
| missions | range_km, cruise_speed_ms, payload_kg, endurance_h, altitude_m ... |
| geometries | span_m, wing_area_m2, aspect_ratio, taper_ratio, tail, fuselage ... |
| propulsions | n_props, prop_layout, diameter, pitch, rpm, motor, battery ... |
| aerodynamics | cl_alpha, cd0_vlm, cd0_parasitic, cd0_total, k, ld_max ... |
| performances | v_stall, cl/cd_cruise, thrust_req/avail, power, range, endurance, SM ... |
| run_history | status, attempts, elapsed_s, error_msg, batch_id ... |

---

## 3. 测试结果

- 单元/集成测试：`pytest` **49/49 通过**（含 14 个 Phase 1.5 新增测试）。
- 寄生阻力、推进模型、采样器均有手算/物理交叉验证。

## 4. 100 组验证结果

| 指标 | 结果 |
|---|---|
| 采样方式 | 拉丁超立方采样（LHS, seed=20261125） |
| 生成成功率（几何+重量可行性） | **100%**（100/100，含可行性预筛） |
| 分析成功率（完整链路） | **100%**（100/100，0 失败） |
| 平均计算时间 | **8.5 s/组**（总计 851 s） |
| 入库记录 | run_history=100, missions/geometries/propulsions/aerodynamics/performances 各 100 |

### 性能指标分布（100 组）

| 指标 | min | max | mean |
|---|---|---|---|
| 航程 range_km | 96.4 | 420.0 | 189.2 |
| 续航 endurance_h | 0.55 | 3.31 | 1.63 |
| 失速速度 v_stall (m/s) | 11.7 | 26.1 | 18.8 |
| 静稳定裕度 SM | 0.10 | 0.10 | 0.10（配平目标） |
| 爬升率 ROC (m/s) | 0.0 | 3.92 | 1.15 |
| 可用推力 (N) | 2.0 | 414.8 | 37.0 |
| 巡航电功率 (W) | 72.0 | 12364.1 | 1661.5 |

指标均在合理物理范围：航程 96~420 km、失速 12~26 m/s、巡航功率 72~12 kW，覆盖小型无人机设计域。

## 5. 典型设计案例

样本 `20261125_145851_sampled_be2300`（中等 MTOW 代表）：

| 链路 | 关键参数 | 值 |
|---|---|---|
| **Mission** | 航程/巡航速度/载荷 | 246 km / 36.5 m/s / 2.06 kg |
| | 续航/高度 | 1.81 h / 1441 m |
| **Geometry** | 翼展/机翼面积/展弦比 | 1.55 m / 0.354 m² / 6.77 |
| | MTOW | 15.34 kg |
| **Propulsion** | 螺旋桨数量/布局 | 1 / distributed |
| | 直径/电机功率 | Ø0.68 m / 1.58 kW |
| **Aerodynamics** | CLα / L/D_max | 5.08 /rad / 17.7 |
| **Performance** | 巡航 L/D / 需用推力 / 可用推力 | 13.7 / 11.0 N / 15.0 N |
| | 巡航功率 / 航程 / 续航 | 1371 W / 183 km / 1.18 h |
| | 失速速度 / 静稳定裕度 | 23.3 m/s / 0.10 |

---

## 6. 数据质量增强的关键修复

Phase 1.5 批量验证暴露并修复了以下底层数据问题（这些是 Phase 1 单案例未触发的）：

1. **尾臂不足**：高展弦比下机身长度被 `0.45·翼展` 上限钳制，导致尾臂为负 → 机身长度增设 MAC 下限 `L ≥ 2.25·c̄/0.58`（保证尾臂 ≥ 2·c̄）。
2. **重量迭代发散**：电推进重量迭代 `mtow = payload + a·mtow`，续航 > 2.5h（电池 220Wh/kg）时 `a≥1` 发散 → 加发散防护 + 收紧续航采样范围。
3. **收敛过慢**：`a→1` 时松弛迭代收敛极慢 → 迭代上限 60→300、容差放宽至 0.01。
4. **尾翼面积比超标**：高展弦比下垂尾面积比偏大 → 校验边界与采样范围校准。
5. **可行性预筛**：采样后执行 `size_aircraft + validate` 预筛，过滤边界不可行组合。

## 7. 已知局限与 Phase 2 建议

1. 螺旋桨为动量理论快速模型（未做高精度 CFD/叶素法），Phase 2 可升级为 BEMT。
2. 寄生阻力用组件叠加经验公式（形状因子为经验值），可对标实验数据标定。
3. 波阻项已预留但亚声速恒为 0（跨声速无人机暂不覆盖）。
4. 批处理为顺序执行（单机约 8.5s/组），Phase 2 大规模生成（1~2 万组）建议加多进程并行（预计可 10× 加速）。
5. 采样空间已校准至电推进（220Wh/kg）可行域；Phase 2 若需覆盖更大设计域，需同步扩展重量迭代模型或电池技术参数。
