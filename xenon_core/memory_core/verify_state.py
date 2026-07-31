"""验证 .memory/ 最终状态"""
import json
from pathlib import Path

# .memory 在项目根目录
memory = Path('D:/Xenon/agent_Xenon/.memory')

# 列出所有 L2 节点
print('=== L2 星系节点 ===')
galaxy_files = sorted(memory.glob('galaxy/*.json'))
if not galaxy_files:
    print('  (空)')
for f in galaxy_files:
    with open(f) as fh:
        n = json.load(fh)
    kids = len(n.get('children_ids', []))
    pid = n.get('parent_id', 'NONE')
    print(f'  {n["node_id"]:40s} | {n["title"][:40]:40s} | children={kids:3d} | parent={pid}')

print()

# 检查宇宙节点
print('=== L1 宇宙节点 ===')
for f in sorted(memory.glob('meta/*.json')):
    with open(f) as fh:
        n = json.load(fh)
    kids = n.get('children_ids', [])
    print(f'  {n["node_id"]} | children={len(kids)}')
    for kid in kids[:10]:
        print(f'    -> {kid}')
    if len(kids) > 10:
        print(f'    ... and {len(kids)-10} more')

# 层级分布
print()
levels = {}
for f in memory.rglob('*.json'):
    with open(f) as fh:
        n = json.load(fh)
    lv = n.get('level', 1)
    levels[lv] = levels.get(lv, 0) + 1

total = sum(levels.values())
print(f'总节点: {total}')
print('层级分布:')
lmap = {1:'宇宙',2:'星系',3:'恒星',4:'行星',5:'街道',6:'物品',7:'夸克'}
for lv in sorted(levels):
    cnt = levels[lv]
    bar = '#' * max(1, cnt // 20)
    print(f'  L{lv} {lmap[lv]:4s}: {cnt:4d}  {bar}')

# 连通性
orphans = 0
for f in memory.rglob('*.json'):
    with open(f) as fh:
        n = json.load(fh)
    if not n.get('parent_id'):
        orphans += 1
if total > 0:
    print(f'\n孤立节点: {orphans}')
    print(f'连通率: {(total-orphans)/total*100:.1f}%')

# 金字塔形状
print(f'\n金字塔: {" → ".join(str(levels.get(i,0)) for i in range(1,8))}')
