# Phase 1 完成报告 — 基础设计分析闭环

> 状态：**已完成，验收测试全部通过（34/34）**。按规约暂停，等待确认后进入 Phase 2。
> 日期：2026-11-25

---

## 1. 交付内容

**命令行闭环**（8 秒/次完整分析）：

```
python main.py run config/mission.yaml
```

```
Mission → Geometry(重量迭代+布局) → OpenVSP(vsp3+STL) → XFOIL(2D极线)
→ VSPAero(整机极曲线) → CG配平 → Performance → Report(Markdown+图表) → SQLite
```

| 模块 | 文件 | 状态 |
|---|---|---|
| M01 Core | `src/core/{config,logger,workflow}.py`, `main.py` | ✅ |
| M02 Geometry | `src/geometry/{sizing,wing,tail,propeller,aircraft}.py` | ✅ |
| M03 Airfoil | `src/airfoil/uiuc_database.py` | ✅ 1665 翼型索引 |
| M04 OpenVSP | `src/openvsp/adapter.py` + AngelScript 模板 | ✅ 双模型方案 |
| M05 Aero | `src/xfoil/adapter.py`, `src/vspaero/adapter.py` | ✅ |
| M06 Performance | `src/performance/evaluator.py` | ✅ |
| M07 Database | `src/database/sqlite_manager.py` | ✅ 最小实现 |
| M08 Optimization | `src/optimization/optimizer.py` | 接口占位（Phase 3） |
| M11 Report | `src/report/generator.py` + `report.md.j2` | ✅ |

接口契约集中于 `src/core/contracts.py`（MissionSpec/Aircraft/AeroResult/AirfoilPolar/PerformanceResult），模块间零内部依赖。

## 2. Demo 案例结果（5kg 载荷 / 200km 航程 / 30 m/s 巡航）

| 指标 | 数值 | 约束 |
|---|---|---|
| MTOW | 21.7 kg | — |
| 翼展 / 面积 / AR | 2.46 m / 0.68 m² / 9.0 | — |
| CLα / CD₀ / L/D_max | 5.7 /rad / 0.015 / 19.6 | — |
| 失速速度 | 20.0 m/s | — |
| 巡航电功率 | 582 W | — |
| 航程 / 续航 | 349 km / 3.2 h | ✅ ≥200 km / ≥2.0 h |
| 静稳定裕度（配平后） | 0.100 | ✅ ∈ [0.05, 0.15] |
| 爬升率 | 2.2 m/s | ✅ ≥2.0 m/s |

产物（`data/results/<run_id>/`）：`aircraft.vsp3`（含机身，vspviewer 可开）、`aero.vsp3`（气动模型）、`aircraft.stl`、`aero.polar`、`clarky_polar.dat`（XFOIL）、`report.md` + 3 张图、`results.json`、全部中间脚本与日志。

## 3. 关键技术决策（开发中发现）

1. **双 vsp3 模型**：VSPAero 3.55 薄面元模式下，闭合 POD 体产生伪尾涡污染整场求解（GMRES NaN / 负诱导阻力）。解法：气动模型只含升力面（机翼+平尾+垂尾），完整模型含机身仅用于展示/导出。机身阻力将按寄生阻力法在 Performance 计入（Phase 2 完善）。
2. **上反角限制**：POD + 后掠且上反的机翼组合触发面元退化 → 气动模型中主翼上反角置 0（对纵向气动影响可忽略，横航向留待后续）。
3. **XFOIL 批处理**：必须文件重定向 stdin（管道会丢行）；`PACC` 后跟两个文件名提示；**ASEQ 参数序为 (起始角, 终止角, 步长)**；极线保存文件名必须短（Fortran 路径长度限制）。
4. **力矩列**：VSPAero `.polar` 中 `CMytot` 才是关于 Xcg 的俯仰力矩（`CMoy` 不可用）；Xcg 输入经实测有效（ΔCm=ΔCL·Δx/c̄ 精确吻合）。
5. **CG 配平**：按中性点将重心放到目标 SM（力矩线性转移，免重跑 VSPAero），物理对应电池/载荷布置。

## 4. 测试

`pytest tests/` — **34 通过**（含 3 个端到端集成测试，真实调用 OpenVSP/XFOIL）：
- 单元：配置合并、翼型解析（环绕序两种起点）、几何手算交叉验证、极线解析、性能公式手算验证
- 集成：CLI 端到端、API 端到端（产物完整性+数值合理性）、数据库入库读回

## 5. 已知局限（Phase 2 待改进）

1. 机身阻力未计入 CD₀（当前 CD0 来自升力面 VLM 拟合，偏乐观）→ 寄生阻力法补充。
2. VLM 无失速（CL_max 用 XFOIL 2D×0.9 折算，α>12° XFOIL 精度下降）。
3. XFOIL 高 α 收敛失败点直接跳过；CLmax 有 ~10% 不确定度。
4. 巡航高度 Re 用根弦估算，未按 MAC。
5. 重量估算用经验系数（空重 0.30、电池 220 Wh/kg），未分结构细节。

## 6. 下一步（Phase 2 预览，待确认）

- 自动布局生成（设计空间采样）+ 批量分析入 SQLite（目标 1~2 万组）
- 寄生阻力法（部件阻力累加）替换 CD₀ 估计
- 并行化批量运行 + 失败重试
