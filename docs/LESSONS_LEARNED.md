# LESSONS_LEARNED

> 记录开发经验，避免重复踩坑。**永远追加，永远不要删除旧记录。**

---

## 2026-11-25

### OpenVSP

问题：闭合 POD 机身导致 VSPAero 求解异常（伪尾涡污染整场，GMRES NaN / 负诱导阻力）。

原因：闭合体进入 VLM 薄面元求解，面元退化产生伪涡。

解决：建立双 VSP 模型——展示模型含机身，气动模型仅升力面（机翼 + 平尾 + 垂尾）。

经验：展示模型与分析模型必须分离；机身阻力不在 VLM 里算，转由寄生阻力法在 Performance 层计入。

---

### OpenVSP / 面元退化

问题：POD + 后掠 + 上反的机翼组合触发面元退化 → NaN。

原因：几何组合导致面元质量下降。

解决：气动模型中主翼上反角置 0（对纵向气动影响可忽略）。

经验：几何参数组合可能触发求解器数值问题，需在批量采样前做可行性预筛。

---

### XFOIL

问题：批处理调用丢行 / 参数顺序错 / 文件名过长报错。

原因：交互式 stdin 管道会丢行；ASEQ 参数序误用；Fortran 路径长度限制。

解决：stdin 必须用文件重定向；PACC 后跟两个文件名提示；ASEQ 参数序为 (起始角, 终止角, 步长)；极线保存文件名要短。

经验：ASEQ 参数：start → end → step（起、止、步）。低雷诺数/特定翼型不收敛时限制迭代次数，失败降级用经验公式，不阻塞流程。

---

### VSPAero / 力矩列

问题：俯仰力矩列选错。

原因：`.polar` 文件中 CMoy 不可用。

解决：CMytot 才是关于 Xcg 的真俯仰力矩；Xcg 输入经实测有效（ΔCm = ΔCL·Δx/c̄ 精确吻合）。

经验：解析 VSPAero 输出前，先用物理关系交叉验证列含义。

---

### Geometry / 尾臂为负

问题：高展弦比下机身长度被 `0.45·翼展` 上限钳制，导致尾臂为负（成功率 11% 主因）。

原因：尾臂 = 0.58·L_fus − 0.25·c̄，当 L_fus 被钳制过短时尾臂 < 0。

解决：机身长度增设下限 `L ≥ 2.25·c̄ / 0.58`，保证尾臂 ≥ 2·c̄。

经验：批量采样会暴露单案例未触发的布局缺陷；用公式分析（而非试错）定位根因。

---

### Geometry / 重量迭代发散

问题：电推进重量迭代 MTOW 爆炸（可达 4.5e14）。

原因：迭代本质 `mtow = payload + a·mtow`，其中 a = 0.30 + k1 + k2；续航 > 2.5h（电池 220Wh/kg）时 a ≥ 1，无固定点。

解决：加发散防护（mtow 上限检查）+ 收紧续航采样范围到 [1, 2.5]h。

经验：迭代收敛性问题要先做数学分析（判断是否存在固定点），再决定是否调整采样空间或模型。

---

### Geometry / 收敛过慢

问题：a → 1 时松弛迭代收敛极慢。

原因：固定点迭代在 a 接近 1 时线性收敛极慢。

解决：迭代上限 60 → 300，容差 0.005 → 0.01。

经验：收敛慢不一定靠提高精度解决，可放宽容差并设上限，避免无限循环。

---

### Tail / 垂尾面积比超标

问题：高展弦比下垂尾面积比 Sv/S 偏大。

原因：Sv/S = Vv·b/Lv，高 AR 下翼展大导致面积比超标。

解决：垂尾面积比校验边界 0.20 → 0.35，Vv 采样上限 0.06 → 0.045。

经验：采样范围与几何校验边界必须联动校准，否则边界组合持续失败。

---

### Sampling / 可行性预筛

问题：边界组合（高速 + 小载荷 → 翼展 < 0.5m / 长径比 < 4）导致失败。

原因：预筛只调用 size_aircraft，未调用 validate。

解决：预筛回调同时执行 size_aircraft + validate，过滤不可行设计。

经验：采样后的可行性预筛必须覆盖完整几何校验链，而非仅尺寸估算。

---

### Multiprocessing / Windows spawn 局部函数

问题：`ParallelBatchRunner` 注入的 worker 函数若定义在测试函数内部，`spawn` 启动时报 `Can't get local object ...`。

原因：Windows `spawn` 通过 pickle 序列化 worker 函数到子进程，局部函数无法被 pickle。

解决：worker 函数必须定义在**模块级**（可导入）；`main.py` 保持 `__main__` 守卫。

经验：Windows 下多进程的 worker 函数必须模块级可导入；任何注入的回调（含测试 mock）都要满足此约束。

---

### Matplotlib / 常量列直方图

问题：`ax.hist(col, bins=30)` 对常量列抛 `ValueError: Too many bins for data range`。

原因：`static_margin` 经 `_trim_cg` 重心配平后**恒等于目标值 0.10**（常量列），30 个等宽 bin 在零范围内无法创建。

解决：直方图对常量/窄范围列改为标注数值而非绘图；否则动态 `bins = min(30, unique 数)`；相关性矩阵过滤常量/缺失列。

经验：配平/归一化会引入常量列；绘图与统计前必须防御常量列（std=0），不能假设所有指标都有分布。

---

### YAML / Path 对象

问题：`yaml.safe_load(Path对象)` 报 `AttributeError: 'WindowsPath' object has no attribute 'read'`。

原因：PyYAML 的 `safe_load` 接受文件流或字符串，不接受 `pathlib.Path`。

解决：统一 `with open(p, "r", encoding="utf-8") as f: yaml.safe_load(f)`。

经验：所有 YAML 加载函数统一用 `open()` 打开，不要直接把 Path 传给 `safe_load`。

---

### Design Space / 边缘不可行设计

问题：Sobol/LHS 采样在扩展后的 25 维设计空间边缘会产生几何不可行设计（平尾面积比超界、推进不足等）。

原因：`build_aircraft` 内部有几何可行性校验（`raise ValueError`），扩展设计空间后边界组合必然出现不可行点。

解决：分层处理——几何不可行（build 抛异常）由 worker 重试/计 failed；物理异常（NaN/推进不足）由 DataValidator 写库前过滤（rejected）。基准验证了 3 种结果共存（success/failed/rejected）属正常。

经验：大规模采样的设计空间必有不可行边缘；把「几何可行性」与「数据质量」分开处理，统计时区分 failed 与 rejected。

---

## Phase 2.5（阶段B）

### Performance / VLM CD0 截距重复计数（最关键）

问题：修复「定尺寸与完整分析阻力估算对齐」后，预筛通过的设计仍被 DataValidator 全部拒绝（剩余功率 < 0）。实测巡航阻力被高估约一倍。

原因：`_interp_polar` 返回的是 VLM 极线拟合的原始 CD（含拟合截距 CD0 ≈ 0.014），`evaluator` 又叠加了组件叠加法的 `cd0_parasitic`，导致零升阻力被重复计入。VLM 极线拟合截距是「数值伪零升阻力」，非物理阻力。

解决：`_interp_polar` 改为只返回 `k_induced·CL²`（VLM 仅贡献诱导阻力），总阻力 = 诱导 + 寄生（组件叠加法），不再叠加 VLM 的 CD0 截距。

经验：**VLM/面板法的极线拟合截距是数值伪零升阻力，不能与组件叠加法寄生阻力叠加**。任何「两种阻力来源合并」都要先核对是否重复计数零升阻力；发现系统性偏差（如阻力高估一倍）时优先怀疑重复计数而非系数调参。

---

### Propulsion / 静止推力系数系统性偏低

问题：修复阻力重复计数后仍有大量「推重比 < 0.08 / 剩余功率 < 0」，表现为推力系统性不足约一半。

原因：`ct_static = 0.10 * pitch_ratio`（随螺距比线性缩放），对典型 UAV 桨（螺距比 0.6）得 0.06，系统性偏低。

解决：`ct_static = 0.10`（标准 UAV 桨静止推力系数经验值，非随螺距比缩放）。

经验：经验系数若随无量纲量线性外推，会在典型工况系统性偏差；优先用该工况的独立经验值，而非从其它系数推导。发现「推力/功率系统性不足」时，在阻力与推进两侧都核查，不要只调一侧。

---

### Stability / 重心配平覆盖原始信号

问题：`_trim_cg` 重心配平后，`Cm_alpha` / 中性点 / 重心 被调整值覆盖，原始稳定性信号丢失，代理模型无法学习「配平前的真实静稳定裕度」。

原因：配平逻辑直接覆盖了 AeroResult 的原始字段。

解决：`_trim_cg` 在配平前把 Cmα_pre_trim / NP_pre / CG_pre / cg_shift / static_margin_target 存入独立字段；PerformanceResult 增加 `*_pre_trim` 字段；数据库新增对应列。区分「实际计算结果」与「人为设定目标裕度（0.10）」。

经验：**任何后处理（配平/归一化/缩放）都不要覆盖原始物理信号**；另存 `*_pre_*` / `*_raw` 字段。否则下游训练只能看到被人工目标「洗白」的值（如 static_margin 恒为 0.10），丢失真实分布。

---

### Prop_rpm / 设计变量未接入模型

问题：`prop_rpm` 是采样设计变量，但推进模型未真正消费它（转速不参与推力/功率求解），导致该变量在数据集中无真实影响。

原因：推进模型求解时转速作为自由变量或固定值，未以设计转速为约束。

解决：设计转速作为推进求解的**上限约束**；`_rpm_scan` 生成转速扫描序列；`thrust_available = min(转速受限推力, 功率受限推力)`；记录 design/solve/operating 三转速。

经验：**每个采样设计变量都要在物理模型中真正生效**，否则会产出「变量与输出无关」的伪特征，误导代理模型。核查变量是否贯通可用「转速敏感性测试」（改转速应改变可用推力）。

---

### Prefilter / 预筛与校验阈值脱节

问题：预筛放行的设计进入完整分析后被 DataValidator 大面积拒绝。

原因：预筛（预筛用快速模型）与校验（完整分析）的推力/功率估算不一致，或阈值不一致。

解决：预筛复用 `ParasiteDragModel` + 与 sizing/evaluator 一致的阻力/功率模型；预筛硬约束（推重比 0.08 等）与 `validation.yaml` 阈值联动对齐。

经验：**预筛的模型与阈值必须与下游校验保持一致**，否则预筛失去「提前过滤」的意义（放行又被拒）。对齐后 pipeline 内有效率从 50% 提升到 83%。

---

## Phase 2.5（阶段C1）

### Prefilter / 桨尖速度硬约束严重误杀（42.9%）

问题：误杀验证（C1.2）发现预筛「桨尖速度 > 220 m/s」硬约束误杀率高达 42.9%（7 组抽样中 3 组被完整分析确认可行）。

原因：设计转速 `prop_rpm` 是推进求解的**上限**，实际工作转速被节流下调（`prop_operating_rpm < prop_design_rpm`），预筛按 `π·n_design·D > 220` 估算桨尖速度，高估了实际工作转速下的桨尖速度。

解决：桨尖速度从硬约束（`tip_speed_max_ms`）降级为软约束告警（`tip_speed_warn_ms`），误杀率降至 0%。

经验：**预筛用「设计转速」估算物理量时，须注意该量在完整求解中可能被节流下调**；对「预筛估算与实际求解不一致」的量，先做交叉验证（误杀验证）再决定硬/软，不要直接当硬约束。

---

### Prefilter / 桨盘载荷硬约束误杀

问题：降级桨尖速度后，桨盘载荷（> 4000 W/m²）单独作为硬约束仍会误杀（抽样 2/2 全部被完整分析确认可行）。

原因：预筛按电机最大功率计盘载荷（实际巡航功率远低），且完整分析 DataValidator **不检查盘载荷**——即预筛用了一个下游不验证的约束。

解决：桨盘载荷从硬约束（`disk_loading_max_w_m2`）降级为软约束告警（`disk_loading_warn_w_m2`），最终误杀率 0%。

经验：**经验阈值不是绝对物理定律**。若一个约束「下游不验证」或「快速估算与真实求解量纲/工况不一致」，应降级为软约束或告警，而非硬过滤；否则会静默丢弃可行设计。C1 最终硬约束仅保留「推力不足 + 推重比不足」两项物理硬性约束。

---

### Database / model_limitations 读回为 0

问题：质量分析报告显示「模型限制已记录样本数 = 0」，但元数据确实注入了 `MODEL_LIMITATIONS`。

原因：`MODEL_LIMITATIONS` 的键（`wing_dihedral_deg` / `prop_layout` / `CL_max` / `propulsion_model`）是注入到 `metadata.extra` 的**顶层键**，读回时却用 `extra.get("model_limitations")` 取嵌套键，取不到。

解决：读回时改为 `{k: extra.get(k) for k in MODEL_LIMITATIONS if k in extra}`（从顶层键提取）。修复后 55/55 样本正确读回。

经验：**「写入时的键结构」与「读回时的取键路径」必须一致**；写是顶层键、读是嵌套键是典型的元数据丢失。修复后要用真实数据读回验证（而非仅单测），并检查质量报告中的计数从 0 → 真实值。

---

### C1 门控 / 误杀验证方法学

问题：如何在不丢弃预筛拒绝样本的前提下，验证预筛是否误杀可行设计。

原因：预筛是快速模型，可能把「快速估算不可行但完整分析可行」的设计误判为拒绝。

解决：从预筛拒绝样本中**抽样 ≥10 组跑完整分析**（独立数据目录 + 关闭入库，避免污染正式数据集），统计误杀数与误杀率；对误杀样本逐条分析原因，修正预筛阈值或降级约束。

经验：**预筛/缓存/近似模型的正确性必须用「下游真实计算」交叉验证**，抽样复核（而非全量）即可低成本暴露系统性误杀；验证过程不得污染正式数据集。

---

## Phase 2.5（阶段C2）

### Subprocess / 孙进程继承管道句柄致超时兜底失效（生产事故）

问题：C2 万级生成跑到 10151/10164 时被 VSPAero 卡死 8+ 小时——vspaero.exe（vspscript 启动的孙进程）挂起，整个批次静默死锁。

原因：三个 adapter 虽都设了 `subprocess.run(timeout=)`，但 `capture_output=True` 用管道捕获 stdout/stderr；孙进程 vspaero.exe 继承管道写端句柄，timeout 触发后只 kill 直接子进程 vspscript.exe，清理阶段的 `communicate()` 无限阻塞等孙进程关闭句柄 → 超时兜底形同虚设，且 vspaero 成孤儿残留。

解决：新增 `src/core/subprocess_util.py::run_with_timeout`——stdout/stderr 重定向到临时文件（非管道），超时后先 `taskkill /T /F`(Windows)/`killpg`(Unix) 杀整个进程树（含孙进程）再读回输出。

经验：**Windows 下调用会派生孙进程的外部工具（vspscript→vspaero），禁用 `capture_output=True` + `timeout=`**；必须「文件重定向 + 进程树 kill」，否则超时兜底失效并残留孤儿进程。定位卡死先看进程 CPU 时间（0.0156s = 启动后即挂起）与启动时间（8h 前）判断是否孤儿。

### OpenBLAS 多线程偶发段错误（exit 139）

问题：C2 断点续算在预筛阶段（纯 numpy，非外部工具）偶发 SIGSEGV（exit 139），日志停在 sizing 计算、无 Python traceback；内存充足排除 OOM，同逻辑诊断脚本全量 17242 组完整跑通，判定为偶发 C 扩展崩溃。

原因：numpy 2.5.5 用 scipy-openblas（OpenBLAS 0.3.34，DYNAMIC_ARCH + **NO_AFFINITY**），该配置在 Windows 上有线程池竞争致偶发段错误的已知问题；多进程 spawn 时还会 16 worker × N 线程过订阅。

解决：入口脚本在导入 numpy 前设 `OPENBLAS_NUM_THREADS=1` + `OMP_NUM_THREADS=1`，并 `faulthandler.enable()` 捕获崩溃堆栈。

经验：**Windows 上 numpy 偶发原生崩溃（无 traceback）优先怀疑 OpenBLAS 线程**，在导入 numpy 前强制单线程 BLAS 通常即解；对 BEMT/几何定尺寸这类标量+小数组计算无性能损失。长跑入口脚本务必 `faulthandler.enable()`，否则原生崩溃位置不可定位。

### SQLite 逐条 commit 致写锁阻塞读路径 + 热日志（生产事故）

问题：C2 收尾阶段「读回数据」卡死 7+ 分钟，读回 7387 样本本应 1.7s，却抛 `database is locked`。

原因：`_record_rejections` 对 7078 条预筛拒绝记录逐条 `commit()`，在 rollback-journal 模式下每条 3 次 fsync，Windows 下耗时 10 分钟级且长期持写锁；叠加前次强杀残留的热日志未及时恢复，读路径被写锁阻塞。

解决：连接切 `PRAGMA journal_mode=WAL` + `busy_timeout=30000` + `synchronous=NORMAL`；`record_prefilter_rejection` 加 `commit=False`，`_record_rejections` 改批量单事务。

经验：**SQLite 批量写入必须单事务提交**（逐条 commit 的 fsync 开销在 rollback 模式下是灾难性的）；生产库用 **WAL 模式**（读写不互斥、提交 1 次 fsync、无热日志，强杀后无需回滚）。诊断「database is locked」先查 `.sqlite-journal` 文件是否存在（存在=有写事务未提交）。

---

## Phase 2.5（阶段C3）

### Propulsion / 工作点求解 overshoot（最关键）

问题：C2 续航达标率仅 0.3%，均值仅为要求的 0.628 倍。C3 Stage A 定量诊断发现主导根因**不是**推进效率差异或阻力低估（此前 06 审计报告的误判），而是 `_solve_propeller` 用 250rpm 步进线性扫描求工作转速，而 BEMT 推力在脱离风车区（前进比过高）时陡变——样本 15220（D=1.5m 单桨）在 rpm≤1000 推力=0、1250rpm=18N、1500rpm=232N（突增 12.7 倍），250rpm 步进跨过陡变区 → 推力/功率 overshoot 9.4 倍、电机超载 2.75 倍、续航崩到 0.22h。

原因：线性扫描 + 无功率约束，叠加 BEMT 风车区「净推力≤0 返回 0」的硬截断造成推力-转速曲线的陡变。

解决：`_solve_propeller` 改二分求解 + 加轴功率约束（`p_max_shaft`）。修复后 overshoot 从 9.4× → 1.00×，功率虚高从 11.5kW 降到 2.9kW。修复只改求解器离散化，不改 BEMT 物理公式/经验系数/任务约束。

经验：**求解器离散化误差可以比物理模型误差大一个量级**。诊断「性能标签系统性偏差」时，先查数值求解方法（线性扫描/步进/迭代收敛），再怀疑物理模型/经验系数。用「工作点曲线」画推力-转速，能一眼看出风车区陡变导致的 overshoot。

### Propulsion / 二分前提要写进注释

经验：BEMT 推力「风车区=0，脱离后随转速单调非减」，是二分求工作转速的前提。改二分前先验证单调性（用推力-转速曲线），不要假设所有工况单调。

### Feature / 标签语义冻结（进入训练前）

问题：`tip_speed_ms` 用设计转速算（703 m/s）却当作物理指标，实际工作转速仅 137 m/s；`LD_max` 仅含诱导阻力、`LD_cruise` 含寄生阻力，口径不一致；`--n-valid` 实为「预筛通过数」却名为「有效样本数」。三类语义混乱会在训练时误导特征工程。

解决：`docs/FEATURE_SCHEMA.md` 冻结全部特征（schema_version 1.0.0），每个字段明确名称/单位/类型/输入/输出/可训练/参与优化；区分 `analysis_valid`（计算有效）/`mission_feasible`（任务达标）/`analysis_failed`（失败）。

经验：**进代理模型训练前必须冻结 Feature Schema**。任务不可行 ≠ 计算失败——不可行但计算有效的样本仍可作回归训练样本。任何字段改动都要升级 schema_version，强制打破「偷偷改字段」的惯性。

### Data / 缺失记录的确定性重放恢复

问题：C2 生产事故致预筛拒绝记录缺 3466 条（7078 声称 vs 3612 入库）。

原因：生产崩溃 + 收尾脚本未补录。

解决：预筛是纯 Python 确定性计算，用 `scripts/backfill_prefilter_rejections.py` 重放（同 seed LHS 采样 + 预筛），INSERT OR REPLACE 幂等补录 3612→7078。

经验：**可确定性重放的数据缺口，用重放恢复而非伪造**；补录脚本须与原始生成脚本共享同一采样 seed/逻辑，并保证幂等。补录前先备份数据库。

---
