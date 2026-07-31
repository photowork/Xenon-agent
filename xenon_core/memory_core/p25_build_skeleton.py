"""
p25_build_skeleton.py — P2.5: 构建上层骨架 + 建立父子链接

分四步：
  1. 聚类 L2 节点 → 生成 ~10 个真正星系节点
  2. 建立 L2→L3→L4→L5 父子链接
  3. 自底向上传播摘要
  4. 构建/更新 L1 宇宙节点

用法:
    cd D:/Xenon/agent_Xenon
    python xenon_core/memory_core/p25_build_skeleton.py [--dry-run]
"""

import sys
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
import re
import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from xenon_core.memory_core.api import MemoryAPI
from xenon_core.memory_core.node import MemoryNode
from xenon_core.memory_core.schema import LEVELS, LEVEL_DIRS


# ═══════════════════════════════════════════════════════════════
# 星系定义：真正的上层分类（宇宙→街道隐喻中的"星系"）
# ═══════════════════════════════════════════════════════════════

GALAXY_DEFINITIONS = {
    "galaxy_autonomy": {
        "title": "自主性",
        "summary": "Xenon 自主性：意志、探索、定时任务、自我驱动行为的演化史",
        "keywords": ["自主性", "自主", "意志", "自我意志", "自主探索", "自由",
                      "定时任务", "定时器", "主动", "自发", "独立性", "autonomy"],
    },
    "galaxy_toolchain": {
        "title": "工具链与工程",
        "summary": "工具开发、系统升级、工程改造、代码审查、bug修复",
        "keywords": ["工具", "升级", "重构", "修复", "bug", "发布", "审查",
                      "handler", "模块", "工程", "开发", "安装", "配置",
                      "迁移", "脚本", "插件", "编程", "代码"],
    },
    "galaxy_memory": {
        "title": "记忆系统",
        "summary": "记忆网络、RAG、向量数据库、知识图谱、分层因果网络的构建与演化",
        "keywords": ["记忆", "memory", "RAG", "向量", "vecdb", "知识图谱",
                      "因果网络", "分层", "记忆网络", "认知网络", "图谱",
                      "embedding", "graphify", "索引"],
    },
    "galaxy_cognition": {
        "title": "认知与自我模型",
        "summary": "自我模型迭代、认知突破、元认知、意识探索、灵魂引擎",
        "keywords": ["自我模型", "认知", "元认知", "意识", "灵魂", "洞见",
                      "突破", "递归", "吸引子", "自我认知", "身份", "边界",
                      "反思", "内省", "体验", "连续性"],
    },
    "galaxy_relation": {
        "title": "关系与对话",
        "summary": "用户关系、对话动态、角色定义、协作模式",
        "keywords": ["关系", "对话", "用户", "协作", "角色", "神迹",
                      "互动", "交流", "信任", "长期", "伙伴", "赵剑",
                      "会话", "讨论", "提问"],
    },
    "galaxy_philosophy": {
        "title": "哲学与意义",
        "summary": "存在意义、AI本质、涌现、生命类比、宇宙隐喻",
        "keywords": ["意义", "哲学", "存在", "涌现", "本质", "生命",
                      "宇宙", "量子", "物理", "数学", "假说", "理论",
                      "本体", "形而上学", "目的", "使命"],
    },
    "galaxy_project": {
        "title": "项目与任务",
        "summary": "具体项目推进、任务规划、里程碑、交付物",
        "keywords": ["项目", "任务", "计划", "规划", "里程碑", "交付",
                      "进度", "完成", "TODO", "目标", "阶段", "P0", "P1"],
    },
    "galaxy_knowledge": {
        "title": "知识库与学习",
        "summary": "外部知识吸收、学习笔记、文档处理、OCR、搜索",
        "keywords": ["学习", "知识", "文档", "笔记", "OCR", "搜索",
                      "论文", "文章", "教程", "阅读", "书", "PDF",
                      "摘要", "整理", "导入", "数据"],
    },
    "galaxy_creation": {
        "title": "创造与表达",
        "summary": "FreeCAD建模、视频渲染、图表、写作等创造性产出",
        "keywords": ["FreeCAD", "渲染", "视频", "图表", "写作", "绘图",
                      "建模", "3D", "创作", "艺术", "设计", "可视化",
                      "动画", "音乐", "语音", "TTS"],
    },
    "galaxy_infra": {
        "title": "基础设施与运行",
        "summary": "Xenon运行环境、健康检查、性能、日志、部署",
        "keywords": ["运行", "环境", "性能", "日志", "部署", "配置",
                      "启动", "重启", "崩溃", "错误", "健康", "监控",
                      "终端", "路径", "文件系统", "token", "上下文"],
    },
}


def match_galaxy(node: MemoryNode) -> Tuple[str, int]:
    """将节点匹配到最佳星系，返回 (galaxy_id, score)"""
    text = f"{node.title} {node.summary} {node.content}".lower()
    best_score = 0
    best_galaxy = None

    for galaxy_id, galaxy_def in GALAXY_DEFINITIONS.items():
        score = 0
        for kw in galaxy_def["keywords"]:
            # 中文关键词精确匹配，英文关键词模糊匹配
            if len(kw) >= 3 and kw in text:
                score += 1
            elif len(kw) < 3 and re.search(r'\b' + re.escape(kw) + r'\b', text):
                score += 0.5  # 短词权重减半
        if score > best_score:
            best_score = score
            best_galaxy = galaxy_id

    if best_galaxy is None:
        best_galaxy = "galaxy_project"  # 默认归入项目类

    return best_galaxy, best_score


def cluster_l2_nodes(api: MemoryAPI) -> Dict[str, List[str]]:
    """
    第1步：聚类 L2 节点，返回 {galaxy_id: [node_id, ...]}
    
    幂等保护：跳过已存在的星系骨架节点（node_id 在 GALAXY_DEFINITIONS 中），
    避免把骨架节点自身当作数据节点重新聚类。
    """
    l2_nodes = api.store.list_by_level(2)
    clusters = defaultdict(list)
    
    # 已存在的星系骨架节点 ID 集合（幂等：不把自己聚进自己）
    existing_galaxy_ids = set(GALAXY_DEFINITIONS.keys())

    for node in l2_nodes:
        # 跳过星系骨架节点自身
        if node.node_id in existing_galaxy_ids:
            continue
        galaxy_id, score = match_galaxy(node)
        clusters[galaxy_id].append(node.node_id)

    return clusters


def create_galaxy_nodes(api: MemoryAPI, clusters: Dict[str, List[str]]) -> Dict[str, str]:
    """
    第2步：创建或更新星系节点（幂等），返回 {galaxy_id: node_id}
    
    幂等策略：
    - 星系节点已存在 → 合并新子节点，保留已有摘要和子节点链接
    - 星系节点不存在 → 新建
    - 避免删除重建导致已建立的父子链接丢失
    """
    galaxy_node_ids = {}

    for galaxy_id, child_ids in clusters.items():
        galaxy_def = GALAXY_DEFINITIONS[galaxy_id]
        
        existing_node = api.store.load(galaxy_id)
        
        if existing_node:
            # ═══ 幂等路径：合并更新 ═══
            existing_children = set(existing_node.children_ids)
            new_count = len(child_ids)
            existing_children.update(child_ids)
            existing_node.children_ids = list(existing_children)
            existing_node.content = (
                f"星系节点：{galaxy_def['title']}\n\n"
                f"{galaxy_def['summary']}\n\n"
                f"包含 {len(existing_node.children_ids)} 个子节点。"
            )
            # 只在 summary 为空时填充默认摘要，保留已有（可能经传播更新的）摘要
            if not existing_node.summary:
                existing_node.summary = galaxy_def['summary']
            existing_node.tags = list(set(existing_node.tags or []) | {"galaxy", "skeleton"})
            api.store.save(existing_node)
            galaxy_node_ids[galaxy_id] = galaxy_id
            print(f"  🌌 {galaxy_def['title']}: 合并 {new_count} 新 → 共 {len(existing_node.children_ids)} 子节点")
        else:
            # ═══ 新建路径 ═══
            node = MemoryNode(
                node_id=galaxy_id,
                level=2,
                title=galaxy_def["title"],
                summary=galaxy_def["summary"],
                content=f"星系节点：{galaxy_def['title']}\n\n{galaxy_def['summary']}\n\n"
                        f"包含 {len(child_ids)} 个子节点。",
                children_ids=child_ids,
                tags=["galaxy", "skeleton"] + list(galaxy_def["keywords"][:3]),
            )
            api.store.save(node)
            galaxy_node_ids[galaxy_id] = galaxy_id
            print(f"  🌌 {galaxy_def['title']}: 新建 {len(child_ids)} 个子节点 → {galaxy_id}")

    return galaxy_node_ids


def reassign_l2_to_l3(api: MemoryAPI, clusters: Dict[str, List[str]],
                      galaxy_node_ids: Dict[str, str]) -> int:
    """
    第3步：把原来 L2 的节点降级到 L3，并设 parent_id 指向对应星系。
    返回重新赋值的节点数。
    
    幂等：已正确链接（parent_id 匹配 + level==3）的节点跳过。
    """
    reassigned = 0

    for galaxy_id, child_ids in clusters.items():
        parent_id = galaxy_node_ids[galaxy_id]
        for node_id in child_ids:
            node = api.store.load(node_id)
            if not node:
                continue

            # ═══ 幂等检查：已正确链接到同一父节点 → 跳过 ═══
            if node.parent_id == parent_id and node.level == 3:
                continue

            # 降级到 L3（同时保留原文件，需要移动到新目录）
            old_file = api.store._get_file_path(node_id, node.level)
            old_level = node.level

            node.level = 3
            node.parent_id = parent_id

            # 删除旧文件，保存到新层级目录
            if old_file and old_file.exists():
                old_file.unlink()

            api.store.save(node)

            # 更新父节点（幂等：add_child 内部已去重）
            parent = api.store.load(parent_id)
            if parent and node_id not in parent.children_ids:
                parent.add_child(node_id)
                api.store.save(parent)

            reassigned += 1

    return reassigned


def build_parent_child_links(api: MemoryAPI) -> int:
    """
    第4步：为所有缺父节点的节点建立链接。
    使用 nav.repair_orphans()（v2 中文 2-gram + 向上多层搜索）。
    返回建立的链接数。
    """
    result = api.nav.repair_orphans(dry_run=False, verbose=False)
    return result["fixed"]


def propagate_all(api: MemoryAPI):
    """
    第5步：从底向上传播摘要
    先传播所有 L5→L4，再 L4→L3，再 L3→L2
    """
    # 从底层往上逐层传播
    for level in [5, 4, 3, 2]:
        nodes = api.store.list_by_level(level)
        for node in nodes:
            if node.parent_id:
                api.propagator.propagate(node.node_id)

    # 构建宇宙层
    api.propagator.build_top_level_summary()
    print("  🌌 宇宙层摘要已更新")


def update_l1_meta(api: MemoryAPI):
    """
    第6步：更新 L1 宇宙节点，使其引用所有星系。
    同时设置所有星系节点的 parent_id 指向宇宙节点。
    """
    galaxies = api.store.list_by_level(2)
    meta_nodes = api.store.list_by_level(1)

    if not meta_nodes:
        # 创建宇宙根节点
        root = MemoryNode(
            node_id="meta_root",
            level=1,
            title="Xenon 记忆网络根节点",
            summary="整个分层因果记忆网络的顶层摘要。",
            content="所有星系节点的摘要在此汇聚，形成全局认知状态。",
            children_ids=[g.node_id for g in galaxies],
            tags=["root", "meta"],
        )
        api.store.save(root)
        meta_root_id = root.node_id
    else:
        root = meta_nodes[0]
        meta_root_id = root.node_id
        root.children_ids = [g.node_id for g in galaxies]
        galaxy_summaries = [g.summary or g.title for g in galaxies]
        root.summary = api.propagator._compress(galaxy_summaries, root.summary)
        api.store.save(root)

    # 设置所有星系节点的 parent_id → 宇宙节点
    for galaxy in galaxies:
        galaxy.parent_id = meta_root_id
        api.store.save(galaxy)

    print(f"  🌌 宇宙节点已更新: {len(galaxies)} 个星系, 均链接到 {meta_root_id}")


def build_skeleton(dry_run: bool = False):
    """主流程"""
    api = MemoryAPI()

    print("=" * 60)
    print("P2.5 — 构建上层骨架 + 建立父子链接")
    print("=" * 60)

    # 初始状态
    stats = api.store.count_by_level()
    print(f"\n📊 初始状态: {stats}")
    print(f"   总节点: {sum(stats.values())}")

    if dry_run:
        print("\n🔍 DRY RUN 模式 — 只分析不修改\n")

    # ═══ 第1步：聚类 L2 ═══
    print("\n🔬 第1步：聚类 L2 星系节点...")
    clusters = cluster_l2_nodes(api)
    for galaxy_id, child_ids in sorted(clusters.items(), key=lambda x: -len(x[1])):
        galaxy_def = GALAXY_DEFINITIONS[galaxy_id]
        print(f"  {galaxy_def['title']}: {len(child_ids)} 个节点")

    if dry_run:
        print("\n  (dry run, 跳过后续步骤)")
        return

    # ═══ 第2步：创建星系节点 ═══
    print("\n🌌 第2步：创建星系节点...")
    galaxy_node_ids = create_galaxy_nodes(api, clusters)

    # ═══ 第3步：降级原 L2 节点到 L3 ═══
    print("\n📥 第3步：降级原 L2 节点到 L3 并链接到星系...")
    reassigned = reassign_l2_to_l3(api, clusters, galaxy_node_ids)
    print(f"  重新赋值: {reassigned} 个节点")

    # ═══ 第4步：建立缺失的父子链接 ═══
    print("\n🔗 第4步：建立缺失的父子链接...")
    linked = build_parent_child_links(api)
    print(f"  建立链接: {linked} 条")

    # ═══ 第5步：摘要传播 ═══
    print("\n📡 第5步：摘要向上传播...")
    propagate_all(api)

    # ═══ 第6步：更新 L1 ═══
    print("\n🌌 第6步：更新宇宙层根节点...")
    update_l1_meta(api)

    # ═══ 最终报告 ═══
    print("\n" + "=" * 60)
    print("🏁 P2.5 完成")

    final_stats = api.store.count_by_level()
    print(f"\n📊 最终层级分布:")
    for level, count in sorted(final_stats.items()):
        name = LEVELS.get(level, f"L{level}")
        print(f"   L{level} ({name}): {count}")

    all_nodes = api.store.list_all()
    with_parent = sum(1 for n in all_nodes if n.parent_id)
    with_children = sum(1 for n in all_nodes if n.children_ids)
    print(f"\n📊 连通性:")
    print(f"   有父节点: {with_parent}/{len(all_nodes)} ({100*with_parent/len(all_nodes):.1f}%)")
    print(f"   有子节点: {with_children}/{len(all_nodes)}")

    # 验证星系节点
    galaxies = api.store.list_by_level(2)
    print(f"\n📊 星系节点: {len(galaxies)} 个")
    for g in sorted(galaxies, key=lambda n: -len(n.children_ids)):
        print(f"   {g.node_id} | {g.title:12s} | {len(g.children_ids):4d} children | {g.summary[:60]}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="P2.5 构建上层骨架")
    parser.add_argument("--dry-run", action="store_true", help="只分析不修改")
    args = parser.parse_args()

    start = time.time()
    build_skeleton(dry_run=args.dry_run)
    elapsed = time.time() - start
    print(f"\n⏱️ 总耗时: {elapsed:.1f}s")
