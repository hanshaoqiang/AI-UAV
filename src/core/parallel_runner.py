"""Phase 2: ParallelBatchRunner — 多进程批量运行。

架构：worker 计算 + 主进程单写者。
  - worker 进程：独立 DesignPipeline（enable_db=False），只计算不写库；
    内部失败重试。
  - 主进程：消费结果队列，独占 SQLite 单写者入库 + 写元数据 + 断点状态。

特性：
  - 动态任务分配（task_queue，worker 空闲即拉取下一任务）
  - 失败重试（每任务 max_retries 次）
  - 断点恢复（state_file 记录 completed design_id）
  - CPU 资源控制（max_workers 可配，默认保守）
  - Windows spawn 安全（worker 函数模块级可导入、`__main__` 守卫）

接口稳定：保留原有顺序 BatchRunner（batch_runner.py），本模块为新增类。
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import queue
import time
import traceback
from pathlib import Path
from typing import Optional

logger = logging.getLogger("uavdp.batch")


# ============================================================
# 失败分类（Phase 2.5 C1：区分失败阶段，供质量报告与误杀分析）
# ============================================================
def classify_failure_str(error: str | None) -> str:
    """把 worker 失败信息分类为数据质量可追踪的阶段类别。

    类别：
      geometry_infeasible  纯 Python 几何/重量不可行（外部工具之前）
      aero_failed          VSPAero 气动分析失败（极线生成/解析）
      external_tool_failed OpenVSP/XFOIL 等外部工具运行失败
      other_failed         其余无法归类失败
    """
    msg = error or ""
    # 几何/重量不可行（build_aircraft / size_aircraft 抛 ValueError）
    if ("几何" in msg) or ("重量迭代" in msg) or ("未收敛" in msg):
        return "geometry_infeasible"
    # 气动分析失败（VSPAero 极线文件/解析）
    if ("VSPAero" in msg) or ("极线" in msg) or ("无法解析" in msg):
        return "aero_failed"
    # 外部工具运行失败（OpenVSP vspscript / XFOIL）
    if ("vsp3" in msg) or ("vspscript" in msg) or ("xfoil" in msg) or ("XFOIL" in msg):
        return "external_tool_failed"
    return "other_failed"


# ============================================================
# worker 进程（模块级，spawn 可导入）
# ============================================================
def _run_pipeline_task(config: dict, mission, design_cfg: dict | None):
    """在 worker 进程内构造独立 pipeline 并执行单任务（不写库）。"""
    from src.core.workflow import DesignPipeline
    pipeline = DesignPipeline(config, enable_db=False)
    return pipeline.run(mission, design_cfg=design_cfg)


def _parallel_worker(task_queue, result_queue, config, max_retries: int,
                     log_level: int, task_fn=None):
    """worker 进程入口。拉取任务 → 重试执行 → 结果入队。"""
    if task_fn is None:
        task_fn = _run_pipeline_task
    logging.getLogger().setLevel(log_level)
    try:
        while True:
            item = task_queue.get()
            if item is None:      # 毒丸 → 退出
                break
            design_id, mission, design_cfg = item
            ok = False
            result = None
            error = None
            attempts = 0
            for attempt in range(1, max_retries + 2):
                attempts = attempt
                try:
                    result = task_fn(config, mission, design_cfg)
                    ok = True
                    break
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    if attempt > max_retries:
                        logger.error("任务 %s 第 %d 次失败后放弃: %s",
                                     design_id, attempt, e)
            result_queue.put((design_id, ok, result, error, attempts))
    except Exception as e:  # worker 兜底，防进程意外崩溃
        logger.error("worker 异常: %s", traceback.format_exc())
        try:
            result_queue.put((None, False, None, f"worker崩溃: {e}", 0))
        except Exception:
            pass


# ============================================================
# 资源控制
# ============================================================
def _default_max_workers(config: dict) -> int:
    """CPU 资源控制：优先配置 parallel.max_workers；未配则保守取 min(8, cpu)。"""
    n_cpu = os.cpu_count() or 4
    cfg = config.get("parallel") or {}
    n = cfg.get("max_workers")
    if n is None or int(n) <= 0:
        n = min(8, n_cpu)
    return max(1, min(int(n), n_cpu))


# ============================================================
# 主进程 runner
# ============================================================
class ParallelBatchRunner:
    """多进程批量运行器（单写者入库）。

    用法：
      runner = ParallelBatchRunner(config, max_workers=4)
      stats = runner.run(tasks, dataset_id="ds_001", random_seed=42,
                         sample_method="lhs")
    """

    def __init__(self, config: dict, max_workers: int | None = None,
                 max_retries: int = 2, state_file: str | Path | None = None,
                 log_level: int = logging.WARNING, worker_fn=None,
                 stall_timeout_s: float | None = None):
        self.config = config
        self.max_retries = max_retries
        self.max_workers = max_workers if max_workers else _default_max_workers(config)
        self.state_file = Path(state_file) if state_file else None
        self.log_level = log_level
        self.worker_fn = worker_fn          # 可注入（测试用），默认 _run_pipeline_task
        # 无进展看门狗：主进程超过该秒数未收到任何结果且 worker 仍存活时，
        # 判定批次死锁并强制终止（未完成任务标记 failed，供断点续算）。
        # None = 禁用（向后兼容）。生产路径由 DatasetGenerator 默认启用 1800s。
        self.stall_timeout_s = stall_timeout_s
        self.stats: dict = {}
        self.results: list[dict] = []

    # ---------------- 主入口 ----------------
    def run(self, tasks: list[dict], resume: bool = True,
            batch_id: str | None = None, dataset_id: str | None = None,
            random_seed: int | None = None, sample_method: str = "",
            collect_metadata: bool = True, validator=None,
            keep_invalid: bool = False) -> dict:
        """执行任务列表。tasks 每项含 design_id / mission / design_cfg。

        validator:   可选 DataValidator（或含 validate_result(result, mission) 的对象）。
                     提供时对每个成功结果做后置校验，error 级异常样本默认不入库。
        keep_invalid: True → 校验未通过的样本仍写库（标记在 metadata.extra.validation）。
                     默认 False → 异常样本过滤（记入 stats.rejected）。
        """
        t_start = time.time()
        batch_id = batch_id or f"pbatch_{time.strftime('%Y%m%d_%H%M%S')}"

        # 0) 任务映射 + 断点恢复
        task_map: dict[str, dict] = {}
        for i, t in enumerate(tasks):
            did = t.get("design_id", f"design_{i:04d}")
            task_map[did] = t
        done = self._load_completed() if resume else {}
        pending = [did for did in task_map if did not in done]

        self.stats = {"total": len(tasks), "success": 0, "failed": 0,
                      "rejected": 0, "skipped": len(done), "pending": len(pending),
                      # Phase 2.5 C1：失败阶段分类（供质量报告）
                      "failed_geometry_infeasible": 0,
                      "failed_aero_failed": 0,
                      "failed_external_tool_failed": 0,
                      "failed_other_failed": 0}
        self.results = []

        if not pending:
            self.stats["elapsed_s"] = 0.0
            self.stats["batch_id"] = batch_id
            logger.info("无待处理任务（全部已完成/跳过）")
            return dict(self.stats)

        # 1) 主进程单写者数据库
        from src.database.sqlite_manager import SQLiteManager
        db = SQLiteManager(self.config["paths"]["db_path"])

        # 2) 队列 + 入队
        ctx = mp.get_context("spawn")
        task_queue = ctx.Queue()
        result_queue = ctx.Queue()
        for did in pending:
            t = task_map[did]
            task_queue.put((did, t["mission"], t.get("design_cfg")))

        # 3) 启动 worker
        n_workers = min(self.max_workers, len(pending))
        workers = []
        for _ in range(n_workers):
            p = ctx.Process(
                target=_parallel_worker,
                args=(task_queue, result_queue, self.config, self.max_retries,
                      self.log_level, self.worker_fn))
            p.daemon = True
            p.start()
            workers.append(p)
        logger.info("并行批处理: %d 任务, %d worker, 跳过 %d",
                    len(pending), n_workers, len(done))

        # 4) 主进程消费结果（单写者入库）
        completed = dict(done)
        finished: set = set()
        last_progress = time.time()
        try:
            while len(finished) < len(pending):
                try:
                    did, ok, result, error, attempts = result_queue.get(timeout=2.0)
                except queue.Empty:
                    if all(not p.is_alive() for p in workers):
                        logger.error("所有 worker 已退出但任务未完成，中止等待")
                        break
                    # 无进展看门狗：超过阈值未收到任何结果且 worker 仍存活，
                    # 判定批次死锁并强制终止（未完成任务后续标记 failed，供断点续算）。
                    if (self.stall_timeout_s is not None and
                            time.time() - last_progress > self.stall_timeout_s):
                        logger.error(
                            "批次停滞 %.0fs 无进展（阈值 %.0fs），判定死锁，强制终止",
                            time.time() - last_progress, self.stall_timeout_s)
                        break
                    continue
                last_progress = time.time()
                finished.add(did)
                mission = task_map[did]["mission"]
                if ok:
                    # 写库前校验（可选）：error 级异常样本默认过滤
                    validation = None
                    valid = True
                    if validator is not None:
                        try:
                            validation = validator.validate_result(
                                result, mission=mission)
                            valid = validation.valid
                        except Exception as e:  # 校验异常不应阻断（视为有效）
                            logger.warning("校验失败(视为有效): %s", e)
                            valid = True
                    if valid or keep_invalid:
                        self._persist_success(db, did, mission, result, batch_id,
                                              dataset_id, random_seed, sample_method,
                                              collect_metadata, validation=validation)
                        self.stats["success"] += 1
                        completed[did] = result.get("run_id", did)
                        logger.info("[%d/%d] 成功: %s (%.0fs, %d 次尝试, valid=%s)",
                                    len(finished), len(pending), did,
                                    result.get("elapsed_s", 0.0), attempts, valid)
                    else:
                        self.stats["rejected"] += 1
                        completed[did] = result.get("run_id", did)
                        self.results.append({
                            "design_id": did, "run_id": result.get("run_id"),
                            "status": "rejected",
                            "validation": validation.to_dict() if validation else None})
                        logger.info("[%d/%d] 校验拒绝(不入库): %s",
                                    len(finished), len(pending), did)
                else:
                    self.stats["failed"] += 1
                    category = classify_failure_str(error)
                    self.stats[f"failed_{category}"] = \
                        self.stats.get(f"failed_{category}", 0) + 1
                    self.results.append({"design_id": did, "status": category,
                                         "error": error})
                    logger.error("[%d/%d] 失败(%s): %s (%s)",
                                 len(finished), len(pending), category, did, error)
                self._save_state(batch_id, completed)
        finally:
            # 5) 结束 worker（毒丸 + join + 兜底 terminate）
            for _ in workers:
                try:
                    task_queue.put(None)
                except Exception:
                    pass
            for p in workers:
                p.join(timeout=10)
            for p in workers:
                if p.is_alive():
                    p.terminate()

        # 6) 处理 worker 异常退出导致的丢失任务
        for did in pending:
            if did not in finished:
                self.stats["failed"] += 1
                self.stats["failed_other_failed"] = \
                    self.stats.get("failed_other_failed", 0) + 1
                self.results.append({"design_id": did, "status": "other_failed",
                                     "error": "worker 异常退出，任务未完成"})
                logger.error("任务丢失(worker 异常): %s", did)

        db.close()
        self.stats["elapsed_s"] = time.time() - t_start
        self.stats["batch_id"] = batch_id
        logger.info("并行批处理完成: %s", self.stats)
        return dict(self.stats)

    # ---------------- 单写者入库 ----------------
    def _persist_success(self, db, design_id: str, mission, result: dict,
                         batch_id: str, dataset_id: str | None, random_seed,
                         sample_method: str, collect_metadata: bool,
                         validation=None) -> None:
        """主进程独占写库：六表 + run_history + 可选 dataset_metadata。"""
        run_id = result["run_id"]
        ac = result["aircraft"]
        db.start_run(run_id, batch_id)
        db.save_run(run_id, mission.to_dict(), ac.to_dict(),
                    ac.propulsion.to_dict(), result["aero"].to_dict(),
                    result["performance"].to_dict())
        db.finish_run(run_id, result.get("elapsed_s", 0.0), "success")
        if collect_metadata:
            from src.dataset.metadata import build_metadata
            extra = {"design_id": design_id}
            if validation is not None:
                extra["validation"] = validation.to_dict()
            meta = build_metadata(
                run_id, dataset_id=dataset_id, random_seed=random_seed,
                sample_method=sample_method, tools=self.config.get("tools"),
                extra=extra)
            db.save_metadata(meta)
        self.results.append({"design_id": design_id, "run_id": run_id,
                             "status": "success",
                             "elapsed_s": result.get("elapsed_s", 0.0),
                             "valid": validation.valid if validation else True})

    # ---------------- 断点恢复 ----------------
    def _load_completed(self) -> dict:
        """读取 state_file 中已完成的 design_id → run_id 映射。"""
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                return dict(data.get("completed", {}))
            except Exception as e:
                logger.warning("断点状态读取失败(%s)，从头开始", e)
        return {}

    def _save_state(self, batch_id: str, completed: dict) -> None:
        """保存批次状态（供中断恢复）。"""
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {"batch_id": batch_id, "stats": dict(self.stats),
                 "completed": dict(completed)}
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
