"""启动自检脚本 — 验证记忆系统完整性"""
import sys
sys.path.insert(0, r'D:\Xenon\agent_Xenon')

from xenon_core.memory_core import MemoryAPI
from xenon_core.memory_core.schema import STORAGE_ROOT, LEVEL_DIRS
from xenon_core.recursion_detector import RecursionDetector

print("=" * 50)
print("Xenon 启动自检 — 记忆系统 & 递归检测器")
print("=" * 50)

# 1. 递归检测器
print("\n[1/4] 递归检测器 v3...")
rd = RecursionDetector()
print(f"  RecursionDetector: OK")
print(f"  阈值: {rd._threshold}")
print(f"  已触发: {rd.triggered}")

# 2. 模块导入
print("\n[2/4] MemoryAPI 导入...")
api = MemoryAPI()
print(f"  MemoryAPI: OK")

# 3. 金字塔验证
print("\n[3/4] 层级金字塔...")
total = 0
shape_parts = []
for level, dname in sorted(LEVEL_DIRS.items()):
    d = STORAGE_ROOT / dname
    cnt = len(list(d.glob('*.json'))) if d.exists() else 0
    total += cnt
    shape_parts.append(str(cnt))
    print(f"  L{level} {dname}: {cnt}")
print(f"  总计: {total}")
print(f"  形状: {'→'.join(shape_parts)}")

# 4. 搜索测试
print("\n[4/4] 搜索功能...")
tests = ["自主性", "递归", "记忆", "因果"]
for q in tests:
    results = api.search(q)
    levels = set(r.get("level", 0) for r in results)
    print(f"  搜索 '{q}': {len(results)} 条 (层级: {sorted(levels)})")

print("\n" + "=" * 50)
if total > 0:
    print("✅ 启动正常 — 记忆系统就绪")
else:
    print("❌ 记忆为空 — 需要迁移")
print("=" * 50)
