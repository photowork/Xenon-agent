"""
migrate.py — P2: 将 Memory/memory_Write 中的记忆迁移到分层因果记忆网络

用法:
    cd D:\Xenon\agent_Xenon
    python xenon_core/memory_core/migrate.py

策略:
    1. 扫描 Memory/memory_Write/*.txt
    2. 从文件名解析日期、时间、标题
    3. 读取内容
    4. 调用 MemoryAPI.write() 写入 .memory/ 分层网络
    5. 输出迁移报告

层级策略:
    - 自动推测 (api._infer_level) — 根据关键词判断
    - 默认 fallback 到 level=4 (行星层，事件簇)
    - 特别重要的记忆可手动提升层级
"""

import sys
import os
from pathlib import Path
import re
import time
import traceback
from datetime import datetime
from typing import Optional, Tuple

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from xenon_core.memory_core.api import MemoryAPI, _infer_level

# ── 配置 ──────────────────────────────────────────────
SOURCE_DIR = PROJECT_ROOT / "Memory" / "memory_Write"
DRY_RUN = False          # True = 只分析不写入
BATCH_SIZE = 100         # 每批处理数量
START_INDEX = 0          # 从第几条开始（用于断点续传）
MAX_FILES = None         # None = 全部，或指定最大数量
SKIP_EXISTING = True     # 跳过已存在的（按 title hash 检查）

# ── 文件名正则 ─────────────────────────────────────────
# 格式1: YYYY-MM-DD_HH-MM-SS[_microseconds]_标题.txt
# 格式2: YYYY-MM-DD_标题.md
# 格式3: YYYY-MM-DD_HH-MM_标题.txt (旧格式, 只有时-分)
RE_FILENAME_1 = re.compile(
    r'^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(?:_(\d+))?_(.+)\.txt$'
)
RE_FILENAME_2 = re.compile(
    r'^(\d{4}-\d{2}-\d{2})_(.+)\.(?:txt|md)$'
)


def parse_filename(file_path: Path) -> Optional[dict]:
    """解析文件名，提取元数据"""
    name = file_path.stem  # 无扩展名
    m = RE_FILENAME_1.match(file_path.name)
    if m:
        date_str = m.group(1)
        time_str = m.group(2).replace('-', ':')
        usec = m.group(3)
        title = m.group(4)
        return {
            "date": date_str,
            "time": time_str,
            "timestamp": f"{date_str}T{time_str}",
            "title": title,
        }

    m = RE_FILENAME_2.match(file_path.name)
    if m:
        date_str = m.group(1)
        title = m.group(2)
        return {
            "date": date_str,
            "time": "00:00:00",
            "timestamp": f"{date_str}T00:00:00",
            "title": title,
        }

    return None


def read_content(file_path: Path) -> str:
    """安全读取文件内容"""
    try:
        return file_path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return file_path.read_text(encoding='gbk', errors='replace')


def truncate_title(title: str, max_len: int = 80) -> str:
    """截断过长的标题"""
    if len(title) <= max_len:
        return title
    return title[:max_len-3] + "..."


def extract_summary(content: str, max_len: int = 200) -> str:
    """从内容中提取摘要"""
    # 尝试取第一段有意义的内容
    lines = [l.strip() for l in content.split('\n') if l.strip()]
    if not lines:
        return content[:max_len]
    # 跳过标题行（以 # 开头）
    for line in lines:
        if not line.startswith('#') and len(line) > 10:
            return line[:max_len] + ("..." if len(line) > max_len else "")
    return lines[0][:max_len] + ("..." if len(lines[0]) > max_len else "")


def migrate(
    start_index: int = 0,
    max_files: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """执行迁移"""
    api = MemoryAPI() if not dry_run else None

    files = sorted(SOURCE_DIR.glob("*.txt"))
    total = len(files)

    # 切片
    files = files[start_index:]
    if max_files:
        files = files[:max_files]

    stats = {
        "total_found": total,
        "to_process": len(files),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
        "level_distribution": {},
        "start_time": time.time(),
    }

    print(f"📁 源目录: {SOURCE_DIR}")
    print(f"📊 共发现 {total} 个文件，本次处理 {len(files)} 个（从索引 {start_index} 开始）")
    if dry_run:
        print("🔍 DRY RUN 模式 — 只分析不写入")
    print(f"{'='*60}")

    for i, file_path in enumerate(files):
        idx = start_index + i + 1
        filename = file_path.name

        try:
            # 1. 解析文件名
            meta = parse_filename(file_path)
            if not meta:
                print(f"  [{idx}/{total}] ⚠️ 跳过（无法解析文件名）: {filename}")
                stats["skipped"] += 1
                continue

            title_raw = meta["title"]
            title = truncate_title(title_raw)

            # 2. 读取内容
            content = read_content(file_path)
            if not content.strip():
                print(f"  [{idx}/{total}] ⚠️ 跳过（空文件）: {filename}")
                stats["skipped"] += 1
                continue

            # 3. 提取摘要
            summary = extract_summary(content)

            # 4. 推测层级
            level = _infer_level(title, summary, content)
            stats["level_distribution"][level] = stats["level_distribution"].get(level, 0) + 1

            if dry_run:
                print(f"  [{idx}/{total}] 📝 {title[:50]} → 层级 {level} ({meta['date']})")
                stats["success"] += 1
                continue

            # 5. 写入分层网络
            result = api.write(
                content=content,
                summary=summary,
                title=title,
                tags=[meta["date"]],
                level=level,
                source_type="migrated",
            )

            if result.get("success"):
                stats["success"] += 1
                node_id = result.get("node_id", "?")
                if i < 10 or stats["success"] % 100 == 0:
                    print(f"  [{idx}/{total}] ✅ {title[:50]} → {node_id} (L{level})")
            else:
                error_msg = result.get("error", "unknown")
                print(f"  [{idx}/{total}] ❌ {filename[:60]} — {error_msg}")
                stats["failed"] += 1
                stats["errors"].append({"file": filename, "error": error_msg})

        except Exception as e:
            print(f"  [{idx}/{total}] 💥 {filename[:60]} — {str(e)}")
            stats["failed"] += 1
            stats["errors"].append({"file": filename, "error": str(e)})
            if not dry_run:
                traceback.print_exc()

        # 批次报告
        if (i + 1) % BATCH_SIZE == 0:
            elapsed = time.time() - stats["start_time"]
            print(f"  --- 批次报告 [{idx}/{total}] 成功:{stats['success']} "
                  f"失败:{stats['failed']} 跳过:{stats['skipped']} "
                  f"耗时:{elapsed:.1f}s ---")

    # 最终报告
    elapsed = time.time() - stats["start_time"]
    print(f"\n{'='*60}")
    print(f"🏁 迁移完成")
    print(f"   总文件数: {stats['total_found']}")
    print(f"   本次处理: {stats['to_process']}")
    print(f"   ✅ 成功:   {stats['success']}")
    print(f"   ❌ 失败:   {stats['failed']}")
    print(f"   ⚠️ 跳过:   {stats['skipped']}")
    print(f"   ⏱️ 耗时:   {elapsed:.1f}s")
    print(f"   📊 层级分布: {dict(sorted(stats['level_distribution'].items()))}")

    if stats["errors"]:
        print(f"\n   错误详情 (前 20 条):")
        for err in stats["errors"][:20]:
            print(f"     - {err['file']}: {err['error'][:100]}")

    return stats


# ── CLI ────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="迁移扁平记忆到分层因果网络")
    parser.add_argument("--dry-run", action="store_true", help="只分析不写入")
    parser.add_argument("--start", type=int, default=START_INDEX, help="起始索引")
    parser.add_argument("--max", type=int, default=MAX_FILES, help="最大处理数")
    parser.add_argument("--no-skip", action="store_true", help="不跳过已存在的")
    args = parser.parse_args()

    migrate(
        start_index=args.start or START_INDEX,
        max_files=args.max or MAX_FILES,
        dry_run=args.dry_run or DRY_RUN,
    )
