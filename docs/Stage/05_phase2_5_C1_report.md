# Phase 2.5 阶段C1 完成报告 — 预筛接入与 100 组试验数据质量验证

> 阶段目标：将可行性预筛（FeasibilityPrefilter）正式接入数据生成采样流程，用 100 组 LHS 试验数据验证预筛的接入正确性与数据质量，为阶段C2（正式大规模数据集生成）铺路。

> 阶段门控：本阶段**只执行 C1**（C1.1 预筛接入 → C1.2 误杀验证 → C1.3 稳定性标签核查 → C1.4 100 组 LHS 生成 → C1.5 数据质量分析 → C1.6 测试验收）。完成后停止等待确认，未进入 C2、未做大规模生成、未做代理模型训练。

---

## 1. 阶段概览

| 项 | 结果 |
|---|---|
| C1.1 预筛接入 | 已接入 `DatasetGenerator` 正式采样流程，预筛在 OpenVSP/XFOIL/VSPAero 外部工具前运行，开关可配置 |
| C1.2 预筛误杀验证 | 抽样 10 组预筛拒绝样本完整分析，误杀率 **0.0%**（两轮迭代降级桨尖速度/桨盘载荷为软约束） |
| C1.3 稳定性标签核查 | `static_margin`（配平后人为目标）与 `static_margin_pre_trim`（配平前实际计算值）字段真实存在、计算正确、正确入库 |
| C1.4 100 组 LHS 生成 | 100 采样 → 34 预筛拒绝 → 66 完整分析 → 55 成功 + 11 校验拒绝 + 0 失败 |
| C1.5 数据质量分析 | A 采样 / B 筛选 / C 物理 / D 标签 / E 数据库 五类全部完成 |
| C1.6 测试 | **143/143 通过**（Phase 2 的 118 + 阶段B 的 15 + C1 新增 10） |
| 工具版本 | OpenVSP 3.55.0 / VSPAero 3.55.0 / XFOIL 6.99 / 平台 0.2.0 |

---

## 2. 修改文件清单

### 2.1 新增文件

| 文件 | 说明 |
|---|---|
| `src/analysis/quality.py` | `DataQualityAnalyzer`（五类质量分析）+ `build_quality_report()`（Markdown 报告） |
| `src/analysis/__init__.py` | analysis 包 |
| `scripts/run_c1.py` | C1 执行脚本（100 组 LHS + 误杀验证 + 质量分析 + 报告输出） |
| `tests/test_phase2_5_C1.py` | C1 新增 9 个测试 |

### 2.2 修改文件

| 文件 | 修改内容 | 修改原因 |
|---|---|---|
| `src/database/sqlite_manager.py` | 新增 `prefilter_rejections` 表 + `record_prefilter_rejection()` / `fetch_prefilter_rejections()` / `count_prefilter_rejections()`；修复 `_fetch_sample` 读回 `model_limitations` | 预筛拒绝样本不丢弃、全量入库；修复元数据读回 bug |
| `src/core/parallel_runner.py` | 新增 `classify_failure_str()` 失败阶段分类 + stats 计数 | 区分几何不可行/气动失败/外部软件失败 |
| `src/dataset/generator.py` | 接入 `FeasibilityPrefilter`，`generate()` 新增 `prefilter_enabled` 参数；采样后预筛、拒绝记录不丢弃；summary 新增 `prefilter` 段 | C1.1 预筛接入正式流程 |
| `config/dataset.yaml` | 新增 `prefilter.enabled: true` + `prefilter.record_rejected: true` | 预筛开关与拒绝记录开关 |
| `config/prefilter.yaml` | 桨尖速度/桨盘载荷从 hard 降级为 soft | C1.2 误杀验证结论 |
| `src/validation/prefilter.py` | 硬/软约束结构调整（hard 仅保留推力+推重比） | C1.2 误杀验证结论 |
| `src/benchmark/benchmark.py` | `run()` 新增 `prefilter_enabled` 透传 | 保持 Benchmark 与生成器一致 |
| `tests/test_prefilter.py` | 桨尖速度/桨盘载荷改告警断言 + 新增推力不足拒绝测试 | 同步硬/软约束变更 |
| `tests/test_dataset_generator.py` / `tests/test_benchmark.py` | mock 测试添加 `prefilter_enabled=False` | 隔离预筛，避免 mock 测试耦合预筛 |

---

## 3. 技术实现要点

### 3.1 C1.1 预筛接入（`src/dataset/generator.py`）

- `DatasetGenerator` 新增 `prefilter` property（懒加载 `FeasibilityPrefilter`）与 `_prefilter_enabled()`（显式参数 > config > 默认 `true`）。
- `generate()` 新增 `prefilter_enabled` 参数；采样完成后、构建任务前执行预筛：
  - 通过 → 进入完整分析任务（保留 Random/LHS/Sobol 采样）。
  - 拒绝 → **不丢弃**，调用 `_record_rejections()` 记录到 `prefilter_rejections` 表并导出 JSON（含 design_id、原始设计变量、失败原因、具体约束指标、对应阈值、样本生成时间）。
- summary 新增 `prefilter` 段（n_sampled / n_rejected / n_passed / rejections）。
- 预筛拒绝样本与完整分析的 success/rejected/failed 严格分离，不伪装失败、不凑数。

### 3.2 C1.2 预筛误杀验证（`scripts/run_c1.py::run_misclassification_check`）

- 从预筛拒绝样本抽样 ≥10 组，用**独立数据目录 + 关闭入库**的 `DesignPipeline` 跑完整分析，避免污染正式数据集。
- 统计误杀数与误杀率，对误杀样本逐条分析原因。
- **两轮迭代结论**：
  - 第 1 轮：桨尖速度硬约束误杀率 **42.9%**（7 组抽样中 3 组被确认可行）→ 原因：设计转速是推进求解上限，实际工作转速被节流下调，预筛高估实际桨尖速度 → 降级为软告警。
  - 第 2 轮：桨盘载荷单独硬约束误杀（2/2）→ 原因：按电机最大功率计（实际巡航功率远低），且完整分析不检查盘载荷 → 降级为软告警。
  - 最终误杀率 **0.0%**（10/10 确认不可行）。

### 3.3 C1.3 稳定性标签核查

- 确认 `static_margin`（配平后恒 = 目标 0.10，**人为设定值**）与 `static_margin_pre_trim`（配平前**实际计算值**）字段真实存在、计算正确、正确入库。
- 不允许把目标静稳定裕度当实际标签；不可靠字段已在 `MODEL_LIMITATIONS` 元数据标记。
- 迁移兼容：旧表通过 `_migrate` + `ALTER TABLE ADD COLUMN` 幂等补列。

### 3.4 失败阶段分类（`src/core/parallel_runner.py`）

```python
def classify_failure_str(error: str | None) -> str:
    msg = error or ""
    if ("几何" in msg) or ("重量迭代" in msg) or ("未收敛" in msg):
        return "geometry_infeasible"
    if ("VSPAero" in msg) or ("极线" in msg) or ("无法解析" in msg):
        return "aero_failed"
    if ("vsp3" in msg) or ("vspscript" in msg) or ("xfoil" in msg) or ("XFOIL" in msg):
        return "external_tool_failed"
    return "other_failed"
```

### 3.5 任务输入与预测输出语义分离

- `mission_range_km`（任务输入）/ `predicted_range_km`（模型预测）/ `range_margin_km`（裕度）。
- 稳定性标签区分 `static_margin`（配平后人为目标）与 `static_margin_pre_trim`（配平前实际计算值）。

---

## 4. 100 组 LHS 试验数据生成结果（C1.4）

正式设计空间 + 预筛开启 + 真实工具链路 + 多进程（8 workers）+ 断点恢复：

| 阶段 | 数量 |
|---|---|
| LHS 采样 | 100 |
| 预筛拒绝 | 34（推力不足×22、推重比不足×32、几何/重量×2） |
| 预筛通过（进入完整分析） | 66 |
| 完整分析成功 | 55 |
| 校验拒绝（性能不满足） | 11 |
| 失败（几何/外部工具/气动） | 0 |

- 预筛通过率：**66.0%**
- 完整分析有效率（valid/通过）：**83.3%**
- 整体有效率（valid/采样）：**55.0%**
- 断点恢复：`state.json` 记录进度，`INSERT OR REPLACE` 幂等。

---

## 5. 数据质量分析（C1.5）

### A. 采样质量

- 100 组 LHS，25 维设计空间；连续变量无缺失；离散变量（airfoil_root 24 翼型、airfoil_tip 29 翼型、prop_layout nose/tail/distributed/wing 4 布局）频数均衡。

### B. 筛选质量

- 预筛通过率 66.0%、完整分析有效率 83.3%、整体有效率 55.0%（见上）。
- 预筛误杀率 **0.0%**（抽样 10 组完整分析交叉验证）。
- 失败阶段分类：geometry_infeasible×0 / aero_failed×0 / external_tool_failed×0 / other_failed×0。

### C. 物理合理性（55 有效样本，均值/区间）

| 指标 | 均值 | 最小 | 最大 |
|---|---|---|---|
| MTOW | 11.66 kg | 1.35 | 62.21 |
| 翼展 | 1.77 m | 0.70 | 3.28 |
| 展弦比 | 9.83 | 6.58 | 13.87 |
| 桨尖速度 | 256.1 m/s | 99.5 | 436.9 |
| 桨盘载荷 | 770.1 W/m² | 57.5 | 7502.7 |
| 推重比 | 0.112 | 0.082 | 0.165 |
| 巡航功率 | 341.3 W | 25.0 | 3080.4 |
| 失速速度 | 20.1 m/s | 12.9 | 28.2 |
| 航程 | 219.1 km | 94.1 | 418.8 |
| 静稳定裕度（配平前） | 0.586 | 0.176 | 1.314 |

- 静稳定裕度（配平后）恒 = 0.10（人为目标，非真实标签），已区分。

### D. 标签质量

- 样本数 55、缺失值字段 0、重复设计 0、异常值字段 1（`disk_loading_w_m2`，高值离群，软告警未过滤）。
- **模型限制已记录样本数 55/55**（`MODEL_LIMITATIONS` 正确读回）。
- 变量-物理因果显著：`aspect_ratio→ld_max` 0.922、`prop_rpm→tip_speed_ms` 0.75、`payload_kg→mtow_kg` 0.703、`aspect_ratio→ld_cruise` 0.574。
- 任务输入/预测输出一致性：55/55 样本 `range_margin_km = predicted - mission` 自洽。

### E. 数据库质量

- 库中样本数 = 统计成功数 = 55，读写一致 True。
- design_id 唯一 True；失败记录完整 True；元数据完整 55/55；断点恢复一致 True。

---

## 6. 测试结果（C1.6）

- **143/143 通过**（41.2s），无回归。
- C1 新增 `tests/test_phase2_5_C1.py` 9 个测试覆盖：预筛拆分与拒绝记录完整性、预筛开关、配置控制、端到端记录到 DB+JSON、失败分类、稳定性标签往返、任务输入/预测输出不混淆、断点恢复幂等。
- 预筛开关开/关均已测试；真实外部工具、多进程、SQLite 单写者、断点恢复均有覆盖。

---

## 7. 已知问题

1. **桨尖速度/桨盘载荷已降级为软告警**（非硬过滤）：完整分析 DataValidator 不检查这两项，快速预筛估算与实际求解不一致。数据集中这两项分布含高值离群点（如盘载荷 7502 W/m²），代理模型训练时应知晓其非硬约束。
2. **推进为动量理论快速模型**（非 BEMT/CFD）：推力/功率/航程为快速模型预测值，不应表述为高精度真实飞行数据（已记入 `MODEL_LIMITATIONS`）。
3. **`CL_max` 为 VSPAero alpha 扫描末点线性外推值**（涡格法不失速），非真实失速升力系数；真实 2D 失速见 airfoil_polar（暂未入库）。
4. **整体有效率 55%**（预筛通过率 66% × 完整分析有效率 83.3%）：C2 大规模生成时需按此有效率规划采样规模；不可为凑数放宽约束或修改设计空间。

---

## 8. 下一阶段建议（阶段C2）

1. **正式大规模数据集生成**：复用 C1 的预筛 + 多进程 + 断点恢复 + SQLite 单写者框架，扩展样本量与采样方法（Random/LHS/Sobol 混合），持续监控预筛通过率/完整分析有效率/失败阶段分布。
2. **若需恢复桨尖速度/桨盘载荷硬过滤**：先让完整分析真正计算并验证这两项（与预筛估算对齐），再做硬过滤，避免重蹈误杀覆辙。
3. **代理模型训练前**：剔除或标记 `static_margin`（恒为 0.10 的常量列），训练标签用 `static_margin_pre_trim`；输入用 `mission_range_km`，预测目标用 `predicted_range_km`。
4. **推进模型升级 BEMT（可选）**：提高推力/功率/航程精度，替换动量理论快速模型。

---

## 9. 交付物

- 报告：`docs/Stage/05_phase2_5_C1_report.md`（本文件）
- 质量报告：`reports/c1/c1_100_lhs_quality_report.md`
- 分析结果：`reports/c1/c1_100_lhs_analysis.json`
- 试验数据：`data/datasets/c1_100_lhs.{json,yaml}`、`c1_100_lhs_prefilter_rejected.json`、`c1_100_lhs_failures.json`、`c1_100_lhs_state.json`
- 执行脚本：`scripts/run_c1.py`
- 项目记忆：`PROJECT_MEMORY.md` / `LESSONS_LEARNED.md` / `CHANGELOG.md` / `DESIGN_DECISIONS.md` 已更新
