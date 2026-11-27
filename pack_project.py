#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI-UAV 项目一键打包脚本（用于上传到会话 / 推送到 GitHub）。

在 E:\\workbuddy\\AI-UAV 目录下运行：

    python pack_project.py                 # 只打包源码（默认，推荐先传这个）
    python pack_project.py --with-uiuc     # 连同 UIUC 翼型库一起打包
    python pack_project.py --with-data     # 连同数据库/大产物一起打包
    python pack_project.py --root "E:\\workbuddy\\AI-UAV"   # 指定项目根目录

输出：AI-UAV_src_<时间戳>.zip（生成在项目根目录）。
打包完成后把生成的 zip 拖进对话窗口即可。
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 参与代码协作的“必要文件”扩展名（源码 + 配置 + 文档 + 模板）
# ---------------------------------------------------------------------------
INCLUDE_EXTS = {
    ".py", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini",
    ".md", ".rst", ".txt", ".jinja2", ".j2", ".html", ".css", ".js",
    ".csv", ".bat", ".ps1", ".sh", ".ipynb", ".m", ".c", ".cpp", ".h",
}

# 始终排除的目录（虚拟环境 / 缓存 / IDE / 构建产物）
EXCLUDE_DIR_NAMES = {
    ".git", ".svn", ".hg",
    "venv", ".venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", ".ipynb_checkpoints",
    "build", "dist", "node_modules",
}

# 始终排除的大文件 / 生成物扩展名（模型、数据库、图片、日志、压缩包等）
EXCLUDE_EXTS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".o",
    ".vsp3", ".stl", ".step", ".stp", ".iges", ".igs",
    ".db", ".sqlite", ".sqlite3", ".dat",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
    ".log", ".npz", ".npy", ".h5", ".hdf5", ".pkl", ".joblib",
    ".onnx", ".pt", ".pth", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".tif", ".tiff",
    ".pdf", ".pptx", ".docx", ".xlsx",
}

# 默认排除的数据类目录（大体积，后续单独传项目网盘）
DATA_DIR_NAMES = {
    "uiuc", "data", "dataset", "datasets", "airfoil_db", "airfoils",
    "vsp3", "mesh", "meshes", "output", "outputs", "results", "result",
    "logs", "log", "cache", "tmp", "temp",
}


def is_excluded_dir(name: str) -> bool:
    if name in EXCLUDE_DIR_NAMES:
        return True
    if name.endswith(".egg-info"):
        return True
    return False


def is_data_dir(name: str, with_data: bool) -> bool:
    if with_data:
        return False
    return name in DATA_DIR_NAMES


def collect_files(root: Path, with_uiuc: bool, with_data: bool):
    """返回 (要打包的文件列表, 被排除的文件计数)。"""
    included: list[tuple[Path, str]] = []  # (绝对路径, zip 内相对路径)
    excluded_count = 0

    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue

        rel = path.relative_to(root)
        parts = rel.parts

        # 目录级过滤
        skip = False
        for part in parts[:-1]:
            if is_excluded_dir(part):
                skip = True
                break
            # uiuc 目录单独开关
            if part == "uiuc" and not with_uiuc:
                skip = True
                break
            if is_data_dir(part, with_data):
                skip = True
                break
        if skip:
            excluded_count += 1
            continue

        # 扩展名级过滤
        ext = path.suffix.lower()
        if ext in EXCLUDE_EXTS:
            excluded_count += 1
            continue
        if ext and ext not in INCLUDE_EXTS:
            excluded_count += 1
            continue

        included.append((path, rel.as_posix()))

    return included, excluded_count


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-UAV 项目一键打包脚本")
    parser.add_argument("--root", type=str, default=".",
                        help="项目根目录，默认当前目录")
    parser.add_argument("--with-uiuc", action="store_true",
                        help="连同 UIUC 翼型库一起打包")
    parser.add_argument("--with-data", action="store_true",
                        help="连同数据库/大产物一起打包")
    parser.add_argument("--out", type=str, default=None,
                        help="输出 zip 路径，默认在项目根目录自动命名")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[错误] 项目目录不存在: {root}", file=sys.stderr)
        return 1

    included, excluded_count = collect_files(root, args.with_uiuc, args.with_data)

    if not included:
        print("[错误] 没有找到任何要打包的文件，请确认 --root 指向正确目录。",
              file=sys.stderr)
        return 1

    tag = "src"
    if args.with_uiuc:
        tag = "src+uiuc"
    if args.with_data:
        tag = "full"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else root / f"AI-UAV_{tag}_{ts}.zip"
    out_path = out_path.resolve()

    total_size = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, rel_path in included:
            zf.write(abs_path, arcname=rel_path)
            total_size += abs_path.stat().st_size

    print("=" * 60)
    print("打包完成")
    print("=" * 60)
    print(f"  输出文件 : {out_path}")
    print(f"  包含文件 : {len(included)} 个")
    print(f"  总大小   : {total_size / 1024 / 1024:.2f} MB")
    print(f"  已排除   : {excluded_count} 个（缓存/虚拟环境/大文件/数据产物）")
    print()
    print("下一步：把上面这个 zip 文件拖进对话窗口上传即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
