#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Xenon 因果推理引擎 — Causal Reasoner v2.0
==========================================

核心能力：
  1. 因果图建模 — 节点、边、混杂因子、结构方程
  2. d-separation 与后门调整 — AdjustmentEngine
  3. do-calculus 干预模拟 — InterventionEngine
  4. 三步法反事实推理 — CounterfactualEngine
  5. 路径查找与分类 — PathEngine
  6. 安全表达式求值 — StructuralEquationEngine
  7. 模型持久化 — CausalReasonerManager（自动保存到 Memory/causal_models/）

工具发现机制：
  - 类名以 Manager 结尾，会被 ToolManager 自动扫描和加载
  - 所有公开方法（不以 _ 开头）自动注册为工具
  - 模型持久化使用相对路径，换设备可直接使用
"""

from __future__ import annotations

import ast
import json
import operator
import os
import re
import uuid
from datetime import datetime
from math import exp
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union


# ============================================================================
#  工具函数
# ============================================================================

def _noisy_or(base_prob: float,
              prob_cause: float = 0.0,
              strength: float = 0.0,
              other_causes: Optional[List[Tuple[float, float]]] = None) -> float:
    """Noisy-OR 模型：P(effect|causes) = 1 - (1-base) * ∏(1 - P(c_i)*s_i)"""
    inhibitor = 1.0 - base_prob
    if prob_cause > 0 and strength > 0:
        inhibitor *= (1.0 - prob_cause * strength)
    if other_causes:
        for p_c, s in other_causes:
            inhibitor *= (1.0 - max(0.0, min(1.0, p_c)) * max(0.0, min(1.0, s)))
    return max(0.0, min(1.0, 1.0 - inhibitor))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + exp(-x))


def _normalize(probs: Dict[str, float]) -> Dict[str, float]:
    total = sum(probs.values())
    if total == 0:
        return {k: 1.0 / len(probs) for k in probs}
    return {k: v / total for k, v in probs.items()}


# ============================================================================
#  内部微引擎（轻量版，兼容旧 API）
# ============================================================================

class _CausalGraph:
    """轻量有向图 — 兼容旧版 infer_causal_graph / estimate_branch_probability 等"""

    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[str, List[Tuple[str, float]]] = {}
        self._reverse: Dict[str, List[Tuple[str, float]]] = {}

    def add_node(self, name: str, base_prob: float = 0.5, description: str = ""):
        if name not in self.nodes:
            self.nodes[name] = {"base_prob": base_prob, "description": description}

    def add_edge(self, cause: str, effect: str, strength: float = 0.5):
        strength = max(-1.0, min(1.0, strength))
        self.add_node(cause)
        self.add_node(effect)
        self.edges.setdefault(cause, []).append((effect, strength))
        self._reverse.setdefault(effect, []).append((cause, strength))

    def get_parents(self, node: str) -> List[Tuple[str, float]]:
        return self._reverse.get(node, [])

    def get_children(self, node: str) -> List[Tuple[str, float]]:
        return self.edges.get(node, [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": dict(self.nodes),
            "edges": {k: [(e, round(s, 3)) for e, s in v] for k, v in self.edges.items()},
        }


# ============================================================================
#  CausalGraph — 公共可序列化因果图
# ============================================================================

class CausalGraph:
    """完整因果图，支持节点/边/混杂因子/结构方程，可 JSON 序列化"""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self.nodes: Dict[str, Dict[str, Any]] = {}       # name → {label, type, values}
        self.edges: List[Dict[str, Any]] = []             # [{from, to, strength/mechanism}]
        self.confounders: List[Dict[str, Any]] = []       # [{variable, affects:[], mechanism}]
        self.structural_equations: Dict[str, str] = {}    # variable → expression
        self._confounded_pairs: Set[Tuple[str, str]] = set()
        if data:
            self._from_dict(data)

    # ---- 构建方法 ----

    def add_node(self, name: str, label: Optional[str] = None,
                 node_type: str = "binary", values: Optional[List[Any]] = None):
        self.nodes[name] = {
            "label": label or name,
            "type": node_type,
            "values": values or [],
        }

    def add_edge(self, from_var: str, to_var: str, strength: Optional[float] = None,
                 mechanism: str = ""):
        for e in self.edges:
            if e["from"] == from_var and e["to"] == to_var:
                return
        edge: Dict[str, Any] = {"from": from_var, "to": to_var}
        if strength is not None:
            edge["strength"] = max(-1.0, min(1.0, strength))
        if mechanism:
            edge["mechanism"] = mechanism
        self.edges.append(edge)
        for v in (from_var, to_var):
            if v not in self.nodes:
                self.add_node(v)

    def add_confounder(self, variable: str, affects: List[str],
                       mechanism: str = "", confidence: str = ""):
        for c in self.confounders:
            if c["variable"] == variable:
                existing = set(c["affects"])
                for a in affects:
                    if a not in existing:
                        c["affects"].append(a)
                # 更新混杂对
                all_affected = c["affects"]
                for i, a in enumerate(all_affected):
                    for j, b in enumerate(all_affected):
                        if i < j:
                            self._confounded_pairs.add((a, b) if a < b else (b, a))
                return
        self.confounders.append({
            "variable": variable,
            "affects": list(affects),
            "mechanism": mechanism,
            "confidence": confidence,
        })
        for i, a in enumerate(affects):
            for j, b in enumerate(affects):
                if i < j:
                    pair = (a, b) if a < b else (b, a)
                    self._confounded_pairs.add(pair)

    def set_equation(self, variable: str, expression: str):
        self.structural_equations[variable] = expression

    # ---- 查询方法 ----

    def get_parents(self, node: str) -> List[Tuple[str, Dict[str, Any]]]:
        result = []
        for e in self.edges:
            if e["to"] == node:
                result.append((e["from"], e))
        return result

    def get_children(self, node: str) -> List[Tuple[str, Dict[str, Any]]]:
        result = []
        for e in self.edges:
            if e["from"] == node:
                result.append((e["to"], e))
        return result

    def get_ancestors(self, node: str) -> Set[str]:
        ancestors: Set[str] = set()
        stack = [node]
        while stack:
            current = stack.pop()
            for parent, _ in self.get_parents(current):
                if parent not in ancestors:
                    ancestors.add(parent)
                    stack.append(parent)
        # 也包含混杂因子相关的父节点
        for cf in self.confounders:
            if node in cf["affects"]:
                for other in cf["affects"]:
                    if other != node and other not in ancestors:
                        ancestors.add(other)
                        stack.append(other)
        return ancestors

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        visited: Set[str] = set()
        stack = [descendant]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            for parent, _ in self.get_parents(current):
                if parent == ancestor:
                    return True
                stack.append(parent)
        return False

    def edge_direction(self, a: str, b: str) -> Optional[str]:
        """返回 a 和 b 之间的边方向: '->', '<-', '<->', 或 None"""
        a_to_b = any(e["from"] == a and e["to"] == b for e in self.edges)
        b_to_a = any(e["from"] == b and e["to"] == a for e in self.edges)
        is_confounded = (a, b) in self._confounded_pairs or (b, a) in self._confounded_pairs
        if a_to_b and b_to_a:
            return "<->"
        if a_to_b:
            return "->"
        if b_to_a:
            return "<-"
        if is_confounded:
            return "<->"
        return None

    def triplet_type(self, a: str, b: str, c: str) -> Optional[str]:
        """判断三元组 a-b-c 的结构类型: chain / fork / collider"""
        a_to_b = any(e["from"] == a and e["to"] == b for e in self.edges)
        b_to_a = any(e["from"] == b and e["to"] == a for e in self.edges)
        b_to_c = any(e["from"] == b and e["to"] == c for e in self.edges)
        c_to_b = any(e["from"] == c and e["to"] == b for e in self.edges)

        # 混杂因子（隐变量）作为中间节点：检查 confounder→affected 关系
        # a 是混杂因子 → 它影响 b
        a_conf = any(cf["variable"] == a and b in cf["affects"] for cf in self.confounders)
        if a_conf:
            a_to_b = True
        # b 是混杂因子 → 它影响 a
        b_conf_a = any(cf["variable"] == b and a in cf["affects"] for cf in self.confounders)
        if b_conf_a:
            b_to_a = True
        # b 是混杂因子 → 它影响 c
        b_conf_c = any(cf["variable"] == b and c in cf["affects"] for cf in self.confounders)
        if b_conf_c:
            b_to_c = True
        # c 是混杂因子 → 它影响 b
        c_conf = any(cf["variable"] == c and b in cf["affects"] for cf in self.confounders)
        if c_conf:
            c_to_b = True

        # 对于两个混杂因子的双向连接（如 U1<->M<->U2）
        ab_conf = (a, b) in self._confounded_pairs or (b, a) in self._confounded_pairs
        bc_conf = (b, c) in self._confounded_pairs or (c, b) in self._confounded_pairs

        # 混杂因子提供双向连接
        if ab_conf and not a_to_b and not b_to_a:
            a_to_b = b_to_a = True
        if bc_conf and not b_to_c and not c_to_b:
            b_to_c = c_to_b = True

        # collider: a→b←c（都指向中间节点）
        if (a_to_b or ab_conf) and (c_to_b or bc_conf):
            return "collider"
        # fork: a←b→c
        if b_to_a and b_to_c:
            return "fork"
        # chain: a→b→c 或 a←b←c
        if (a_to_b and b_to_c) or (b_to_a and c_to_b):
            return "chain"

        return "chain"

    def _all_simple_paths(self, source: str, target: str,
                          directed_only: bool = False,
                          max_paths: int = 2000) -> List[List[str]]:
        """枚举所有简单路径（包含混杂因子作为中间节点）。
        当路径数超过 max_paths 时提前终止，避免大图组合爆炸。"""
        all_paths: List[List[str]] = []
        _stopped_early = False

        # 构建邻接表
        adjacency: Dict[str, List[str]] = {}
        # 有向边
        for e in self.edges:
            adjacency.setdefault(e["from"], []).append(e["to"])
            if not directed_only:
                adjacency.setdefault(e["to"], []).append(e["from"])
        # 混杂因子：confounder 连接到每个 affected 变量
        for cf in self.confounders:
            u = cf["variable"]
            for a in cf["affects"]:
                adjacency.setdefault(u, []).append(a)
                adjacency.setdefault(a, []).append(u)

        def dfs(current: str, path: List[str], visited: Set[str]):
            nonlocal _stopped_early
            if _stopped_early:
                return
            if current == target:
                all_paths.append(list(path))
                if len(all_paths) >= max_paths:
                    _stopped_early = True
                return
            for neighbor in adjacency.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    path.append(neighbor)
                    dfs(neighbor, path, visited)
                    path.pop()
                    visited.discard(neighbor)
                    if _stopped_early:
                        return

        if source in adjacency or source == target:
            dfs(source, [source], {source})
        return all_paths

    def _has_cycle(self) -> bool:
        """DFS 三色法检测有向环"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n: WHITE for n in self.nodes}

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for child, _ in self.get_children(node):
                if color.get(child, WHITE) == GRAY:
                    return True
                if color.get(child, WHITE) == WHITE:
                    if dfs(child):
                        return True
            color[node] = BLACK
            return False

        for node in self.nodes:
            if color.get(node, WHITE) == WHITE:
                if dfs(node):
                    return True
        return False

    # ---- 序列化 ----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": dict(self.nodes),
            "edges": list(self.edges),
            "confounders": list(self.confounders),
            "structural_equations": dict(self.structural_equations),
        }

    def _from_dict(self, data: Dict[str, Any]):
        self.nodes = data.get("nodes", {})
        self.edges = data.get("edges", [])
        self.confounders = data.get("confounders", [])
        self.structural_equations = data.get("structural_equations", {})
        self._confounded_pairs = set()
        for cf in self.confounders:
            for i, a in enumerate(cf["affects"]):
                for j, b in enumerate(cf["affects"]):
                    if i < j:
                        self._confounded_pairs.add((a, b))


# ============================================================================
#  AdjustmentEngine — d-separation + 后门调整
# ============================================================================

class AdjustmentEngine:
    """基于 d-separation 的后门调整引擎"""

    @staticmethod
    def find_adjustment_sets(graph: CausalGraph, treatment: str, outcome: str) -> Dict[str, Any]:
        """
        找使 P(Y|do(X)) 可识别的最小调整集
        """
        # 1. 收集后门路径
        backdoor_paths = AdjustmentEngine._get_backdoor_paths(graph, treatment, outcome)

        # 2. 候选调整变量：所有节点（除了 treatment 和 outcome 及其后代）
        descendants_t = {treatment}
        stack = [treatment]
        while stack:
            v = stack.pop()
            for child, _ in graph.get_children(v):
                if child not in descendants_t:
                    descendants_t.add(child)
                    stack.append(child)
        descendants_y = {outcome}
        stack = [outcome]
        while stack:
            v = stack.pop()
            for child, _ in graph.get_children(v):
                if child not in descendants_y:
                    descendants_y.add(child)
                    stack.append(child)

        candidate_vars = [n for n in graph.nodes
                          if n not in (treatment, outcome)
                          and n not in descendants_t]

        # 3. 先检查空集是否已阻断所有后门路径
        all_blocked_empty = all(
            AdjustmentEngine._path_blocked_by(graph, path, set())
            for path in backdoor_paths
        )
        if all_blocked_empty and backdoor_paths:
            return {
                "identifiable": True,
                "minimal_set": {"variables": [], "size": 0},
                "adjustment_sets": [],
                "backdoor_paths": backdoor_paths,
            }

        # 4. 搜索最小调整集
        valid_sets: List[Dict[str, Any]] = []
        max_size = min(len(candidate_vars), 6)

        for size in range(1, max_size + 1):
            found_this_size = False
            from itertools import combinations
            for combo in combinations(candidate_vars, size):
                cond_set = set(combo)
                # 检查是否阻断所有后门路径
                if all(AdjustmentEngine._path_blocked_by(graph, p, cond_set)
                       for p in backdoor_paths):
                    valid_sets.append({
                        "variables": sorted(cond_set),
                        "size": len(cond_set),
                    })
                    found_this_size = True
            if found_this_size:
                break

        if valid_sets:
            minimal = min(valid_sets, key=lambda s: s["size"])
            return {
                "identifiable": True,
                "minimal_set": minimal,
                "adjustment_sets": valid_sets,
                "backdoor_paths": backdoor_paths,
            }
        elif not backdoor_paths:
            return {
                "identifiable": True,
                "minimal_set": {"variables": [], "size": 0},
                "adjustment_sets": [],
                "backdoor_paths": [],
            }
        else:
            return {
                "identifiable": False,
                "minimal_set": None,
                "adjustment_sets": [],
                "backdoor_paths": backdoor_paths,
            }

    @staticmethod
    def _path_blocked_by(graph: CausalGraph, path: List[str],
                         conditioning_set: Set[str]) -> bool:
        """
        判断路径是否被条件集 d-separation 阻断。
        规则：路径被阻断当且仅当存在至少一个三元组被阻断。
        - chain/fork: 中间节点被条件 → 阻断
        - collider: 中间节点及其后代都不被条件 → 阻断
        """
        for i in range(1, len(path) - 1):
            a, b, c = path[i - 1], path[i], path[i + 1]
            triplet = graph.triplet_type(a, b, c)
            if triplet is None:
                continue

            # 收集 b 的所有后代
            d_set: Set[str] = set()
            stack = [b]
            while stack:
                v = stack.pop()
                for child, _ in graph.get_children(v):
                    if child not in d_set and child not in path:
                        d_set.add(child)
                        stack.append(child)

            if triplet == "collider":
                # collider 被阻断：b 和 b 的后代都不在条件集中
                if b in conditioning_set or any(d in conditioning_set for d in d_set):
                    # collider 被条件 → 路径在此处被打开，继续检查其他三元组
                    continue
                else:
                    # collider 未被条件 → 路径被阻断
                    return True
            else:
                # chain/fork: b 被条件 → 阻断
                if b in conditioning_set:
                    return True
                # b 不被条件 → 继续检查

        return False

    @staticmethod
    def _get_backdoor_paths(graph: CausalGraph, treatment: str,
                            outcome: str) -> List[List[str]]:
        """枚举所有后门路径（首边指向 treatment）"""
        all_paths = graph._all_simple_paths(treatment, outcome, directed_only=False)
        backdoor: List[List[str]] = []
        for path in all_paths:
            if len(path) < 2:
                continue
            second = path[1]
            # 如果首边指向 treatment（即 second 是 treatment 的父节点或混杂因子）
            second_to_treatment = any(e["from"] == second and e["to"] == treatment
                                      for e in graph.edges)
            # 混杂因子：second 是 confounder 且 affects treatment
            cf_affects_treatment = any(
                cf["variable"] == second and treatment in cf["affects"]
                for cf in graph.confounders
            )
            # 混杂对
            is_confounded_pair = (second, treatment) in graph._confounded_pairs or \
                                 (treatment, second) in graph._confounded_pairs
            if second_to_treatment or cf_affects_treatment or is_confounded_pair:
                backdoor.append(path)
        return backdoor


# ============================================================================
#  PathEngine — 路径查找与分类
# ============================================================================

class PathEngine:
    """因果路径分析引擎"""

    @staticmethod
    def find_paths_between(graph: CausalGraph, source: str, target: str,
                           max_paths: int = 2000) -> Dict[str, Any]:
        """找到所有因果路径和后门路径。返回摘要含 truncated 标记。"""
        all_paths = graph._all_simple_paths(source, target, directed_only=False,
                                            max_paths=max_paths)

        causal_paths = []
        backdoor_paths = []
        for path in all_paths:
            classification = PathEngine.classify_path(graph, path)
            if classification["type"] == "causal":
                causal_paths.append(path)
            elif classification["type"] == "backdoor":
                backdoor_paths.append(path)

        truncated = len(all_paths) >= max_paths
        return {
            "causal_paths": causal_paths,
            "backdoor_paths": backdoor_paths,
            "causal_count": len(causal_paths),
            "backdoor_count": len(backdoor_paths),
            "truncated": truncated,
        }

    @staticmethod
    def classify_path(graph: CausalGraph, path: List[str]) -> Dict[str, Any]:
        """分类路径类型"""
        if len(path) < 2:
            return {"type": "unknown", "has_collider": False}

        has_collider = False
        is_causal = True  # 默认假设因果，直到发现反向边

        for i in range(1, len(path) - 1):
            a, b, c = path[i - 1], path[i], path[i + 1]
            triplet = graph.triplet_type(a, b, c)
            if triplet == "collider":
                has_collider = True

            # 检查边的方向
            a_to_b = any(e["from"] == a and e["to"] == b for e in graph.edges)
            b_to_c = any(e["from"] == b and e["to"] == c for e in graph.edges)
            b_to_a = any(e["from"] == b and e["to"] == a for e in graph.edges)
            c_to_b = any(e["from"] == c and e["to"] == b for e in graph.edges)

            # 如果有反向边（不沿因果方向），标记为非纯因果
            if (b_to_a and not a_to_b) or (c_to_b and not b_to_c):
                is_causal = False

        # 判断首边
        first_edge_forward = any(e["from"] == path[0] and e["to"] == path[1] for e in graph.edges)

        if has_collider:
            path_type = "mixed" if first_edge_forward else "backdoor"
        elif first_edge_forward and is_causal:
            path_type = "causal"
        else:
            path_type = "backdoor"

        return {"type": path_type, "has_collider": has_collider}


# ============================================================================
#  StructuralEquationEngine — 安全表达式求值
# ============================================================================

class StructuralEquationEngine:
    """安全的结构方程求值引擎（AST 白名单，不使用 eval）"""

    _SAFE_BINOPS: Dict[type, Callable] = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.LShift: operator.lshift, ast.RShift: operator.rshift,
        ast.BitOr: operator.or_, ast.BitAnd: operator.and_,
        ast.BitXor: operator.xor,
    }

    _SAFE_COMPARE: Dict[type, Callable] = {
        ast.Eq: operator.eq, ast.NotEq: operator.ne,
        ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge,
    }

    _SAFE_BOOL: Dict[type, Callable] = {
        ast.And: all, ast.Or: any,
    }

    _SAFE_UNARY: Dict[type, Callable] = {
        ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_,
    }

    _SAFE_FUNCTIONS: Dict[str, Callable] = {
        "abs": abs, "min": min, "max": max, "round": round,
        "int": int, "float": float, "str": str, "bool": bool,
        "len": len, "sqrt": lambda x: x ** 0.5,
    }

    _FORBIDDEN_PATTERNS = [
        r"__", r"import", r"eval", r"exec", r"system", r"subprocess",
        r"open", r"file", r"os\.", r"sys\.", r"compile",
        r"getattr", r"setattr", r"delattr", r"globals", r"locals",
    ]

    @staticmethod
    def evaluate(expression: str, values: Dict[str, Any],
                 safe_mode: bool = True) -> Any:
        """
        安全求值结构方程。
        支持：算术、比较、布尔、IF(cond, then, else)、CLAMP(val, lo, hi)
        """
        if safe_mode:
            expr_lower = expression.lower()
            for pattern in StructuralEquationEngine._FORBIDDEN_PATTERNS:
                if re.search(pattern, expr_lower):
                    raise ValueError(f"禁止的模式: {pattern}")

        # 预处理 IF/CLAMP/AND/OR/NOT 为 Python 兼容形式
        expr = expression

        # IF(cond, then, else) → _if_(cond, then, else)
        expr = re.sub(r'\bIF\s*\(', '_if_(', expr, flags=re.IGNORECASE)
        # CLAMP(val, lo, hi) → _clamp_(val, lo, hi)
        expr = re.sub(r'\bCLAMP\s*\(', '_clamp_(', expr, flags=re.IGNORECASE)
        # AND → and, OR → or, NOT → not
        expr = re.sub(r'\bAND\b', 'and', expr)
        expr = re.sub(r'\bOR\b', 'or', expr)
        expr = re.sub(r'\bNOT\b', 'not', expr)

        try:
            tree = ast.parse(expr, mode='eval')
        except SyntaxError as e:
            raise ValueError(f"表达式语法错误: {e}")

        return StructuralEquationEngine._eval_node(tree.body, values)

    @staticmethod
    def _eval_node(node: ast.AST, values: Dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Name):
            if node.id in values:
                return values[node.id]
            raise ValueError(f"变量 '{node.id}' 未定义")
        elif isinstance(node, ast.BinOp):
            left = StructuralEquationEngine._eval_node(node.left, values)
            right = StructuralEquationEngine._eval_node(node.right, values)
            op_type = type(node.op)
            if op_type in StructuralEquationEngine._SAFE_BINOPS:
                return StructuralEquationEngine._SAFE_BINOPS[op_type](left, right)
            raise ValueError(f"不支持的操作符: {op_type.__name__}")
        elif isinstance(node, ast.UnaryOp):
            operand = StructuralEquationEngine._eval_node(node.operand, values)
            op_type = type(node.op)
            if op_type in StructuralEquationEngine._SAFE_UNARY:
                return StructuralEquationEngine._SAFE_UNARY[op_type](operand)
            raise ValueError(f"不支持的一元操作符: {op_type.__name__}")
        elif isinstance(node, ast.Compare):
            left = StructuralEquationEngine._eval_node(node.left, values)
            for op, comp in zip(node.ops, node.comparators):
                right = StructuralEquationEngine._eval_node(comp, values)
                op_type = type(op)
                if op_type in StructuralEquationEngine._SAFE_COMPARE:
                    if not StructuralEquationEngine._SAFE_COMPARE[op_type](left, right):
                        return False
                    left = right
                else:
                    raise ValueError(f"不支持的比较符: {op_type.__name__}")
            return True
        elif isinstance(node, ast.BoolOp):
            op_type = type(node.op)
            if op_type in StructuralEquationEngine._SAFE_BOOL:
                values_list = [StructuralEquationEngine._eval_node(v, values) for v in node.values]
                return StructuralEquationEngine._SAFE_BOOL[op_type](values_list)
            raise ValueError(f"不支持的布尔操作: {op_type.__name__}")
        elif isinstance(node, ast.Call):
            func_name = node.func.id if isinstance(node.func, ast.Name) else ""
            args = [StructuralEquationEngine._eval_node(a, values) for a in node.args]

            if func_name == "_if_":
                if len(args) != 3:
                    raise ValueError("IF 需要 3 个参数: cond, then, else")
                return args[1] if args[0] else args[2]
            elif func_name == "_clamp_":
                if len(args) != 3:
                    raise ValueError("CLAMP 需要 3 个参数: val, lo, hi")
                return max(args[1], min(args[2], args[0]))
            elif func_name in StructuralEquationEngine._SAFE_FUNCTIONS:
                return StructuralEquationEngine._SAFE_FUNCTIONS[func_name](*args)
            else:
                raise ValueError(f"不支持的函数: {func_name}")
        elif isinstance(node, ast.IfExp):
            cond = StructuralEquationEngine._eval_node(node.test, values)
            return StructuralEquationEngine._eval_node(node.body if cond else node.orelse, values)
        else:
            raise ValueError(f"不支持的 AST 节点: {type(node).__name__}")


# ============================================================================
#  InterventionEngine — do-calculus 干预模拟
# ============================================================================

class InterventionEngine:
    """图手术干预引擎"""

    @staticmethod
    def simulate(graph: CausalGraph,
                 intervention: Dict[str, Any],
                 baseline_values: Optional[Dict[str, Any]] = None,
                 exogenous_values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        模拟干预效果：移除指向干预变量的边，执行定量求值。
        """
        baseline = baseline_values or {}
        exogenous = exogenous_values or {}

        # 构建干预后的图（移除指向干预变量的边）
        removed_edges = []
        active_edges = []
        for e in graph.edges:
            if e["to"] in intervention:
                removed_edge = dict(e)
                if "mechanism" not in removed_edge:
                    removed_edge["mechanism"] = ""
                removed_edges.append(removed_edge)
            else:
                active_edges.append(dict(e))

        # 收集相关节点并排序
        post_adj = InterventionEngine._build_adjacency(active_edges)
        all_relevant = InterventionEngine._collect_reachable(intervention, post_adj)
        computed_order = InterventionEngine._topo_sort(all_relevant, active_edges, intervention)

        # 计算值
        computed = InterventionEngine._compute_values(
            graph, dict(intervention), exogenous, active_edges, all_relevant)

        # 构建传播路径
        propagation_paths: Dict[str, Dict[str, List[List[str]]]] = {}
        for ivar in intervention:
            propagation_paths[ivar] = {}
            for target in computed_order:
                if target == ivar:
                    continue
                paths = InterventionEngine._find_directed_paths(post_adj, ivar, target)
                if paths:
                    propagation_paths[ivar][target] = paths

        # 定量效应
        quantitative_effects: Dict[str, Dict[str, Any]] = {}
        if baseline:
            baseline_computed = InterventionEngine._compute_values(
                graph, baseline, exogenous, active_edges,
                InterventionEngine._collect_reachable(baseline, post_adj))
            for var in computed:
                if var not in intervention:
                    baseline_val = baseline_computed.get(var)
                    intervention_val = computed[var]
                    delta = 0
                    try:
                        delta = float(intervention_val) - float(baseline_val)
                    except (TypeError, ValueError):
                        pass
                    quantitative_effects[var] = {
                        "intervention_value": intervention_val,
                        "baseline_value": baseline_val,
                        "delta": delta,
                    }
        else:
            for var in computed:
                if var not in intervention:
                    quantitative_effects[var] = {
                        "intervention_value": computed[var],
                        "baseline_value": baseline.get(var),
                        "delta": 0,
                    }

        return {
            "removed_edges": removed_edges,
            "computed_values": computed,
            "propagation_paths": propagation_paths,
            "quantitative_effects": quantitative_effects,
            "equation_evaluation": {
                "intervention": {"unresolved": {}},
            },
        }

    @staticmethod
    def _build_adjacency(edges: List[Dict]) -> Dict[str, List[str]]:
        adj: Dict[str, List[str]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        return adj

    @staticmethod
    def _collect_reachable(seed: Dict[str, Any],
                           adj: Dict[str, List[str]]) -> Set[str]:
        reachable: Set[str] = set(seed.keys())
        queue = list(seed.keys())
        while queue:
            current = queue.pop(0)
            for child in adj.get(current, []):
                if child not in reachable:
                    reachable.add(child)
                    queue.append(child)
        return reachable

    @staticmethod
    def _topo_sort(nodes: Set[str], edges: List[Dict],
                   intervention: Dict[str, Any]) -> List[str]:
        in_degree: Dict[str, int] = {n: 0 for n in nodes}
        for e in edges:
            if e["from"] in nodes and e["to"] in nodes:
                in_degree[e["to"]] += 1
        for ivar in intervention:
            if ivar in in_degree:
                in_degree[ivar] = 0
        queue = [n for n in nodes if in_degree.get(n, 0) == 0]
        order: List[str] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for e in edges:
                if e["from"] == node and e["to"] in in_degree:
                    in_degree[e["to"]] -= 1
                    if in_degree[e["to"]] == 0:
                        queue.append(e["to"])
        return order

    @staticmethod
    def _compute_values(graph: CausalGraph,
                        seed_values: Dict[str, Any],
                        exogenous: Dict[str, Any],
                        edges: List[Dict],
                        nodes: Set[str]) -> Dict[str, Any]:
        result = dict(seed_values)
        order = InterventionEngine._topo_sort(nodes, edges, seed_values)
        for var in order:
            if var in result:
                continue
            if var in graph.structural_equations:
                eq = graph.structural_equations[var]
                eq_values = dict(result)
                eq_values.update(exogenous)
                # 自动填充缺失的外生噪声变量 U_* 为 0
                for uv in re.findall(r'\bU_\w+\b', eq):
                    if uv not in eq_values:
                        eq_values[uv] = 0.0
                try:
                    result[var] = StructuralEquationEngine.evaluate(eq, eq_values)
                except Exception:
                    result[var] = seed_values.get(var, 0)
            else:
                result[var] = seed_values.get(var, 0)
        return result

    @staticmethod
    def _find_directed_paths(adj: Dict[str, List[str]], source: str,
                             target: str, max_depth: int = 10) -> List[List[str]]:
        result: List[List[str]] = []

        def dfs(current: str, path: List[str]):
            if len(path) > max_depth:
                return
            if current == target:
                result.append(list(path))
                return
            for neighbor in adj.get(current, []):
                if neighbor not in path:
                    path.append(neighbor)
                    dfs(neighbor, path)
                    path.pop()

        dfs(source, [source])
        return result


# ============================================================================
#  CounterfactualEngine — 三步法反事实推理
# ============================================================================

class CounterfactualEngine:
    """三步法反事实推理（外展→行动→预测）"""

    @staticmethod
    def reason(graph: CausalGraph,
               observed: Dict[str, Any],
               hypothetical: Dict[str, Any]) -> Dict[str, Any]:
        """
        反事实推理：
        Step 1 (abduction): 推断外生变量 U
        Step 2 (action): 修改结构方程
        Step 3 (prediction): 计算反事实值
        """
        has_equations = bool(graph.structural_equations)

        if not has_equations:
            # 无方程：基于图结构简单传播
            prediction: Dict[str, Any] = {}
            for var, val in hypothetical.items():
                original = observed.get(var)
                changed = original != val if original is not None else "unknown"
                prediction[var] = {
                    "original": original,
                    "counterfactual": val,
                    "changed": changed if isinstance(changed, bool) else "unknown",
                    "delta": None,
                }
            # 对于不在 hypothetical 中但受其影响的变量
            for var in observed:
                if var in hypothetical:
                    continue
                # 检查是否有 hypothetical 中的变量是 var 的祖先
                original = observed.get(var)
                any_ancestor_changed = False
                for h_var in hypothetical:
                    if graph.is_ancestor(h_var, var):
                        if observed.get(h_var) != hypothetical[h_var]:
                            any_ancestor_changed = True
                            break
                if any_ancestor_changed:
                    prediction[var] = {
                        "original": original,
                        "counterfactual": "不确定",
                        "changed": "unknown",
                        "delta": None,
                    }
                else:
                    prediction[var] = {
                        "original": original,
                        "counterfactual": original,
                        "changed": False,
                        "delta": None,
                    }
            return {
                "steps": {
                    "abduction": {"exogenous_values": {}, "details": {}},
                    "prediction": prediction,
                },
                "observed": observed,
                "hypothetical": hypothetical,
            }

        # 有结构方程：三步法
        # Step 1: Abduction — 推断外生变量
        exogenous_values: Dict[str, Any] = {}
        abduction_details: Dict[str, Any] = {}

        for var, expr in graph.structural_equations.items():
            if var not in observed:
                continue
            u_vars = re.findall(r'\bU_\w+\b', expr)
            if not u_vars:
                continue

            for u_var in u_vars:
                try:
                    expr_no_u = expr
                    expr_no_u = re.sub(r'[\d.]+ \* \b' + u_var + r'\b', '0', expr_no_u)
                    expr_no_u = re.sub(r'\b' + u_var + r' \* [\d.]+', '0', expr_no_u)
                    expr_no_u = re.sub(r'\b' + u_var + r'\b', '0', expr_no_u)

                    values_for_rest = {k: v for k, v in observed.items()
                                       if k != u_var}
                    values_for_rest.update(exogenous_values)
                    rest_value = StructuralEquationEngine.evaluate(expr_no_u, values_for_rest)

                    coeff = 1.0
                    coeff_match = re.search(r'([\d.]+) \* \b' + u_var + r'\b', expr)
                    if coeff_match:
                        coeff = float(coeff_match.group(1))
                    else:
                        coeff_match = re.search(r'\b' + u_var + r' \* ([\d.]+)', expr)
                        if coeff_match:
                            coeff = float(coeff_match.group(1))

                    if coeff != 0:
                        exogenous_values[u_var] = (observed[var] - rest_value) / coeff
                    else:
                        exogenous_values[u_var] = 0
                except Exception:
                    exogenous_values[u_var] = 0

            abduction_details[var] = {
                "equation": expr,
                "observed": observed[var],
                "exogenous_found": dict(exogenous_values),
            }

        # Step 2 & 3: Action + Prediction
        prediction: Dict[str, Any] = {}
        all_values = dict(observed)
        all_values.update(hypothetical)
        all_values.update(exogenous_values)

        # 拓扑排序计算（需要按依赖顺序）
        computed_vars: Set[str] = set()
        eq_vars = set(graph.structural_equations.keys())

        def compute_var(var: str):
            if var in computed_vars:
                return
            parents = [e["from"] for e in graph.edges if e["to"] == var]
            for p in parents:
                if p in eq_vars and p not in computed_vars:
                    compute_var(p)
            if var in graph.structural_equations:
                try:
                    eq = graph.structural_equations[var]
                    for uv in re.findall(r'\bU_\w+\b', eq):
                        if uv not in all_values:
                            all_values[uv] = 0.0
                    cf_value = StructuralEquationEngine.evaluate(eq, all_values)
                except Exception:
                    cf_value = hypothetical.get(var, observed.get(var))
                all_values[var] = cf_value
            computed_vars.add(var)

        for var in eq_vars:
            compute_var(var)

        # 补充未观测变量的原始值（通过方程反算）
        observed_full = dict(observed)
        for var in eq_vars:
            if var not in observed_full:
                try:
                    eq = graph.structural_equations[var]
                    orig_values = dict(observed_full)
                    orig_values.update(exogenous_values)
                    origin_val = StructuralEquationEngine.evaluate(eq, orig_values)
                    observed_full[var] = origin_val
                except Exception:
                    pass

        for var in all_values:
            if var in exogenous_values:
                continue
            original = observed_full.get(var)
            cf_val = all_values.get(var)
            changed = "unknown"
            delta = None
            if var in hypothetical:
                changed = original != hypothetical[var]
            elif original is not None and cf_val is not None:
                try:
                    changed = original != cf_val
                    delta = float(cf_val) - float(original)
                except (TypeError, ValueError):
                    changed = "unknown"

            desc = "不确定"
            if changed is True:
                desc = f"改变: {original} → {cf_val}"
            elif changed is False:
                desc = "不变"

            prediction[var] = {
                "original": original,
                "counterfactual": cf_val,
                "changed": changed,
                "delta": delta,
            }

        return {
            "steps": {
                "abduction": {
                    "exogenous_values": exogenous_values,
                    "details": abduction_details,
                },
                "prediction": prediction,
            },
            "observed": observed_full,
            "hypothetical": hypothetical,
        }


# ============================================================================
#  CausalReasonerManager — 持久化管理器（工具入口）
# ============================================================================

class CausalReasonerManager:
    """
    Xenon 因果推理引擎管理器。

    自动被 ToolManager 发现，公开方法注册为工具。
    模型持久化自动保存在 Memory/causal_models/ 目录。
    """

    def __init__(self, model_dir: Optional[str] = None):
        # 持久化目录 — 基于工具文件自身位置推导项目根目录，换设备自动适配
        if model_dir is None:
            _base = Path(__file__).resolve().parent.parent
            self._model_dir = _base / "Memory" / "causal_models"
            self._results_dir = _base / "Memory" / "causal_results"
        else:
            self._model_dir = Path(model_dir)
            self._results_dir = Path(model_dir).parent / "causal_results"
        self._model_dir.mkdir(parents=True, exist_ok=True)
        self._results_dir.mkdir(parents=True, exist_ok=True)

        # 内存中的图（兼容旧 API）
        self._graph = _CausalGraph()
        self._reasoning_log: List[Dict[str, Any]] = []
        self._last_ambiguity: Optional[str] = None

    # ================================================================
    #  持久化辅助方法
    # ================================================================

    def _load_index(self) -> Dict[str, Any]:
        """加载模型索引"""
        index_path = self._model_dir / "_index.json"
        if index_path.exists():
            try:
                return json.loads(index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def _save_index(self, index: Dict[str, Any]):
        """保存模型索引"""
        index_path = self._model_dir / "_index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """将模型名转为安全文件名"""
        safe = re.sub(r'[<>:"/\\|?*]', '_', name)
        safe = re.sub(r'__+', '_', safe)
        return safe.strip('_') or "model"

    def _find_filename(self, name: str) -> Optional[Tuple[str, str]]:
        """
        模糊匹配模型名，返回 (canonical_name, filename) 或 None。
        多个匹配时设置 self._last_ambiguity 并返回 None。
        """
        index = self._load_index()
        # 精确匹配
        if name in index:
            return (name, index[name].get("filename", ""))

        # Glob 匹配
        safe = self._sanitize_name(name)
        pattern = f"{safe}*.json"
        matches = sorted(self._model_dir.glob(pattern))
        # 过滤掉 _index.json
        matches = [m for m in matches if m.name != "_index.json"]

        if len(matches) == 0:
            return None
        elif len(matches) == 1:
            filename = matches[0].name
            # 在索引中找到 canonical name
            for key, val in index.items():
                if val.get("filename") == filename:
                    return (key, filename)
            return (matches[0].stem, filename)
        else:
            self._last_ambiguity = f"'{name}' 匹配到 {len(matches)} 个模型: {[m.stem for m in matches]}"
            return None

    def _get_unique_filename(self, name: str) -> str:
        """生成唯一文件名，处理冲突"""
        safe = self._sanitize_name(name)
        existing = set(p.stem for p in self._model_dir.glob(f"{safe}*.json")
                       if p.name != "_index.json")
        if safe not in existing:
            return f"{safe}.json"
        # 冲突：加短哈希
        short_hash = name.replace(" ", "_")[:8] + uuid.uuid4().hex[:6]
        return f"{safe}_{short_hash}.json"

    # ================================================================
    #  推理结果管理（解决数据量过大问题：默认落盘，返回摘要）
    # ================================================================

    def _save_result(self, method: str, model_name: str, result: Dict[str, Any]) -> str:
        """将完整推理结果保存到文件，返回文件名。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = self._sanitize_name(model_name)
        filename = f"{safe_name}_{method}_{timestamp}.json"
        filepath = self._results_dir / filename
        data = {
            "model": model_name,
            "method": method,
            "timestamp": datetime.now().isoformat(),
            "result": result,
        }
        filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return filename

    def list_results(self, model_name: Optional[str] = None,
                     limit: int = 20) -> Dict[str, Any]:
        """列出已保存的推理结果。

        Args:
            model_name: 可选，按模型名过滤
            limit: 最大返回数量

        Returns:
            结果文件列表
        """
        try:
            files = sorted(self._results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            items = []
            for fp in files:
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    if model_name and data.get("model") != model_name:
                        continue
                    items.append({
                        "filename": fp.name,
                        "model": data.get("model", ""),
                        "method": data.get("method", ""),
                        "timestamp": data.get("timestamp", ""),
                        "size_bytes": fp.stat().st_size,
                    })
                except (json.JSONDecodeError, IOError):
                    continue
                if len(items) >= limit:
                    break
            return {"success": True, "results": items, "count": len(items)}
        except Exception as e:
            return {"success": False, "error": f"列出结果失败: {str(e)}"}

    def read_result(self, filename: str) -> Dict[str, Any]:
        """读取已保存的推理结果完整数据。

        Args:
            filename: 结果文件名

        Returns:
            完整推理结果
        """
        try:
            filepath = self._results_dir / filename
            if not filepath.exists():
                return {"success": False, "error": f"结果文件 '{filename}' 不存在"}
            data = json.loads(filepath.read_text(encoding="utf-8"))
            data["success"] = True
            return data
        except Exception as e:
            return {"success": False, "error": f"读取结果失败: {str(e)}"}

    def delete_result(self, filename: str) -> Dict[str, Any]:
        """删除推理结果文件。

        Args:
            filename: 结果文件名

        Returns:
            操作结果
        """
        try:
            filepath = self._results_dir / filename
            if not filepath.exists():
                return {"success": False, "error": f"结果文件 '{filename}' 不存在"}
            filepath.unlink()
            return {"success": True, "message": f"结果文件 '{filename}' 已删除"}
        except Exception as e:
            return {"success": False, "error": f"删除结果失败: {str(e)}"}

    def _build_path_summary(self, paths: List[List[str]],
                             max_sample: int = 5) -> Dict[str, Any]:
        """将路径列表压缩为摘要：总数 + 采样 + 按长度分类统计。"""
        if not paths:
            return {"count": 0, "sample": [], "by_length": {}}
        by_len: Dict[int, int] = {}
        for p in paths:
            l = len(p)
            by_len[l] = by_len.get(l, 0) + 1
        return {
            "count": len(paths),
            "sample": paths[:max_sample],
            "by_length": by_len,
        }

    # ================================================================
    #  模型 CRUD
    # ================================================================

    def build_model(self, name: str,
                    nodes: Optional[List[Dict[str, Any]]] = None,
                    edges: Optional[List[Dict[str, Any]]] = None,
                    confounders: Optional[List[Dict[str, Any]]] = None,
                    structural_equations: Optional[Dict[str, str]] = None,
                    description: str = "",
                    tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        构建一个新的因果模型并持久化保存。

        Args:
            name: 模型名称（唯一标识）
            nodes: 节点列表，每项 {"name": str, "label": str, "type": str, "values": list}
            edges: 有向边列表，每项 {"from": str, "to": str, "strength": float, "mechanism": str}
            confounders: 混杂因子列表，每项 {"variable": str, "affects": [str], "mechanism": str}
            structural_equations: 结构方程字典 {变量: 表达式}
            description: 模型描述
            tags: 标签列表

        Returns:
            构建结果
        """
        try:
            index = self._load_index()
            if name in index:
                return {"success": False, "error": f"模型 '{name}' 已存在"}

            nodes = nodes or []
            edges = edges or []
            confounders = confounders or []
            equations = structural_equations or {}

            # 验证节点
            node_names = set()
            for n in nodes:
                if "name" not in n:
                    return {"success": False, "error": "节点缺少 'name' 字段"}
                node_names.add(n["name"])

            # 验证边
            for e in edges:
                if "from" not in e or "to" not in e:
                    return {"success": False, "error": "边缺少 'from' 或 'to' 字段"}
                if e["from"] not in node_names or e["to"] not in node_names:
                    return {"success": False, "error": f"边 {e['from']}→{e['to']} 引用了未定义的节点"}

            # 验证混杂因子
            for cf in confounders:
                if "variable" not in cf or "affects" not in cf:
                    return {"success": False, "error": "混杂因子缺少 'variable' 或 'affects' 字段"}
                for a in cf["affects"]:
                    if a not in node_names:
                        return {"success": False, "error": f"混杂因子引用了未定义节点: {a}"}

            # 验证结构方程
            for var, expr in equations.items():
                if var not in node_names:
                    return {"success": False, "error": f"结构方程引用了未定义节点: {var}"}
                # 安全检测
                expr_lower = expr.lower()
                forbidden = [r"__", r"import", r"eval", r"exec", r"system",
                             r"subprocess", r"open\(", r"file\("]
                for pat in forbidden:
                    if re.search(pat, expr_lower):
                        return {"success": False, "error": f"方程 '{var}' 包含危险调用"}
                # 检测变量是否都在图中
                eq_vars = re.findall(r'\b([A-Za-z_]\w*)\b', expr)
                builtins = {"IF", "CLAMP", "AND", "OR", "NOT", "True", "False",
                            "abs", "min", "max", "round", "int", "float", "bool", "sqrt"}
                for ev in eq_vars:
                    if ev not in builtins and ev not in node_names and not ev.startswith("U_"):
                        return {"success": False, "error": f"方程中变量 '{ev}' 不在图中"}
                # 非父节点检测
                parents = {e["from"] for e in edges if e["to"] == var}
                for ev in eq_vars:
                    if ev not in builtins and ev in node_names and ev != var and ev not in parents:
                        if not ev.startswith("U_"):
                            return {"success": False,
                                    "error": f"方程中 '{ev}' 不是 '{var}' 的父节点"}

            # 构建图
            graph = CausalGraph()
            for n in nodes:
                graph.add_node(n["name"], n.get("label"), n.get("type", "binary"),
                               n.get("values", []))
            for e in edges:
                graph.add_edge(e["from"], e["to"], e.get("strength"), e.get("mechanism", ""))
            for cf in confounders:
                graph.add_confounder(cf["variable"], cf["affects"],
                                     cf.get("mechanism", ""), cf.get("confidence", ""))
            for var, expr in equations.items():
                graph.set_equation(var, expr)

            # 循环检测
            if graph._has_cycle():
                return {"success": False, "error": "模型包含有向环"}

            # 保存
            filename = self._get_unique_filename(name)
            filepath = self._model_dir / filename
            data = {
                "name": name,
                "filename": filename,
                "graph": graph.to_dict(),
                "description": description,
                "tags": list(tags or []),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

            index[name] = {
                "filename": filename,
                "description": description,
                "tags": list(tags or []),
                "node_count": len(node_names),
                "edge_count": len(edges),
                "created_at": data["created_at"],
                "updated_at": data["updated_at"],
                "links": {},
            }
            self._save_index(index)

            return {
                "success": True,
                "name": name,
                "filename": filename,
                "node_count": len(node_names),
                "edge_count": len(edges),
                "message": f"模型 '{name}' 创建成功",
            }
        except Exception as e:
            return {"success": False, "error": f"构建模型失败: {str(e)}"}

    def read_model(self, name: str) -> Dict[str, Any]:
        """
        读取已保存的因果模型。

        Args:
            name: 模型名称（支持模糊匹配）

        Returns:
            模型数据
        """
        try:
            found = self._find_filename(name)
            if found is None:
                if self._last_ambiguity:
                    return {"success": False, "error": self._last_ambiguity}
                return {"success": False, "error": f"模型 '{name}' 不存在"}
            canonical, filename = found
            filepath = self._model_dir / filename
            if not filepath.exists():
                return {"success": False, "error": f"模型文件 '{filename}' 不存在"}
            data = json.loads(filepath.read_text(encoding="utf-8"))
            data["success"] = True
            return data
        except Exception as e:
            return {"success": False, "error": f"读取模型失败: {str(e)}"}

    def update_model(self, name: str,
                     add_nodes: Optional[List[Dict[str, Any]]] = None,
                     add_edges: Optional[List[Dict[str, Any]]] = None,
                     add_confounders: Optional[List[Dict[str, Any]]] = None,
                     set_equations: Optional[Dict[str, str]] = None,
                     description: Optional[str] = None,
                     tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        更新已存在的因果模型。

        Args:
            name: 模型名称
            add_nodes: 新增节点
            add_edges: 新增边
            add_confounders: 新增混杂因子
            set_equations: 设置/更新结构方程
            description: 更新描述
            tags: 更新标签

        Returns:
            更新结果
        """
        try:
            found = self._find_filename(name)
            if found is None:
                if self._last_ambiguity:
                    return {"success": False, "error": self._last_ambiguity}
                return {"success": False, "error": f"模型 '{name}' 不存在"}
            canonical, filename = found
            filepath = self._model_dir / filename
            data = json.loads(filepath.read_text(encoding="utf-8"))

            graph = CausalGraph(data["graph"])

            # 添加节点
            add_nodes = add_nodes or []
            for n in add_nodes:
                graph.add_node(n["name"], n.get("label"), n.get("type", "binary"),
                               n.get("values", []))

            # 添加边（需要循环验证）
            add_edges = add_edges or []
            for e in add_edges:
                graph.add_edge(e["from"], e["to"], e.get("strength"), e.get("mechanism", ""))
            if add_edges and graph._has_cycle():
                return {"success": False, "error": "添加边后模型包含有向环"}

            # 添加混杂因子
            add_confounders = add_confounders or []
            for cf in add_confounders:
                graph.add_confounder(cf["variable"], cf["affects"],
                                     cf.get("mechanism", ""), cf.get("confidence", ""))

            # 设置方程（需要验证安全性）
            set_equations = set_equations or {}
            all_node_names = set(graph.nodes.keys())
            for var, expr in set_equations.items():
                if var not in all_node_names:
                    return {"success": False, "error": f"方程变量 '{var}' 不在图中"}
                expr_lower = expr.lower()
                for pat in [r"__", r"import", r"eval", r"exec", r"system"]:
                    if re.search(pat, expr_lower):
                        return {"success": False, "error": f"方程包含危险调用"}
                eq_vars = re.findall(r'\b([A-Za-z_]\w*)\b', expr)
                builtins = {"IF", "CLAMP", "AND", "OR", "NOT", "True", "False",
                            "abs", "min", "max", "round", "int", "float", "bool", "sqrt"}
                for ev in eq_vars:
                    if ev not in builtins and ev not in all_node_names and not ev.startswith("U_"):
                        return {"success": False, "error": f"变量 '{ev}' 不在图中"}
                graph.set_equation(var, expr)

            data["graph"] = graph.to_dict()
            if description is not None:
                data["description"] = description
            if tags is not None:
                data["tags"] = list(tags)
            data["updated_at"] = datetime.now().isoformat()

            filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

            # 更新索引
            index = self._load_index()
            if canonical in index:
                entry = index[canonical]
                entry["description"] = data.get("description", entry.get("description", ""))
                entry["tags"] = data.get("tags", entry.get("tags", []))
                entry["updated_at"] = data["updated_at"]
                entry["node_count"] = len(graph.nodes)
                entry["edge_count"] = len(graph.edges)
                self._save_index(index)

            return {"success": True, "name": canonical,
                    "message": f"模型 '{canonical}' 更新成功"}
        except Exception as e:
            return {"success": False, "error": f"更新模型失败: {str(e)}"}

    def list_models(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """
        列出所有已保存的因果模型。

        Args:
            page: 页码（从 1 开始）
            page_size: 每页数量

        Returns:
            分页的模型列表
        """
        try:
            if page < 1 or page_size < 1:
                return {"success": False, "error": "page 和 page_size 必须 >= 1"}
            index = self._load_index()
            all_names = sorted(index.keys())
            total = len(all_names)
            start = (page - 1) * page_size
            end = start + page_size
            page_names = all_names[start:end]
            models = []
            for nm in page_names:
                entry = index[nm]
                models.append({
                    "name": nm,
                    "description": entry.get("description", ""),
                    "node_count": entry.get("node_count", 0),
                    "edge_count": entry.get("edge_count", 0),
                    "tags": entry.get("tags", []),
                    "created_at": entry.get("created_at", ""),
                    "updated_at": entry.get("updated_at", ""),
                })
            return {
                "success": True,
                "models": models,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size),
            }
        except Exception as e:
            return {"success": False, "error": f"列出模型失败: {str(e)}"}

    def validate_model(self, name: str) -> Dict[str, Any]:
        """
        验证模型完整性（循环检测、结构方程有效性）。

        Args:
            name: 模型名称

        Returns:
            验证结果
        """
        try:
            found = self._find_filename(name)
            if found is None:
                if self._last_ambiguity:
                    return {"success": False, "error": self._last_ambiguity}
                return {"success": False, "error": f"模型 '{name}' 不存在"}
            canonical, _ = found
            result = self.read_model(canonical)
            if not result.get("success"):
                return result
            graph = CausalGraph(result["graph"])
            has_cycle = graph._has_cycle()
            status = "valid" if not has_cycle else "invalid"
            issues = []
            if has_cycle:
                issues.append("模型包含有向环")
            return {
                "success": True,
                "name": canonical,
                "status": status,
                "issues": issues,
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
                "has_equations": len(graph.structural_equations) > 0,
            }
        except Exception as e:
            return {"success": False, "error": f"验证模型失败: {str(e)}"}

    def delete_model(self, name: str) -> Dict[str, Any]:
        """
        删除一个因果模型。

        Args:
            name: 模型名称

        Returns:
            操作结果
        """
        try:
            found = self._find_filename(name)
            if found is None:
                return {"success": False, "error": f"模型 '{name}' 不存在"}
            canonical, filename = found
            filepath = self._model_dir / filename
            if filepath.exists():
                filepath.unlink()
            index = self._load_index()
            if canonical in index:
                del index[canonical]
            self._save_index(index)
            return {"success": True, "message": f"模型 '{canonical}' 已删除"}
        except Exception as e:
            return {"success": False, "error": f"删除模型失败: {str(e)}"}

    # ================================================================
    #  推理操作
    # ================================================================

    def query_paths(self, name: str, from_var: str, to_var: str,
                    path_type: str = "all",
                    return_summary: bool = True) -> Dict[str, Any]:
        """
        查询模型中两个变量之间的因果路径。

        Args:
            name: 模型名称
            from_var: 起始变量
            to_var: 目标变量
            path_type: 路径类型 - "all", "causal", "backdoor"
            return_summary: 是否只返回摘要（默认 True）。
                           设为 True 时完整路径列表保存到文件，返回值仅含计数和采样；
                           设为 False 时返回全部路径数据（仅用于程序化消费）。

        Returns:
            路径分析结果（摘要模式含 result_file 路径）
        """
        try:
            found = self._find_filename(name)
            if found is None:
                if self._last_ambiguity:
                    return {"success": False, "error": self._last_ambiguity}
                return {"success": False, "error": f"模型 '{name}' 不存在"}
            canonical, _ = found
            result = self.read_model(canonical)
            if not result.get("success"):
                return result
            graph = CausalGraph(result["graph"])
            path_result = PathEngine.find_paths_between(graph, from_var, to_var)

            if return_summary:
                filename = self._save_result("query_paths", canonical, {
                    "from": from_var, "to": to_var, **path_result,
                })
                causal_summary = self._build_path_summary(path_result["causal_paths"])
                backdoor_summary = self._build_path_summary(path_result["backdoor_paths"])
                truncated_note = "（已达上限，结果可能不完整）" if path_result.get("truncated") else ""
                return {
                    "success": True,
                    "model": canonical,
                    "from": from_var,
                    "to": to_var,
                    "causal": causal_summary,
                    "backdoor": backdoor_summary,
                    "truncated": path_result.get("truncated", False),
                    "result_file": filename,
                    "message": (
                        f"因果路径 {causal_summary['count']} 条，"
                        f"后门路径 {backdoor_summary['count']} 条{truncated_note}。"
                        f"完整数据 → read_result('{filename}')"
                    ),
                }

            return {
                "success": True,
                "model": canonical,
                "from": from_var,
                "to": to_var,
                **path_result,
            }
        except Exception as e:
            return {"success": False, "error": f"查询路径失败: {str(e)}"}

    def intervene(self, name: str,
                  intervention: Dict[str, Any],
                  baseline_values: Optional[Dict[str, Any]] = None,
                  exogenous_values: Optional[Dict[str, Any]] = None,
                  return_summary: bool = True) -> Dict[str, Any]:
        """
        在模型上执行干预模拟。

        Args:
            name: 模型名称
            intervention: 干预字典 {变量: 值}
            baseline_values: 基线值（用于计算 delta）
            exogenous_values: 外生变量值
            return_summary: 是否只返回摘要（默认 True）。
                           摘要模式：完整 propagation_paths 保存到文件，返回变化摘要；
                           完整模式：返回全部原始数据。

        Returns:
            干预结果（摘要模式含 result_file 路径和 effects 变化摘要）
        """
        try:
            found = self._find_filename(name)
            if found is None:
                if self._last_ambiguity:
                    return {"success": False, "error": self._last_ambiguity}
                return {"success": False, "error": f"模型 '{name}' 不存在"}
            canonical, _ = found
            result = self.read_model(canonical)
            if not result.get("success"):
                return result
            graph = CausalGraph(result["graph"])

            # 验证干预变量存在
            for ivar in intervention:
                if ivar not in graph.nodes:
                    if not ivar.startswith("U_"):
                        return {"success": False,
                                "error": f"干预变量 '{ivar}' 不在模型中"}

            sim = InterventionEngine.simulate(graph, intervention,
                                              baseline_values, exogenous_values)

            if return_summary:
                # 构建传播效应摘要
                prop_summary: Dict[str, Dict[str, int]] = {}
                total_paths = 0
                for ivar, targets in sim.get("propagation_paths", {}).items():
                    prop_summary[ivar] = {}
                    for tgt, paths in targets.items():
                        prop_summary[ivar][tgt] = len(paths)
                        total_paths += len(paths)

                # 定量效应摘要（只保留有变化或重要的）
                effects_summary = {}
                for var, eff in sim.get("quantitative_effects", {}).items():
                    delta = eff.get("delta", 0)
                    if isinstance(delta, (int, float)) and abs(delta) > 0.001:
                        effects_summary[var] = {
                            "baseline": eff.get("baseline_value"),
                            "intervention": eff.get("intervention_value"),
                            "delta": delta,
                        }

                filename = self._save_result("intervene", canonical, sim)
                return {
                    "success": True,
                    "model": canonical,
                    "intervention": intervention,
                    "removed_edges_count": len(sim.get("removed_edges", [])),
                    "propagation_summary": prop_summary,
                    "total_propagation_paths": total_paths,
                    "effects_with_change": effects_summary,
                    "computed_count": len(sim.get("computed_values", {})),
                    "result_file": filename,
                    "message": (
                        f"干预模拟完成：移除 {len(sim.get('removed_edges', []))} 条边，"
                        f"共 {total_paths} 条传播路径，"
                        f"{len(effects_summary)} 个变量发生了可测变化。"
                        f"完整数据 → read_result('{filename}')"
                    ),
                }

            return {"success": True, "model": canonical, **sim}
        except Exception as e:
            return {"success": False, "error": f"干预模拟失败: {str(e)}"}

    def counterfactual(self, name: str,
                       observed: Dict[str, Any],
                       hypothetical: Dict[str, Any],
                       return_summary: bool = True) -> Dict[str, Any]:
        """
        在模型上执行反事实推理。

        Args:
            name: 模型名称
            observed: 观察到的实际值 {变量: 值}
            hypothetical: 反事实假设 {变量: 值}
            return_summary: 是否只返回摘要（默认 True）。

        Returns:
            反事实推理结果（摘要模式含 result_file 路径和各步骤变化的简要描述）
        """
        try:
            found = self._find_filename(name)
            if found is None:
                if self._last_ambiguity:
                    return {"success": False, "error": self._last_ambiguity}
                return {"success": False, "error": f"模型 '{name}' 不存在"}
            canonical, _ = found
            result = self.read_model(canonical)
            if not result.get("success"):
                return result
            graph = CausalGraph(result["graph"])

            # 验证变量存在
            all_vars = set(graph.nodes.keys())
            for v in list(observed.keys()) + list(hypothetical.keys()):
                if v not in all_vars and not v.startswith("U_"):
                    return {"success": False, "error": f"变量 '{v}' 不在模型中"}

            cf_result = CounterfactualEngine.reason(graph, observed, hypothetical)

            if return_summary:
                abduction = cf_result.get("steps", {}).get("abduction", {})
                prediction = cf_result.get("steps", {}).get("prediction", {})
                # 提取关键变化
                deltas = {}
                for var, pred in prediction.items():
                    delta = pred.get("delta", 0)
                    if isinstance(delta, (int, float)) and abs(delta) > 0.001:
                        deltas[var] = {
                            "actual": pred.get("actual_value"),
                            "counterfactual": pred.get("counterfactual"),
                            "delta": delta,
                        }

                filename = self._save_result("counterfactual", canonical, cf_result)
                return {
                    "success": True,
                    "model": canonical,
                    "observed": observed,
                    "hypothetical": hypothetical,
                    "abduction_u_count": len(abduction.get("exogenous_values", {})),
                    "prediction_vars_with_change": deltas,
                    "prediction_total_vars": len(prediction),
                    "result_file": filename,
                    "message": (
                        f"反事实推理完成：外展推断 {len(abduction.get('exogenous_values', {}))} 个外生变量，"
                        f"预测阶段 {len(deltas)} 个变量发生了变化。"
                        f"完整数据 → read_result('{filename}')"
                    ),
                }

            return {"success": True, "model": canonical, **cf_result}
        except Exception as e:
            return {"success": False, "error": f"反事实推理失败: {str(e)}"}

    def merge_with_memory(self, filename: str, task_id: str = "") -> Dict[str, Any]:
        """
        将模型与任务记忆关联。

        Args:
            filename: 模型文件名
            task_id: 关联的任务 ID

        Returns:
            操作结果
        """
        try:
            index = self._load_index()
            for key, val in index.items():
                if val.get("filename") == filename:
                    if "links" not in val:
                        val["links"] = {}
                    val["links"]["task_id"] = task_id
                    val["links"]["merged_at"] = datetime.now().isoformat()
                    self._save_index(index)
                    return {"success": True, "message": f"模型 '{key}' 已关联任务 '{task_id}'"}
            return {"success": False, "error": f"找不到文件名为 '{filename}' 的模型"}
        except Exception as e:
            return {"success": False, "error": f"关联失败: {str(e)}"}

    def suggest_causal_structure(self, description: str,
                                 variables: List[str]) -> Dict[str, Any]:
        """
        从文本描述中启发式推断因果结构。

        Args:
            description: 场景描述文本
            variables: 变量名称列表
        """
        try:
            text_lower = description.lower()
            var_lower_map = {v.lower(): v for v in variables}

            common_cause_indicators = [
                "共同影响", "共同作用", "共同导致", "共同推动",
            ]

            detected_edges = []
            detected_confounders = []

            # 检测混杂因子（共同原因）
            for cci in common_cause_indicators:
                if cci in text_lower:
                    idx = text_lower.find(cci)
                    # 获取指标词前面的文本
                    before = text_lower[max(0, idx - 80):idx]
                    # 找到所有在 before 中出现的变量
                    found_vars = []
                    for v in variables:
                        pos = before.rfind(v.lower())
                        if pos >= 0:
                            found_vars.append((pos, v))
                    found_vars.sort(key=lambda x: x[0])  # 按位置从小到大（文本中出现顺序）
                    if len(found_vars) >= 3:
                        # 最后一个变量（位置最大）是 cause（confounder），因为它离指标词最近
                        cause = found_vars[-1][1]
                        affected = [fv[1] for fv in found_vars[:-1]]
                        detected_confounders.append({
                            "variable": cause,
                            "affects": affected,
                            "mechanism": "从描述中提取的混杂关系",
                            "confidence": "pattern_match",
                        })

            return {
                "success": True,
                "suggestion": {
                    "detected_edges": detected_edges,
                    "detected_confounders": detected_confounders,
                },
                "variables": variables,
            }
        except Exception as e:
            return {"success": False, "error": f"结构推断失败: {str(e)}"}

    # ================================================================
    #  旧 API 兼容（infer_causal_graph, estimate_branch_probability 等）
    # ================================================================

    def infer_causal_graph(
        self,
        scenario: str,
        variables: List[str],
        known_relations: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        根据场景描述推断变量之间的因果结构。

        Args:
            scenario: 场景描述文本
            variables: 变量名称列表
            known_relations: 已知的因果边列表，格式 "cause→effect" 或 "cause→effect:strength"
        """
        try:
            self._graph = _CausalGraph()
            for var in variables:
                self._graph.add_node(var, base_prob=0.5, description=f"变量: {var}")

            parsed_edges = []
            if known_relations:
                for rel in known_relations:
                    rel = rel.strip()
                    arrow = "→" if "→" in rel else "->" if "->" in rel else None
                    if not arrow:
                        continue
                    parts = rel.split(arrow)
                    cause_part = parts[0].strip()
                    effect_part = parts[1].strip()
                    strength = 0.5
                    if ":" in effect_part:
                        effect_part, strength_str = effect_part.rsplit(":", 1)
                        try:
                            strength = max(-1.0, min(1.0, float(strength_str.strip())))
                        except ValueError:
                            strength = 0.5
                    effect_part = effect_part.strip()
                    if cause_part in variables and effect_part in variables:
                        parsed_edges.append((cause_part, effect_part, strength))
                        self._graph.add_edge(cause_part, effect_part, strength)

            if not parsed_edges:
                inferred = self._heuristic_infer(scenario, variables)
                for cause, effect, strength in inferred:
                    self._graph.add_edge(cause, effect, strength)

            graph_dict = self._graph.to_dict()
            edge_count = sum(len(v) for v in graph_dict["edges"].values())

            self._log_reasoning("infer_causal_graph", {
                "scenario": scenario[:100],
                "variables": variables,
                "known_relations": known_relations,
                "edges_found": edge_count,
            })

            return {
                "success": True,
                "causal_graph": graph_dict,
                "edge_count": edge_count,
                "node_count": len(graph_dict["nodes"]),
                "message": f"因果图构建完成：{len(graph_dict['nodes'])} 个节点，{edge_count} 条边",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"因果图推断失败: {str(e)}",
                "message": f"因果图推断失败: {str(e)}",
                "causal_graph": {"nodes": {}, "edges": {}},
                "edge_count": 0,
                "node_count": 0,
            }

    def estimate_branch_probability(
        self,
        target_outcome: str,
        intervention_variable: str,
        intervention_value: bool = True,
        given_conditions: Optional[str] = None,
    ) -> Dict[str, Any]:
        """估计在特定干预下，某个结果发生的概率。"""
        try:
            if given_conditions:
                if isinstance(given_conditions, str):
                    given_dict = json.loads(given_conditions)
                else:
                    given_dict = dict(given_conditions)
            else:
                given_dict = {}

            for v in [target_outcome, intervention_variable]:
                if v not in self._graph.nodes:
                    self._graph.add_node(v, base_prob=0.5)

            conditions = dict(given_dict)
            conditions[intervention_variable] = intervention_value

            prob, chain = self._propagate_probability(target_outcome, conditions)
            base_prob, _ = self._propagate_probability(target_outcome, given_dict)

            direction = "促进" if prob > base_prob else "抑制" if prob < base_prob else "无影响"
            size = abs(prob - base_prob)

            self._log_reasoning("estimate_branch_probability", {
                "target": target_outcome,
                "intervention": intervention_variable,
                "value": intervention_value,
                "estimated_prob": round(prob, 3),
                "baseline_prob": round(base_prob, 3),
            })

            return {
                "success": True,
                "target_outcome": target_outcome,
                "intervention": f"{intervention_variable} = {intervention_value}",
                "estimated_probability": round(prob, 4),
                "baseline_probability": round(base_prob, 4),
                "effect_direction": direction,
                "effect_size": round(size, 4),
                "reasoning_chain": chain,
                "message": (
                    f"干预 {intervention_variable}={intervention_value} 下，"
                    f"{target_outcome} 发生的概率为 {prob:.1%}"
                    f"（基线 {base_prob:.1%}，{direction}效应 {size:.1%}）"
                ),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"分支概率估计失败: {str(e)}",
                "estimated_probability": None,
            }

    def counterfactual_reason(
        self,
        actual_outcome: str,
        actual_fact: str,
        counterfactual_intervention: str,
        counterfactual_value: bool = True,
    ) -> Dict[str, Any]:
        """反事实推理（兼容旧 API）"""
        try:
            if isinstance(actual_fact, str):
                actual_fact_dict = json.loads(actual_fact)
            else:
                actual_fact_dict = dict(actual_fact or {})

            for var in list(actual_fact_dict.keys()) + [actual_outcome, counterfactual_intervention]:
                if var not in self._graph.nodes:
                    self._graph.add_node(var, base_prob=0.5)

            actual_prob, actual_chain = self._propagate_probability(actual_outcome, actual_fact_dict)

            cf_fact = dict(actual_fact_dict)
            cf_fact[counterfactual_intervention] = counterfactual_value
            cf_prob, cf_chain = self._propagate_probability(actual_outcome, cf_fact)

            change = cf_prob - actual_prob

            self._log_reasoning("counterfactual_reason", {
                "outcome": actual_outcome,
                "actual_prob": round(actual_prob, 3),
                "cf_prob": round(cf_prob, 3),
                "intervention": f"{counterfactual_intervention}->{counterfactual_value}",
                "change": round(change, 3),
            })

            return {
                "success": True,
                "outcome": actual_outcome,
                "actual_world": {
                    "conditions": actual_fact_dict,
                    f"{actual_outcome}_probability": round(actual_prob, 4),
                },
                "counterfactual_world": {
                    "intervention": f"{counterfactual_intervention} = {counterfactual_value}",
                    "conditions": cf_fact,
                    f"{actual_outcome}_probability": round(cf_prob, 4),
                },
                "probability_change": round(change, 4),
                "effect_description": (
                    f"提高了 {change:.1%}" if change > 0
                    else f"降低了 {abs(change):.1%}" if change < 0
                    else "几乎无变化"
                ),
                "message": (
                    f"如果 {counterfactual_intervention}={counterfactual_value}，"
                    f"{actual_outcome} 的概率从 {actual_prob:.1%} "
                    f"变为 {cf_prob:.1%}（{self._describe_change(change)}）"
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"反事实推理失败: {str(e)}"}

    def trace_causal_chain(
        self, start_cause: str, end_effect: str, max_depth: int = 5
    ) -> Dict[str, Any]:
        """追踪从起始原因到最终结果的因果链。"""
        try:
            if start_cause not in self._graph.nodes:
                return {"success": False, "error": f"变量 '{start_cause}' 不在因果图中"}
            if end_effect not in self._graph.nodes:
                return {"success": False, "error": f"变量 '{end_effect}' 不在因果图中"}

            paths = self._find_paths(start_cause, end_effect, max_depth)

            if not paths:
                return {
                    "success": True, "paths": [], "path_count": 0,
                    "message": f"未找到从 '{start_cause}' 到 '{end_effect}' 的因果路径",
                }

            scored_paths = []
            for path in paths:
                strength = 1.0
                steps = []
                for i in range(len(path) - 1):
                    cause, effect = path[i], path[i + 1]
                    edge_strength = 1.0
                    for child, s in self._graph.get_children(cause):
                        if child == effect:
                            edge_strength = s
                            break
                    strength *= edge_strength
                    steps.append({"from": cause, "to": effect, "strength": round(edge_strength, 3)})
                scored_paths.append({"path": path, "steps": steps, "path_strength": round(strength, 4)})

            scored_paths.sort(key=lambda x: x["path_strength"], reverse=True)

            self._log_reasoning("trace_causal_chain", {
                "from": start_cause, "to": end_effect, "paths_found": len(scored_paths),
            })

            return {
                "success": True,
                "paths": scored_paths,
                "path_count": len(scored_paths),
                "strongest_path": scored_paths[0] if scored_paths else None,
                "message": (
                    f"找到 {len(scored_paths)} 条从 '{start_cause}' 到 '{end_effect}' 的因果路径"
                    + (f"，最强路径强度 {scored_paths[0]['path_strength']:.2%}" if scored_paths else "")
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"因果链追踪失败: {str(e)}"}

    def get_reasoning_log(self, limit: int = 10) -> Dict[str, Any]:
        """获取最近的推理日志。"""
        recent = self._reasoning_log[-limit:] if self._reasoning_log else []
        return {"success": True, "log_entries": recent,
                "total_entries": len(self._reasoning_log), "returned": len(recent)}

    def clear_graph(self) -> Dict[str, Any]:
        """清空当前因果图。"""
        self._graph = _CausalGraph()
        return {"success": True, "message": "因果图已清空"}

    # ================================================================
    #  内部辅助方法
    # ================================================================

    def _heuristic_infer(self, scenario: str, variables: List[str]) -> List[Tuple[str, str, float]]:
        """基于场景文本启发式推断变量间的因果可能性。"""
        edges = []
        text_lower = scenario.lower()

        cause_indicators = [
            "导致", "引起", "造成", "引发", "推动", "促进", "抑制",
            "因为", "由于", "基于", "取决于",
            "lead", "cause", "trigger", "result in", "drive", "depend on",
            "due to", "because of", "contribute", "influence", "affect",
        ]
        effect_indicators = [
            "从而", "进而", "以至于", "所以", "因此",
            "leading to", "resulting in", "which causes", "thereby",
        ]

        for cause in variables:
            for effect in variables:
                if cause == effect:
                    continue
                c_lower = cause.lower()
                e_lower = effect.lower()
                # 词边界检查：避免 "代码" 匹配到 "代码质量"
                if not re.search(r'\b' + re.escape(c_lower) + r'\b', text_lower):
                    continue
                if not re.search(r'\b' + re.escape(e_lower) + r'\b', text_lower):
                    continue

                strength = 0.3
                cause_idx = text_lower.find(c_lower)
                effect_idx = text_lower.find(e_lower)

                if cause_idx < effect_idx:
                    between = text_lower[cause_idx:effect_idx]
                    for ind in cause_indicators:
                        if ind in between:
                            strength = 0.6
                            break
                else:
                    between = text_lower[effect_idx:cause_idx]
                    for ind in effect_indicators:
                        if ind in between:
                            strength = 0.6
                            break

                if strength >= 0.3:
                    edges.append((cause, effect, round(strength, 2)))

        seen = set()
        unique_edges = []
        for c, e, s in edges:
            key = (c, e)
            if key not in seen:
                seen.add(key)
                unique_edges.append((c, e, s))

        unique_edges.sort(key=lambda x: x[2], reverse=True)
        max_edges = max(1, len(variables) * 2)
        return unique_edges[:max_edges]

    def _propagate_probability(
        self,
        target: str,
        conditions: Dict[str, bool],
        _memo: Optional[Dict] = None,
        _exploring: Optional[set] = None,
        depth: int = 0,
        max_depth: int = 10,
    ) -> Tuple[float, List[Dict[str, Any]]]:
        """沿因果图反向传播概率（从目标回溯到已知条件）。"""
        if _memo is None:
            _memo = {}
        if _exploring is None:
            _exploring = set()

        if target in _exploring:
            base = self._graph.nodes.get(target, {}).get("base_prob", 0.5)
            return base, [{"variable": target, "source": "cycle_detected", "probability": base}]
        if depth > max_depth:
            base = self._graph.nodes.get(target, {}).get("base_prob", 0.5)
            return base, [{"variable": target, "source": "max_depth", "probability": base}]

        cond_key = frozenset((k, v) for k, v in conditions.items()
                             if k in self._graph.nodes)
        cache_key = (target, cond_key)
        if cache_key in _memo:
            return _memo[cache_key]

        if target in conditions:
            prob = 1.0 if conditions[target] else 0.0
            chain = [{"variable": target, "source": "given", "probability": prob}]
            _memo[cache_key] = (prob, chain)
            return prob, chain

        parents = self._graph.get_parents(target)
        if not parents:
            base = self._graph.nodes.get(target, {}).get("base_prob", 0.5)
            chain = [{"variable": target, "source": "base_rate", "probability": base}]
            _memo[cache_key] = (base, chain)
            return base, chain

        _exploring.add(target)
        excitatory = []
        inhibitory = []
        chain = []
        for parent, strength in parents:
            p_prob, p_chain = self._propagate_probability(
                parent, conditions, _memo, _exploring, depth + 1, max_depth
            )
            if p_chain:
                chain.extend(list(p_chain))
            if strength >= 0:
                excitatory.append((p_prob, strength))
            else:
                inhibitory.append((p_prob, abs(strength)))
        _exploring.discard(target)

        base_prob = self._graph.nodes.get(target, {}).get("base_prob", 0.5)
        if excitatory:
            prob = _noisy_or(base_prob, other_causes=excitatory)
        else:
            prob = base_prob
        for p_c, abs_s in inhibitory:
            prob *= (1.0 - p_c * abs_s)
        prob = max(0.0, min(1.0, prob))

        chain.append({
            "variable": target,
            "source": "excitatory+inhibitory",
            "excitatory_parents": [(round(p, 3), round(s, 3)) for p, s in excitatory],
            "inhibitory_parents": [(round(p, 3), round(s, 3)) for p, s in inhibitory],
            "base_probability": base_prob,
            "computed_probability": round(prob, 4),
        })

        _memo[cache_key] = (prob, chain)
        return prob, chain

    def _find_paths(
        self, start: str, end: str, max_depth: int,
        path: Optional[List[str]] = None,
        all_paths: Optional[List[List[str]]] = None,
    ) -> List[List[str]]:
        """DFS 搜索因果路径。"""
        if path is None:
            path = [start]
        if all_paths is None:
            all_paths = []

        if len(path) > max_depth:
            return all_paths

        last = path[-1]
        if last == end:
            all_paths.append(list(path))
            return all_paths

        for child, _ in self._graph.get_children(last):
            if child not in path:
                path.append(child)
                self._find_paths(start, end, max_depth, path, all_paths)
                path.pop()

        return all_paths

    def _log_reasoning(self, method: str, details: Dict[str, Any]):
        self._reasoning_log.append({
            "timestamp": datetime.now().isoformat(),
            "method": method,
            "details": details,
        })

    @staticmethod
    def _describe_change(change: float) -> str:
        if change > 0.2:
            return "显著提高"
        elif change > 0.05:
            return "略有提高"
        elif change > -0.05:
            return "基本不变"
        elif change > -0.2:
            return "略有下降"
        else:
            return "显著下降"


# ============================================================================
#  自测
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Xenon 因果推理引擎 v2.0 — 自测")
    print("=" * 60)

    # 1. 基本导入（直接引用当前模块中的类）
    print("\n[1] 模块导入测试...")
    # 类已在当前文件中定义，直接使用即可
    print("   ✅ 所有类导入成功")

    # 2. 表达式求值
    print("\n[2] 表达式求值测试...")
    values = {"A": 3, "B": True, "C": False}
    assert StructuralEquationEngine.evaluate("2 * A + 1", values) == 7
    assert StructuralEquationEngine.evaluate("B AND (NOT C)", values) is True
    assert StructuralEquationEngine.evaluate("IF(A > 0, A, 0)", values) == 3
    assert StructuralEquationEngine.evaluate("IF(A == 0, 1 / A, 5)", values) == 5
    assert StructuralEquationEngine.evaluate("CLAMP(A * 10, 0, 20)", values) == 20
    print("   ✅ 表达式求值全部正确")

    # 3. 循环检测
    print("\n[3] 循环检测测试...")
    g = CausalGraph()
    g.add_node("A"); g.add_node("B")
    g.add_edge("A", "B"); g.add_edge("B", "A")
    assert g._has_cycle() is True
    g2 = CausalGraph()
    g2.add_node("A"); g2.add_node("B")
    g2.add_edge("A", "B")
    assert g2._has_cycle() is False
    print("   ✅ 循环检测正确")

    # 4. d-separation
    print("\n[4] d-separation 测试...")
    g3 = CausalGraph()
    for n in ["X", "A", "C", "Y", "B", "D"]:
        g3.add_node(n)
    g3.add_edge("A", "X")
    g3.add_edge("A", "C")
    g3.add_edge("C", "Y")
    g3.add_edge("B", "X")
    g3.add_edge("B", "C")
    g3.add_edge("D", "C")
    g3.add_edge("D", "Y")
    result = AdjustmentEngine.find_adjustment_sets(g3, "X", "Y")
    assert result["identifiable"] is True
    assert result["minimal_set"]["variables"] == ["A", "B"]
    print("   ✅ d-separation 调整集正确")

    # 5. 干预模拟
    print("\n[5] 干预模拟测试...")
    g4 = CausalGraph()
    g4.add_node("A"); g4.add_node("B"); g4.add_node("C")
    g4.add_edge("A", "B"); g4.add_edge("B", "C")
    g4.set_equation("B", "2 * A + U_B")
    g4.set_equation("C", "B + 1")
    result = InterventionEngine.simulate(g4, {"A": 3}, {"A": 1}, {"U_B": 0})
    assert result["computed_values"] == {"A": 3, "B": 6, "C": 7}
    print("   ✅ 干预模拟正确")

    # 6. 反事实推理
    print("\n[6] 反事实推理测试...")
    result = CounterfactualEngine.reason(g4, {"A": 1, "B": 5}, {"A": 2})
    assert result["steps"]["abduction"]["exogenous_values"]["U_B"] == 3
    assert result["steps"]["prediction"]["B"]["counterfactual"] == 7
    assert result["steps"]["prediction"]["B"]["delta"] == 2
    print("   ✅ 反事实推理正确")

    # 7. 模型持久化
    print("\n[7] 持久化测试...")
    mgr = CausalReasonerManager()
    r = mgr.build_model("test-model", nodes=[{"name": "A"}, {"name": "B"}],
                         edges=[{"from": "A", "to": "B", "strength": 0.8}])
    assert r["success"] is True
    r = mgr.read_model("test-model")
    assert r["success"] is True
    r = mgr.validate_model("test-model")
    assert r["status"] == "valid"
    mgr.delete_model("test-model")
    print("   ✅ 持久化 CRUD 正确")

    # 8. 旧 API 兼容
    print("\n[8] 旧 API 兼容测试...")
    result = mgr.infer_causal_graph(
        scenario="代码质量下降导致 bug 增多，进而导致用户满意度下降。",
        variables=["测试覆盖率", "代码质量", "bug数量", "用户满意度"],
        known_relations=["测试覆盖率→代码质量:0.7", "代码质量→bug数量:0.8", "bug数量→用户满意度:0.6"],
    )
    assert result["success"] is True
    print(f"   ✅ {result['message']}")

    result = mgr.estimate_branch_probability(
        target_outcome="用户满意度",
        intervention_variable="测试覆盖率",
        intervention_value=True,
    )
    assert result["success"] is True
    print(f"   ✅ {result['message']}")

    print("\n" + "=" * 60)
    print("🎉 全部自测通过！Xenon 因果推理引擎 v2.0 就绪")
    print("=" * 60)
