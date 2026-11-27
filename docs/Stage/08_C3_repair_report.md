# Phase 2.5 C3 — Stage B 最小化修复报告

> 阶段：Stage B（已获批准执行，最小化修复）
> 日期：2026-11-26
> 依据：`07_C3_diagnosis_report.md`（Stage A 诊断结论）

---

## 0. 修复总览

| 编号 | 问题 | 修复 | 文件 | 验证 |
|---|---|---|---|---|
| D1 | 工作点求解 overshoot | `_solve_propeller` 二分求解 | `src/propulsion/model.py` | ✅ 单元测试 |
| D2 | 电机超载 | 轴功率约束 | `src/propulsion/model.py` | ✅ 单元测试 |
| D3 | 桨尖速度标签误导 | 拆 `tip_speed_design_ms`/`tip_speed_operating_ms` | contracts/evaluator/sqlite/quality | ✅ 单元测试 |
| D4 | LD_max 口径不一致 | 新增 `LD_max_total` | contracts/evaluator/sqlite/quality | ✅ 单元测试 |
| D5 | 预筛记录缺失+统计口径 | 重放补录 3466 条 + 标注统计口径 | 新脚本 + quality | ✅ 补录完成 |
| D6 | 导出缺失 | CSV/JSON 导出 + 失败清单 | generator/config | ✅ 单元测试 |
| D7 | `--n-valid` 语义误导 | 改名 `--n-tasks` | run_c2.py | ✅ 语法 |
| D8 | failure 口径 bug | 修正判定逻辑 | quality.py | ✅ 逻辑复核 |

**测试**：完整套件 **168 passed**（原 162 + 新增 6）。
**数据库**：已备份 `data/uavdp.sqlite.bak_c3_stageB_20261126_203008`；预筛记录 3612 → 7078 条（可追溯重放恢复，非伪造）。

---

## 1. D1+D2：工作点求解 overshoot 与功率约束（核心）

**问题**：`_solve_propeller` 用 250 rpm 步进线性扫描，BEMT 推力在脱离风车区时陡变（0→几十/几百 N），导致推力/功率 overshoot 数倍；且无功率约束，电机超载。

**修复**（`src/propulsion/model.py`）：
- `_solve_propeller` 改用二分求解（20 次迭代，精度 <0.01 rpm），消除离散化 overshoot。
- 增加 `p_max_shaft` 参数（单桨电机额定轴功率 = `P_max_kw·1000·η_motor·η_esc`），轴功率超限时降到功率允许最大转速（复用 `_available_thrust` 二分）。
- `evaluate` 提前计算并传入 `p_max_shaft_each`。
- `_rpm_scan` 标记为历史废弃（保留但不再调用）。

**验证**（3 个 Stage A 样本复算）：

| 样本 | 修复前 overshoot | 修复后 overshoot | 修复前 P_shaft | 修复后 P_shaft |
|---|---|---|---|---|
| 15220(不足) | 9.40× | **1.00×** | 11584 W | 2878 W |
| 03480(中位) | 1.50× | **1.00×** | 121.9 W/桨 | 90.7 W/桨 |
| 15754(达标) | 1.02× | **1.00×** | 437 W | 435 W |

**说明**：修复后 overshoot 完全消除（1.00×）。样本 15220 的功率从 11.5kW 降到 2.9kW（但仍高于理想值，因 D=1.5m 大桨低转速效率仅 0.30，属真实物理现象，非求解器误差）。**未修改任何 BEMT 物理公式、经验系数、任务约束、预筛阈值。**

---

## 2. D3：桨尖速度字段拆分

**修复**：
- `PerformanceResult` 新增 `tip_speed_design_ms`（π·prop_design_rpm/60·D）与 `tip_speed_operating_ms`（π·prop_operating_rpm/60·D）。
- `evaluator` 计算两字段；数据库 `performances` 表新增两列（`_migrate` 幂等迁移）。
- `quality.py::extract_physical_row` 优先读入库字段，旧数据回退计算；`tip_speed_ms` 语义改为「工作桨尖速度」（物理真实值）。

**效果**：样本 `c2_10000_lhs_002302` 的 `tip_speed_design_ms=703.5 m/s`（马赫 2.07，设计转速虚拟值）与 `tip_speed_operating_ms=137.4 m/s`（马赫 0.40，真实工作值）明确区分。

---

## 3. D4：升阻比口径统一

**修复**：
- `PerformanceResult` 新增 `LD_max_total` = `max(CL/(k_induced·CL² + CD0_parasitic))`（含寄生阻力的最大升阻比）。
- `evaluator` 对极线各点补寄生阻力后取最大。
- 数据库 `performances` 表新增 `ld_max_total` 列。
- 保留 `LD_max`（VLM 纯诱导阻力，inviscid 参考），文档明确二者不可混用。

---

## 4. D5：预筛记录补录与统计口径

**补录**（新脚本 `scripts/backfill_prefilter_rejections.py`）：
- 重放 LHS 采样（seed=42, n=17242）+ 预筛（纯 Python 确定性），补录 7078 条拒绝记录。
- 结果：库中记录 **3612 → 7078** 条（补 3466 条，INSERT OR REPLACE 幂等，不覆盖既有记录）。

**补录后完整拒绝原因统计**（7078 条 → 10923 个标签，多标签率 1.543）：

| 原因 | 标签数 |
|---|---|
| 推重比不足 | 6190 |
| 推力不足 | 3845 |
| 几何/重量 | 888 |

**口径修正**（`quality.py::screening_quality`）：新增 `n_rejections_recorded`，明确「拒绝原因统计基于 DB 实际保留的记录数」，避免与 `n_rejected`（声称总数）混淆。

---

## 5. D6：数据导出

**修复**（`src/dataset/generator.py` + `config/dataset.yaml`）：
- `_export` 新增 CSV 导出（`_export_csv`：design_id/run_id + 扁平特征，对齐 FEATURE_SCHEMA）。
- `formats` 默认值 `[yaml, json]` → `[yaml, json, csv]`。

**失败清单文件化**：12 条失败样本（`c2_10000_lhs_01420` 等，`aero_failed`）已确认真实存在且不在成功集；`finish_c2_report.py` 的硬编码失败 ID 保留（本阶段不重写收尾脚本，Stage C 补文件化导出）。

---

## 6. D7：`--n-valid` 改名

**修复**（`scripts/run_c2.py`）：
- `--n-valid` → `--n-tasks`（`--n-valid` 保留为隐藏别名，`dest="n_tasks"`）。
- help/docstring 明确：`--n-tasks` 控制「进入完整分析的任务数」（预筛通过数），最终有效样本数 = n_tasks × 完整分析有效率（约 72%）。

---

## 7. D8：failure 口径修正

**修复**（`quality.py::database_quality`）：
- `failure_record_complete` 判定从 `len(failures) >= failed + rejected` 改为 `len(failures) >= failed`（仅对比真实失败数，不含校验拒绝）。

---

## 8. 数据库与历史数据处理

- **备份**：`data/uavdp.sqlite.bak_c3_stageB_20261126_203008`（141 MB）。
- **新增列**：`performances` 表新增 `ld_max_total` / `tip_speed_design_ms` / `tip_speed_operating_ms`（幂等 `_migrate`，ALTER TABLE ADD COLUMN）。
- **历史数据**：既有 7387 样本的这三列初始为 NULL（旧数据未重算），`extract_physical_row` 对旧数据自动回退计算（tip_speed 从 design/operating rpm 现算；LD_max_total 旧样本暂无，标记缺失）。**不覆盖、不伪造**。
- **预筛记录**：7078 条由确定性重放恢复（可追溯，非伪造）。

---

## 9. Feature 冻结

`docs/FEATURE_SCHEMA.md`（schema_version 1.0.0）已生成，冻结 AI 训练特征：
- 6 类 Feature（Mission/Geometry/Propulsion/Aerodynamics/Performance/Constraint）+ 数据集级语义（`analysis_valid`/`mission_feasible`/`analysis_failed`）。
- 每个 Feature 明确：名称、单位、类型、是否输入/输出/可训练/参与优化。
- 已包含 C3 新增字段 `tip_speed_design_ms`/`tip_speed_operating_ms`/`ld_max_total`。

---

## 10. 遗留问题（Stage C 验证或后续处理）

| 项 | 状态 |
|---|---|
| 端到端小样本验证（3~10 组） | Stage C 执行 |
| 历史 7387 样本的 `ld_max_total` 回填 | 需重算（Stage C 评估必要性） |
| BEMT 实验基准验证 | 未做（需外部数据） |
| 12 失败样本清单文件化 | Stage C 补 |
| `static_margin_pre_trim` 偏高（max 1.362） | 待外部验证 |

---

## 11. 修改文件清单

| 文件 | 变更 |
|---|---|
| `src/propulsion/model.py` | D1/D2 二分+功率约束 |
| `src/core/contracts.py` | PerformanceResult 新增 3 字段 |
| `src/performance/evaluator.py` | 计算 tip_speed/ld_max_total |
| `src/database/sqlite_manager.py` | performances 新增 3 列 + 写入 |
| `src/analysis/quality.py` | 字段提取 + 统计口径 + failure 口径 |
| `src/dataset/generator.py` | CSV 导出 |
| `config/dataset.yaml` | formats 加 csv |
| `scripts/run_c2.py` | `--n-tasks` 改名 |
| `scripts/backfill_prefilter_rejections.py` | 新增（补录） |
| `tests/test_c3_repair.py` | 新增（D1-D4 测试） |
| `tests/test_dataset_generator.py` | CSV 导出测试 |
| `docs/FEATURE_SCHEMA.md` | 新增（Feature 冻结） |

**Stage B 到此结束。已修改代码/数据库（备份齐全），未启动批量计算或代理模型训练。等待审核批准 Stage C。**
