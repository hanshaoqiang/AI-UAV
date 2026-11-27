# PROJECT_MEMORY

> 下一阶段 AI 开发唯一必须阅读的项目记忆文件。保持精简，只保留当前开发必须知道的信息。

---

# Current Status

当前阶段：**Phase 2.5（数据生成质量优化与正式数据集构建）**

状态：**阶段C3 Stage B 完成**（C1 预筛接入 → C2 万级数据生成 → C3 Stage A 性能一致性诊断 → C3 Stage B 最小化修复 + Feature 冻结）

下一阶段：**阶段C3 Stage C（小规模验证）**，之后 **Phase 3**（代理模型 XGBoost/LightGBM/MLP + NSGA-II + 贝叶斯优化 + Top-K 推荐）

> 阶段门控：C3 Stage B 已按确认执行（修复 D1-D8 + Feature 冻结），等待确认后进入 Stage C 小规模验证。

> **C3 关键结论**：C2 续航达标率 0.3% 的根因是螺旋桨工作点求解 overshoot（250rpm 线性扫描 + 无功率约束），非推进效率/阻力偏差，已修复（Stage B）。AI 训练特征已冻结于 `docs/FEATURE_SCHEMA.md`（schema_version 1.0.0）。

---

# Implemented Modules

✔ Core（config / logger / workflow / batch_runner / parallel_runner）

✔ Geometry（sizing / wing / tail / propeller / aircraft）

✔ Airfoil（UIUC 1665 翼型索引）

✔ OpenVSP（AngelScript 批处理，双 vsp3 模型）

✔ XFOIL（2D 极线）

✔ VSPAero（整机极曲线）

✔ Propulsion（螺旋桨 / 电机 / 电池模型）

✔ ParasiteDrag（组件叠加寄生阻力）

✔ Performance（融合寄生阻力 + 推进闭环）

✔ Sampling（随机 + LHS + Sobol 采样器，25 维设计空间 YAML 驱动）

✔ Validation（DataValidator：六类校验 + 写库前过滤；FeasibilityPrefilter：可行性预筛 + 失败阶段分类）

✔ Dataset（DatasetMetadata 版本溯源 + DatasetGenerator 编排；预筛接入 + 拒绝记录不丢弃）

✔ Analysis（DataQualityAnalyzer：采样/筛选/物理/标签/数据库五类质量分析 + Markdown 报告）

✔ Benchmark（BenchmarkRunner：统计 + 绘图 + 报告）

✔ Database（SQLite 六表 + dataset_metadata + prefilter_rejections 表）

✔ Report（Markdown + 图表）

○ Optimization（接口占位，Phase 3）

○ AI / Agent / CFD（未实现）

---

# Confirmed Architecture

```
Mission
  ↓
Geometry（重量迭代 + 布局）
  ↓
Propulsion（螺旋桨 / 电机 / 电池）
  ↓
OpenVSP（双 vsp3：展示模型 / 气动模型）
  ↓
Aerodynamics（XFOIL 2D + VSPAero 整机 VLM + ParasiteDrag）
  ↓
Performance（失速 / 巡航功率 / 航程 / 续航 / 爬升 / 静稳定裕度）
  ↓
Optimization（Phase 3，接口已预留）
  ↓
Report
```

**Phase 2.5 数据生成链路（大规模数据集）**

```
DesignSampler（random/lhs/sobol，25 维）
  ↓
FeasibilityPrefilter（可行性预筛，毫秒级；硬约束过滤 + 软约束告警）
  ↓
DatasetGenerator（编排）
  ↓
ParallelBatchRunner（worker 计算 + 主进程单写者）
  ↓
DataValidator（写库前校验，error 级样本不入库）
  ↓
SQLite（六表 + dataset_metadata）→ 导出 YAML / JSON
```

---

# Important Design Decisions

- 双 OpenVSP 模型：展示模型含机身，气动模型仅升力面（闭合 POD 体污染 VLM 求解）
- Python 负责流程控制，AngelScript 仅负责几何生成（vspscript.exe 批处理）
- Propeller 作为一级设计变量（支持 1~4 桨、nose/tail/wing/distributed 布局）
- Performance 独立于 VSPAero，只消费 AeroResult 接口（为 CFD 替换预留）
- 寄生阻力采用组件叠加法，CD_total = CD_induced + CD_parasitic + CD_wave
- 所有模块统一接口契约，集中于 `src/core/contracts.py`
- SQLite 作为唯一数据库（六表 + dataset_metadata 七表）
- GUI 最后开发
- 推进采用快速动量理论模型（Phase 2 可升级 BEMT）
- **设计空间由 YAML 驱动**（`config/design_space.yaml`，25 维 = 21 连续 + 4 离散），禁止硬编码
- **根弦/梢弦/前后缘后掠为派生量**（面积 + 展弦比 + 锥比 + 后掠唯一确定，不独立采样）
- **翼型采样用精选白名单**（30 个收敛稳定翼型，写入 YAML），全库随机致收敛率下降
- **多进程采用 worker 计算 + 主进程单写者**（规避 SQLite 并发写竞争）
- **数据质量控制 = 写库前校验过滤**（DataValidator error 级样本不入库；warning 级标记不过滤）
- **阻力分解 = 诱导(k·CL²) + 寄生(组件叠加)，不重复计数**（VLM 极线拟合 CD0 截距为数值伪零升阻力，不可与组件叠加法叠加；定尺寸与完整分析复用 ParasiteDragModel）
- **prop_rpm 为一级设计变量并真正接入推进模型**（设计转速作为求解上限，区分 design/solve/operating 三转速；`thrust_available = min(转速受限推力, 功率受限推力)`）
- **可行性预筛在外部工具前运行**（FeasibilityPrefilter 纯 Python 毫秒级，硬约束过滤 + 软约束告警，记录原因与指标；预筛拒绝样本不丢弃，全量记录 design_id/变量/原因/阈值）
- **预筛硬约束仅保留物理硬性约束**（推力不足 + 推重比不足；桨尖速度/桨盘载荷降级为软约束告警，因快速估算与实际求解不一致、完整分析不检查这两项，经验阈值非绝对物理定律）
- **稳定性标签保留配平前原始信号**（`_trim_cg` 不覆盖配平前 Cmα/NP/CG/调整量，另存 `*_pre_trim` 字段，区分实际计算值与人为目标裕度）
- **失败阶段分类**（`geometry_infeasible` / `aero_failed` / `external_tool_failed` / `other_failed`，区分几何不可行/气动分析失败/外部软件失败）
- **任务输入与预测输出语义分离**（`mission_range_km` 任务输入 / `predicted_range_km` 模型预测 / `range_margin_km` 裕度）
- **螺旋桨工作点求解用二分 + 轴功率约束**（C3 D1/D2：`_solve_propeller` 二分替代 250rpm 线性扫描，消除风车区推力陡变导致的 overshoot；加 `p_max_shaft` 约束避免电机超载。只改求解器，不改物理公式/系数/约束）
- **桨尖速度区分设计/工作转速**（C3 D3：`tip_speed_design_ms` 设计转速 / `tip_speed_operating_ms` 工作转速；前者是约束上限，后者是物理真实值）
- **升阻比统一含寄生阻力口径**（C3 D4：`LD_max` VLM 仅诱导阻力 / `LD_max_total` 含寄生阻力；训练以 `LD_max_total` 为准，二者不可混用）
- **Feature Schema 冻结**（C3 D5：`docs/FEATURE_SCHEMA.md` schema_version 1.0.0，Phase 3 前不得改字段；区分 `analysis_valid`/`mission_feasible`/`analysis_failed`，任务不可行≠计算失败）

---

# Current Interfaces

**Geometry**：Input Mission → Output Aircraft

**OpenVSP**：Input Aircraft → Output vsp3

**XFOIL**：Input 翼型坐标 → Output AirfoilPolar

**VSPAero**：Input 气动 vsp3 → Output AeroResult（CL/CD/Cm vs α）

**ParasiteDrag**：Input Geometry + Reynolds + Mach + Surface → Output CD0

**Propulsion**：Input Propeller/Motor/Battery + 速度 → Output PropulsionResult

**Performance**：Input Aerodynamics + Propulsion → Output PerformanceResult

**DesignSampler**：Input DesignSpace（YAML）→ Output 设计字典（random/lhs/sobol_sample）

**DataValidator**：Input pipeline 结果 dict → Output ValidationResult（validate_result / filter）

**FeasibilityPrefilter**：Input mission + design → Output PrefilterResult（check；硬约束过滤 + 软约束告警 + 指标）

**DatasetGenerator**：Input config + n + method/seed → Output summary（generate；采样→并行→校验→导出）

**ParallelBatchRunner**：Input tasks + validator → Output stats（run；worker 计算 + 单写者）

**BenchmarkRunner**：Input config + n → Output summary + 图 + 报告（run）

**Database**：Input 六表数据 → 入库；`fetch_dataset_samples` / `fetch_dataset_elapsed` 读回

---

# Current Limitations

- BEMT 已实现（Decision 020），但**无实验/公开基准验证**（仅单元测试 + 预筛通过率校准）；失速后极线（>14°）为经验外推
- 波阻项已预留，但亚声速恒为 0（跨声速未覆盖）
- 寄生阻力形状因子为经验值，未对标实验数据标定
- VLM 无失速（CL_max 用 XFOIL 2D × 0.9 折算）
- 重量估算用经验系数（空重 0.30、电池 220 Wh/kg），未分结构细节
- 采样空间已校准至电推进可行域，扩展设计域需同步升级重量/电池模型
- 设计空间边缘组合会产生不可行设计（几何校验 / 推进不足），成功率约 30~40%，靠校验过滤 + 重试兜底；C2 实测 17242 采样整体有效率 42.8%（预筛通过率 59% × 完整分析有效率 72.7%）
- **气动模型限制**：主翼上反角在气动模型（VSPAero）中置 0（面元退化规避），`wing_dihedral_deg` 仅作为几何展示变量；sweep/twist 已真正传递到 OpenVSP，dihedral 气动贡献未计入（记入 `MODEL_LIMITATIONS` 元数据）
- **预筛阈值与 validation 阈值联动**（推重比下限 0.08 等），避免预筛放行、校验全拒的脱节
- **桨尖速度/桨盘载荷为软约束告警**（非硬性过滤），完整分析不检查这两项；`tip_speed_operating_ms`（工作转速）与 `tip_speed_design_ms`（设计转速）已区分（C3 D3）
- **BEMT 无压缩性修正**（桨尖马赫<0.7 可忽略）；工作桨尖马赫约 0.4，但设计转速下可达马赫 2（虚拟值，不反映真实工作点）
- **历史 C2 数据未用 C3 修复后的模型重算**：既有 7387 样本的 `ld_max_total` 列为 NULL，续航/功率标签仍含 overshoot 偏差（需 Stage C 决定是否重算）
- **static_margin_pre_trim 分布偏高**（mean 0.585 / max 1.362），待外部验证 VLM 中性点合理性

---

# Next Phase Goals

**阶段C3 Stage C（等待确认后启动）**：小规模验证（3~10 组端到端，验证功率/能量/续航一致性、桨尖速度/升阻比字段、SQLite/CSV/JSON 一致性、失败/预筛记录完整性），生成 `docs/Stage/09_C3_validation_report.md`。

1. 代理模型训练（XGBoost / LightGBM / MLP）——消费 Phase 2.5 数据集（特征见 `docs/FEATURE_SCHEMA.md`）
2. 多目标优化（NSGA-II，最大化航程/续航/升阻比、最小化 MTOW/功耗）
3. 贝叶斯优化 + Top-K 设计推荐
4. BEMT 实验基准对标（可选，当前无验证数据）
5. 寄生阻力经验系数对标实验标定（可选）
6. 历史 C2 数据用 C3 修复后模型重算（可选，需重跑完整分析）
