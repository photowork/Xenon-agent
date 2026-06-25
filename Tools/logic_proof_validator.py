#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Xenon 逻辑证明与推演验证工具

基于 sympy.logic 引擎构建，提供：
- 命题逻辑：真值表、重言式/矛盾/可满足性检查、逻辑等价判断
- 自然演绎：前提→结论的推理验证、步进式证明校验
- 正规形式：CNF / DNF / NNF 转换、逻辑化简
- 知识库推理：基于命题知识库的前向/后向推理
- 推演规则库：列出并应用常见推理规则

工具清单:
    parse_expression / truth_table
    check_tautology / check_contradiction / check_satisfiability
    logical_equivalence / deduction_check
    to_cnf / to_dnf / to_nnf / simplify
    verify_proof_step / verify_proof
    list_inference_rules / apply_rule
    kb_tell / kb_ask / kb_retract / kb_status
"""

from __future__ import annotations

import itertools
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# ── 后端加载：优先使用 sympy ─────────────────────────────────────

_SYMPY_AVAILABLE = False
_HAS_SYMPY_LOGIC = False

try:
    import sympy
    from sympy import symbols as _sympy_symbols
    from sympy.logic.boolalg import (
        And, Or, Not, Implies, Equivalent, Xor, Nand, Nor,
        simplify_logic, truth_table, to_cnf, to_dnf, to_nnf,
        BooleanTrue, BooleanFalse,
    )
    from sympy.logic.inference import satisfiable, valid, PropKB
    _SYMPY_AVAILABLE = True
    _HAS_SYMPY_LOGIC = True
except ImportError:
    pass


# ── 逻辑连接词映射 ───────────────────────────────────────────

_SYMBOL_TO_SYMPY = {
    "&": "&", "∧": "&", "and": "&", "AND": "&",
    "|": "|", "∨": "|", "or": "|", "OR": "|",
    "~": "~", "¬": "~", "not": "~", "NOT": "~",
    ">>": ">>", "→": ">>", "⇒": ">>",
    "implies": ">>", "IMPLIES": ">>",
    "<->": "<->", "↔": "<->", "⇔": "<->",
    "equivalent": "<->", "EQUIVALENT": "<->",
    "^": "^", "⊕": "^", "xor": "^", "XOR": "^",
}

_INFERENCE_RULES = {
    "modus_ponens": {
        "name": "Modus Ponens (肯定前件)",
        "symbolic": "P → Q, P ⊢ Q",
        "description": "如果 P 蕴含 Q 且 P 为真，则 Q 为真。",
        "premise_count": 2,
        "validate": lambda premises: _check_mp(premises),
    },
    "modus_tollens": {
        "name": "Modus Tollens (否定后件)",
        "symbolic": "P → Q, ¬Q ⊢ ¬P",
        "description": "如果 P 蕴含 Q 且 Q 为假，则 P 为假。",
        "premise_count": 2,
        "validate": lambda premises: _check_mt(premises),
    },
    "hypothetical_syllogism": {
        "name": "Hypothetical Syllogism (假言三段论)",
        "symbolic": "P → Q, Q → R ⊢ P → R",
        "description": "如果 P 蕴含 Q 且 Q 蕴含 R，则 P 蕴含 R。",
        "premise_count": 2,
        "validate": lambda premises: _check_hs(premises),
    },
    "disjunctive_syllogism": {
        "name": "Disjunctive Syllogism (选言三段论)",
        "symbolic": "P ∨ Q, ¬P ⊢ Q",
        "description": "如果 P 或 Q 为真，且 P 为假，则 Q 为真。",
        "premise_count": 2,
        "validate": lambda premises: _check_ds(premises),
    },
    "conjunction_intro": {
        "name": "Conjunction Introduction (合取引入)",
        "symbolic": "P, Q ⊢ P ∧ Q",
        "description": "如果 P 和 Q 各自为真，则 P 且 Q 为真。",
        "premise_count": 2,
        "validate": lambda premises: _check_ci(premises),
    },
    "conjunction_elim": {
        "name": "Conjunction Elimination (合取消除)",
        "symbolic": "P ∧ Q ⊢ P",
        "description": "如果 P 且 Q 为真，则 P 为真。",
        "premise_count": 1,
        "validate": lambda premises: _check_ce(premises),
    },
    "disjunction_intro": {
        "name": "Disjunction Introduction (析取引入)",
        "symbolic": "P ⊢ P ∨ Q",
        "description": "如果 P 为真，则 P 或 Q 为真。",
        "premise_count": 1,
        "validate": lambda premises: _check_di(premises),
    },
    "resolution": {
        "name": "Resolution (归结原理)",
        "symbolic": "P ∨ Q, ¬P ∨ R ⊢ Q ∨ R",
        "description": "如果 P 或 Q，且 ¬P 或 R，则 Q 或 R。",
        "premise_count": 2,
        "validate": lambda premises: _check_resolution(premises),
    },
    "double_negation": {
        "name": "Double Negation (双重否定)",
        "symbolic": "¬¬P ⊢ P",
        "description": "双重否定等于肯定。",
        "premise_count": 1,
        "validate": lambda premises: _check_dn(premises),
    },
    "contraposition": {
        "name": "Contraposition (换质换位)",
        "symbolic": "P → Q ⊢ ¬Q → ¬P",
        "description": "P 蕴含 Q 等价于 ¬Q 蕴含 ¬P。",
        "premise_count": 1,
        "validate": lambda premises: _check_contra(premises),
    },
}


# ── 规则验证辅助函数 ─────────────────────────────────────

_Token = Tuple[str, str]


def _tokenize_logic(expr_str: str) -> List[_Token]:
    """Tokenize a propositional-logic expression without using eval."""
    tokens: List[_Token] = []
    i = 0
    length = len(expr_str)

    multi_char_ops = {
        "<->": "EQUIV",
        "<=>": "EQUIV",
        "->": "IMPLIES",
        "=>": "IMPLIES",
        ">>": "IMPLIES",
        "&&": "AND",
        "||": "OR",
    }
    single_char_ops = {
        "(": "LPAREN",
        ")": "RPAREN",
        "&": "AND",
        "∧": "AND",
        "|": "OR",
        "∨": "OR",
        "~": "NOT",
        "¬": "NOT",
        "!": "NOT",
        "^": "XOR",
        "⊕": "XOR",
        "→": "IMPLIES",
        "⇒": "IMPLIES",
        "↔": "EQUIV",
        "⇔": "EQUIV",
        "⊤": "TRUE",
        "⊥": "FALSE",
    }
    word_ops = {
        "and": "AND",
        "or": "OR",
        "not": "NOT",
        "xor": "XOR",
        "implies": "IMPLIES",
        "equivalent": "EQUIV",
        "true": "TRUE",
        "false": "FALSE",
    }

    while i < length:
        ch = expr_str[i]
        if ch.isspace():
            i += 1
            continue

        matched = False
        for op, kind in multi_char_ops.items():
            if expr_str.startswith(op, i):
                tokens.append((kind, op))
                i += len(op)
                matched = True
                break
        if matched:
            continue

        if ch in single_char_ops:
            tokens.append((single_char_ops[ch], ch))
            i += 1
            continue

        ident_match = re.match(r"[A-Za-z_]\w*", expr_str[i:])
        if ident_match:
            word = ident_match.group(0)
            kind = word_ops.get(word.lower())
            tokens.append((kind, word) if kind else ("IDENT", word))
            i += len(word)
            continue

        raise ValueError(f"无法识别字符 {ch!r} (位置 {i})")

    return tokens


class _LogicParser:
    """Recursive-descent parser for the small proposition language."""

    def __init__(self, tokens: List[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> Any:
        if not self.tokens:
            raise ValueError("表达式为空")
        expr = self._parse_equiv()
        if self._peek() is not None:
            raise ValueError(f"意外的标记 {self._peek()[1]!r}")
        return expr

    def _peek(self) -> Optional[_Token]:
        if self.pos >= len(self.tokens):
            return None
        return self.tokens[self.pos]

    def _match(self, kind: str) -> Optional[_Token]:
        token = self._peek()
        if token and token[0] == kind:
            self.pos += 1
            return token
        return None

    def _parse_equiv(self) -> Any:
        expr = self._parse_implies()
        while self._match("EQUIV"):
            expr = Equivalent(expr, self._parse_implies())
        return expr

    def _parse_implies(self) -> Any:
        left = self._parse_xor()
        if self._match("IMPLIES"):
            return Implies(left, self._parse_implies())
        return left

    def _parse_xor(self) -> Any:
        expr = self._parse_or()
        while self._match("XOR"):
            expr = Xor(expr, self._parse_or())
        return expr

    def _parse_or(self) -> Any:
        expr = self._parse_and()
        while self._match("OR"):
            expr = Or(expr, self._parse_and())
        return expr

    def _parse_and(self) -> Any:
        expr = self._parse_not()
        while self._match("AND"):
            expr = And(expr, self._parse_not())
        return expr

    def _parse_not(self) -> Any:
        if self._match("NOT"):
            return Not(self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> Any:
        token = self._peek()
        if token is None:
            raise ValueError("表达式不完整")

        kind, value = token
        if kind == "LPAREN":
            self.pos += 1
            expr = self._parse_equiv()
            if not self._match("RPAREN"):
                raise ValueError("缺少右括号")
            return expr
        if kind == "IDENT":
            self.pos += 1
            return _sympy_symbols(value)
        if kind == "TRUE":
            self.pos += 1
            return BooleanTrue()
        if kind == "FALSE":
            self.pos += 1
            return BooleanFalse()

        raise ValueError(f"意外的标记 {value!r}")


def _parse_to_sympy(expr_str: str) -> Any:
    """将逻辑表达式字符串解析为 sympy 表达式。"""
    if not _HAS_SYMPY_LOGIC:
        raise RuntimeError("sympy.logic 不可用，无法解析表达式")

    return _LogicParser(_tokenize_logic(expr_str)).parse()


def _extract_symbols(expr_str: str) -> List[str]:
    """从表达式字符串中提取命题符号名。"""
    try:
        return sorted({value for kind, value in _tokenize_logic(expr_str) if kind == "IDENT"})
    except ValueError:
        keywords = {'and', 'or', 'not', 'implies', 'equivalent', 'xor',
                    'AND', 'OR', 'NOT', 'IMPLIES', 'EQUIVALENT', 'XOR',
                    'true', 'false', 'True', 'False', '⊤', '⊥'}
        return sorted(set(
            m.group(1) for m in re.finditer(r'\b([A-Za-z_]\w*)\b', expr_str)
            if m.group(1) not in keywords
        ))


def _check_mp(premises: List[Any]) -> Dict[str, Any]:
    """Modus Ponens: P→Q, P ⊢ Q"""
    if len(premises) < 2:
        return {"valid": False, "error": "需要 2 个前提"}
    for i, j in itertools.permutations(range(len(premises)), 2):
        p_i, p_j = premises[i], premises[j]
        if isinstance(p_i, Implies):
            ante, cons = p_i.args
            if _sympy_equal(p_j, ante):
                return {"valid": True, "conclusion": str(cons)}
    return {"valid": False, "error": "无法匹配 Modus Ponens 模式"}


def _check_mt(premises: List[Any]) -> Dict[str, Any]:
    """Modus Tollens: P→Q, ¬Q ⊢ ¬P"""
    if len(premises) < 2:
        return {"valid": False, "error": "需要 2 个前提"}
    for i, j in itertools.permutations(range(len(premises)), 2):
        p_i, p_j = premises[i], premises[j]
        if isinstance(p_i, Implies):
            ante, cons = p_i.args
            if isinstance(p_j, Not) and _sympy_equal(p_j.args[0], cons):
                return {"valid": True, "conclusion": str(Not(ante))}
    return {"valid": False, "error": "无法匹配 Modus Tollens 模式"}


def _check_hs(premises: List[Any]) -> Dict[str, Any]:
    """Hypothetical Syllogism: P→Q, Q→R ⊢ P→R"""
    if len(premises) < 2:
        return {"valid": False, "error": "需要 2 个前提"}
    implications = [p for p in premises if isinstance(p, Implies)]
    if len(implications) >= 2:
        for i, j in itertools.permutations(range(len(implications)), 2):
            a1, c1 = implications[i].args
            a2, c2 = implications[j].args
            if _sympy_equal(c1, a2):
                return {"valid": True, "conclusion": str(Implies(a1, c2))}
    return {"valid": False, "error": "无法匹配假言三段论模式"}


def _check_ds(premises: List[Any]) -> Dict[str, Any]:
    """Disjunctive Syllogism: P∨Q, ¬P ⊢ Q"""
    if len(premises) < 2:
        return {"valid": False, "error": "需要 2 个前提"}
    for i, j in itertools.permutations(range(len(premises)), 2):
        p_i, p_j = premises[i], premises[j]
        if isinstance(p_i, Or):
            disjuncts = p_i.args
            if isinstance(p_j, Not):
                negated = p_j.args[0]
                remaining = [d for d in disjuncts if not _sympy_equal(d, negated)]
                if len(remaining) < len(disjuncts):
                    conc = remaining[0] if len(remaining) == 1 else Or(*remaining)
                    return {"valid": True, "conclusion": str(conc)}
    return {"valid": False, "error": "无法匹配选言三段论模式"}


def _check_ci(premises: List[Any]) -> Dict[str, Any]:
    """Conjunction Intro: P, Q ⊢ P∧Q"""
    if len(premises) < 2:
        return {"valid": False, "error": "需要 2 个前提"}
    return {"valid": True, "conclusion": str(And(premises[0], premises[1]))}


def _check_ce(premises: List[Any]) -> Dict[str, Any]:
    """Conjunction Elim: P∧Q ⊢ P"""
    if len(premises) < 1:
        return {"valid": False, "error": "需要 1 个前提"}
    p = premises[0]
    if isinstance(p, And):
        alternatives = [str(arg) for arg in p.args]
        return {"valid": True, "conclusion": alternatives[0],
                "alternatives": alternatives}
    return {"valid": False, "error": "前提不是合取式"}


def _check_di(premises: List[Any]) -> Dict[str, Any]:
    """Disjunction Intro: P ⊢ P∨Q"""
    if len(premises) < 1:
        return {"valid": False, "error": "需要 1 个前提"}
    return {"valid": True, "conclusion": str(Or(premises[0], _sympy_symbols("_Q_")))}


def _check_resolution(premises: List[Any]) -> Dict[str, Any]:
    """Resolution: P∨Q, ¬P∨R ⊢ Q∨R"""
    if len(premises) < 2:
        return {"valid": False, "error": "需要 2 个前提"}
    for i, j in itertools.permutations(range(len(premises)), 2):
        p_i, p_j = premises[i], premises[j]
        if isinstance(p_i, Or) and isinstance(p_j, Or):
            for di in p_i.args:
                for dj in p_j.args:
                    if _are_complements(di, dj):
                        # 匹配到了一个互补对
                        remaining_i = [x for x in p_i.args if not _sympy_equal(x, di)]
                        remaining_j = [x for x in p_j.args if not _sympy_equal(x, dj)]
                        all_remaining = remaining_i + remaining_j
                        if all_remaining:
                            conc = all_remaining[0] if len(all_remaining) == 1 else Or(*all_remaining)
                            return {"valid": True, "conclusion": str(conc)}
                        return {"valid": True, "conclusion": "⊥ (空子句)"}
    return {"valid": False, "error": "无法匹配归结模式"}


def _check_dn(premises: List[Any]) -> Dict[str, Any]:
    """Double Negation: ¬¬P ⊢ P"""
    if len(premises) < 1:
        return {"valid": False, "error": "需要 1 个前提"}
    p = premises[0]
    if isinstance(p, Not) and isinstance(p.args[0], Not):
        return {"valid": True, "conclusion": str(p.args[0].args[0])}
    return {"valid": False, "error": "前提不是双重否定形式"}


def _check_contra(premises: List[Any]) -> Dict[str, Any]:
    """Contraposition: P→Q ⊢ ¬Q→¬P"""
    if len(premises) < 1:
        return {"valid": False, "error": "需要 1 个前提"}
    p = premises[0]
    if isinstance(p, Implies):
        ante, cons = p.args
        return {"valid": True, "conclusion": str(Implies(Not(cons), Not(ante)))}
    return {"valid": False, "error": "前提不是蕴含式"}


def _sympy_equal(a: Any, b: Any) -> bool:
    """比较两个 sympy 表达式的逻辑等价性。"""
    try:
        if str(a) == str(b):
            return True
        equals_result = a.equals(b)
        if equals_result is True:
            return True
        return bool(valid(Equivalent(a, b)))
    except Exception:
        return str(a) == str(b)


def _are_complements(a: Any, b: Any) -> bool:
    """Return True when one literal is the negation of the other."""
    if isinstance(a, Not):
        return _sympy_equal(a.args[0], b)
    if isinstance(b, Not):
        return _sympy_equal(a, b.args[0])
    return False


def _safe_parse(expr_str: str) -> Dict[str, Any]:
    """安全解析表达式，返回结构化的解析结果。"""
    if not _HAS_SYMPY_LOGIC:
        return {"success": False, "error": "sympy.logic 不可用，请安装 sympy"}
    try:
        expr = _parse_to_sympy(expr_str)
        symbols_found = _extract_symbols(expr_str)
        return {
            "success": True,
            "expression": expr,
            "expression_str": str(expr),
            "symbols": symbols_found,
        }
    except Exception as e:
        return {"success": False, "error": f"解析失败: {str(e)}"}


def _expr_from_str(expr_str: str) -> Any:
    """解析并返回 sympy 表达式，失败时抛出异常。"""
    result = _safe_parse(expr_str)
    if not result["success"]:
        raise ValueError(result["error"])
    return result["expression"]


# ============================================================
#  全局 KB 实例（供跨会话使用）
# ============================================================

_global_kb = PropKB() if _HAS_SYMPY_LOGIC else None


# ============================================================
#  工具管理器类
# ============================================================

class LogicProofToolManager:
    """逻辑证明与推演验证工具管理器

    自动发现机制将本类的公开方法注册为 Xenon 工具。
    提供命题逻辑分析、推演验证、正规形式转换等多种能力。
    """

    # ── 表达式解析 ────────────────────────────────────────

    def parse_expression(self, expr_str: str) -> Dict[str, Any]:
        """解析逻辑表达式字符串，返回其结构和符号列表。

        :param expr_str: 逻辑表达式，如 'P & Q'、'P → Q'、'(P or Q) and not R'
        :return: 解析结果，包含 expression_str、symbols、type 等
        """
        return _safe_parse(expr_str)

    def list_symbols(self, expr_str: str) -> Dict[str, Any]:
        """提取表达式中的所有命题符号。

        :param expr_str: 逻辑表达式字符串
        :return: 符号列表
        """
        symbols = _extract_symbols(expr_str)
        return {"symbols": symbols, "count": len(symbols)}

    # ── 语义分析 ────────────────────────────────────────

    def truth_table(self, expr_str: str) -> Dict[str, Any]:
        """生成逻辑表达式的完整真值表。

        :param expr_str: 逻辑表达式字符串
        :return: 包含真值表行、符号列标的字典
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}

        parsed = _safe_parse(expr_str)
        if not parsed["success"]:
            return parsed

        expr = parsed["expression"]
        symbols = [_sympy_symbols(s) for s in parsed["symbols"]]

        try:
            raw_table = list(truth_table(expr, symbols))
            header = [str(s) for s in symbols] + ["result"]
            rows = []
            for assignment, result in raw_table:
                row = {str(s): bool(v) for s, v in zip(symbols, assignment)}
                row["result"] = bool(result)
                rows.append(row)

            # 统计
            true_count = sum(1 for r in rows if r["result"])
            false_count = len(rows) - true_count

            return {
                "success": True,
                "expression": expr_str,
                "symbols": [str(s) for s in symbols],
                "header": header,
                "rows": rows,
                "row_count": len(rows),
                "true_count": true_count,
                "false_count": false_count,
                "is_tautology": true_count == len(rows),
                "is_contradiction": false_count == len(rows),
                "is_contingency": true_count > 0 and false_count > 0,
            }
        except Exception as e:
            return {"success": False, "error": f"真值表生成失败: {str(e)}"}

    def check_tautology(self, expr_str: str) -> Dict[str, Any]:
        """检查逻辑表达式是否为重言式（永真式）。

        :param expr_str: 逻辑表达式字符串
        :return: 包含 is_tautology 布尔值的字典
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expr_str)
            is_valid = valid(expr)
            return {
                "success": True,
                "expression": expr_str,
                "is_tautology": bool(is_valid),
                "type": "tautology" if is_valid else "non_tautology",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def check_contradiction(self, expr_str: str) -> Dict[str, Any]:
        """检查逻辑表达式是否为矛盾式（永假式）。

        :param expr_str: 逻辑表达式字符串
        :return: 包含 is_contradiction 布尔值的字典
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expr_str)
            is_valid = valid(Not(expr))
            return {
                "success": True,
                "expression": expr_str,
                "is_contradiction": bool(is_valid),
                "type": "contradiction" if is_valid else "non_contradiction",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def check_satisfiability(self, expr_str: str) -> Dict[str, Any]:
        """检查逻辑表达式是否可满足，并给出一个满足赋值（如果有）。

        :param expr_str: 逻辑表达式字符串
        :return: 可满足性结果和示例赋值
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expr_str)
            result = satisfiable(expr)
            if result is False:
                return {
                    "success": True,
                    "expression": expr_str,
                    "is_satisfiable": False,
                    "type": "contradiction",
                }
            else:
                assignment = {str(k): bool(v) for k, v in result.items()}
                return {
                    "success": True,
                    "expression": expr_str,
                    "is_satisfiable": True,
                    "type": "satisfiable",
                    "example_assignment": assignment,
                }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def logical_equivalence(self, expr_str_1: str, expr_str_2: str) -> Dict[str, Any]:
        """检查两个逻辑表达式是否逻辑等价。

        :param expr_str_1: 第一个逻辑表达式
        :param expr_str_2: 第二个逻辑表达式
        :return: 等价性判断结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr1 = _expr_from_str(expr_str_1)
            expr2 = _expr_from_str(expr_str_2)
            equiv = Equivalent(expr1, expr2)
            is_equiv = bool(valid(equiv))

            # 双向蕴含检查
            lr = bool(valid(Implies(expr1, expr2)))
            rl = bool(valid(Implies(expr2, expr1)))

            return {
                "success": True,
                "expression_1": expr_str_1,
                "expression_2": expr_str_2,
                "are_equivalent": is_equiv,
                "left_implies_right": lr,
                "right_implies_left": rl,
                "relationship": (
                    "equivalent" if is_equiv else
                    "left_implies_right" if lr and not rl else
                    "right_implies_left" if rl and not lr else
                    "independent"
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── 推演验证 ────────────────────────────────────────

    def deduction_check(self, premises: List[str], conclusion: str) -> Dict[str, Any]:
        """检查结论是否可以从前提集合中逻辑推导出来。

        使用真值表枚举法验证：所有前提为真的行中，结论是否也为真。

        :param premises: 前提表达式列表，如 ["P → Q", "P"]
        :param conclusion: 结论表达式，如 "Q"
        :return: 推导验证结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            premise_exprs = [_expr_from_str(p) for p in premises]
            conc_expr = _expr_from_str(conclusion)

            # 构建合取前提
            if not premise_exprs:
                premises_conj = BooleanTrue()
            elif len(premise_exprs) == 1:
                premises_conj = premise_exprs[0]
            else:
                premises_conj = And(*premise_exprs)

            # 所有前提为真时结论也为真，即 (P1 ∧ ... ∧ Pn) → C 为永真式。
            derived = bool(valid(Implies(premises_conj, conc_expr)))

            # 找反例
            counterexample = None
            if not derived:
                # 检查 premises_conj & ~conclusion 的可满足性
                check_expr = And(premises_conj, Not(conc_expr))
                sat_result = satisfiable(check_expr)
                if sat_result and sat_result is not False:
                    counterexample = {str(k): bool(v) for k, v in sat_result.items()}

            return {
                "success": True,
                "premises": premises,
                "conclusion": conclusion,
                "is_valid_deduction": bool(derived),
                "counterexample": counterexample,
                "method": "truth_table_enumeration",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── 正规形式 ────────────────────────────────────────

    def to_cnf(self, expr_str: str) -> Dict[str, Any]:
        """将逻辑表达式转换为合取范式 (CNF)。

        :param expr_str: 逻辑表达式字符串
        :return: CNF 结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expr_str)
            cnf_expr = to_cnf(expr)
            return {
                "success": True,
                "original": expr_str,
                "cnf": str(cnf_expr),
                "cnf_type": type(cnf_expr).__name__,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def to_dnf(self, expr_str: str) -> Dict[str, Any]:
        """将逻辑表达式转换为析取范式 (DNF)。

        :param expr_str: 逻辑表达式字符串
        :return: DNF 结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expr_str)
            dnf_expr = to_dnf(expr)
            return {
                "success": True,
                "original": expr_str,
                "dnf": str(dnf_expr),
                "dnf_type": type(dnf_expr).__name__,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def to_nnf(self, expr_str: str) -> Dict[str, Any]:
        """将逻辑表达式转换为否定范式 (NNF)。

        :param expr_str: 逻辑表达式字符串
        :return: NNF 结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expr_str)
            nnf_expr = to_nnf(expr)
            return {
                "success": True,
                "original": expr_str,
                "nnf": str(nnf_expr),
                "nnf_type": type(nnf_expr).__name__,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def simplify(self, expr_str: str) -> Dict[str, Any]:
        """化简逻辑表达式到最简形式。

        :param expr_str: 逻辑表达式字符串
        :return: 化简结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expr_str)
            simplified = simplify_logic(expr)
            return {
                "success": True,
                "original": expr_str,
                "simplified": str(simplified),
                "simplified_type": type(simplified).__name__,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── 推理规则 ────────────────────────────────────────

    def list_inference_rules(self) -> Dict[str, Any]:
        """列出所有支持的推理规则。

        :return: 推理规则列表
        """
        rules = []
        for key, rule in _INFERENCE_RULES.items():
            rules.append({
                "id": key,
                "name": rule["name"],
                "symbolic": rule["symbolic"],
                "description": rule["description"],
                "premise_count": rule["premise_count"],
            })
        return {
            "success": True,
            "rules": rules,
            "count": len(rules),
        }

    def apply_rule(self, rule_name: str, premises: List[str]) -> Dict[str, Any]:
        """对给定前提应用推理规则，尝试推导出新结论。

        :param rule_name: 规则名称（见 list_inference_rules 的 id 字段）
        :param premises: 前提表达式列表
        :return: 规则应用结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}

        if rule_name not in _INFERENCE_RULES:
            return {
                "success": False,
                "error": f"未知规则 '{rule_name}'",
                "available_rules": list(_INFERENCE_RULES.keys()),
            }

        rule = _INFERENCE_RULES[rule_name]
        try:
            premise_exprs = [_expr_from_str(p) for p in premises]
        except Exception as e:
            return {"success": False, "error": f"前提解析失败: {str(e)}"}

        try:
            result = rule["validate"](premise_exprs)
            if result.get("valid"):
                response = {
                    "success": True,
                    "rule": rule_name,
                    "rule_name": rule["name"],
                    "premises": premises,
                    "conclusion": result["conclusion"],
                    "valid": True,
                }
                if "alternatives" in result:
                    response["alternatives"] = result["alternatives"]
                if "alternative" in result:
                    response["alternative"] = result["alternative"]
                return response
            else:
                return {
                    "success": True,
                    "rule": rule_name,
                    "rule_name": rule["name"],
                    "premises": premises,
                    "valid": False,
                    "error": result.get("error", "无法应用该规则"),
                }
        except Exception as e:
            return {"success": False, "error": f"规则应用失败: {str(e)}"}

    # ── 步进式证明验证 ────────────────────────────────

    def verify_proof_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """验证证明中的一个推理步骤。

        步骤格式：
        {
            "from": [前提索引或表达式列表],
            "rule": "规则名",
            "to": "推导出的表达式"
        }

        :param step: 证明步骤字典
        :return: 步骤验证结果
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}

        step_from = step.get("from", [])
        step_rule = step.get("rule", "")
        step_to = step.get("to", "")

        if not step_rule or not step_to:
            return {"success": False, "error": "步骤缺少 rule 或 to 字段"}

        # 应用规则检查
        if step_rule in _INFERENCE_RULES:
            try:
                result = self.apply_rule(step_rule, step_from)
                if result.get("valid"):
                    expected = result["conclusion"]
                    actual = step_to
                    candidates = [expected]
                    candidates.extend(result.get("alternatives", []))
                    if result.get("alternative"):
                        candidates.append(result["alternative"])

                    # 验证推导出的结论是否匹配
                    match = False
                    for candidate in candidates:
                        try:
                            expected_expr = _expr_from_str(candidate)
                            actual_expr = _expr_from_str(actual)
                            if _sympy_equal(expected_expr, actual_expr):
                                match = True
                                break
                        except Exception:
                            if candidate == actual:
                                match = True
                                break

                    if not match and step_rule == "disjunction_intro":
                        try:
                            premise_exprs = [_expr_from_str(p) for p in step_from]
                            actual_expr = _expr_from_str(actual)
                            match = (
                                len(premise_exprs) == 1
                                and isinstance(actual_expr, Or)
                                and any(_sympy_equal(premise_exprs[0], arg)
                                        for arg in actual_expr.args)
                            )
                        except Exception:
                            match = False

                    if match:
                        return {
                            "success": True,
                            "step_valid": True,
                            "rule": step_rule,
                            "expected": expected,
                            "actual": actual,
                        }
                    else:
                        return {
                            "success": True,
                            "step_valid": False,
                            "rule": step_rule,
                            "error": f"推导结果不匹配: 期望 '{expected}'，得到 '{actual}'",
                            "expected": expected,
                            "actual": actual,
                        }
                else:
                    return {
                        "success": True,
                        "step_valid": False,
                        "rule": step_rule,
                        "error": result.get("error", "无法应用规则"),
                    }
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            # 用通用推演检查
            try:
                result = self.deduction_check(step_from, step_to)
                return {
                    "success": True,
                    "step_valid": result.get("is_valid_deduction", False),
                    "rule": step_rule,
                    "method": "general_deduction_check",
                    "detail": result,
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

    def verify_proof(self, premises: List[str], steps: List[Dict[str, Any]],
                     final_conclusion: Optional[str] = None) -> Dict[str, Any]:
        """验证完整的证明链。

        :param premises: 初始前提列表
        :param steps: 证明步骤列表，每步格式同 verify_proof_step
        :param final_conclusion: 最终结论（可选），如提供则验证最后一步是否推导出它
        :return: 证明验证报告
        """
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}

        derived = list(premises)
        step_results = []

        for i, step in enumerate(steps):
            step_from = step.get("from", [])
            step_rule = step.get("rule", "")

            # 解析引用索引
            resolved_premises = []
            reference_error = None
            for ref in step_from:
                if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
                    idx = int(ref)
                    if 0 <= idx < len(derived):
                        resolved_premises.append(derived[idx])
                    else:
                        reference_error = f"引用越界: 索引 {idx} 超出已推导范围"
                        break
                else:
                    resolved_premises.append(str(ref))

            if reference_error:
                step_results.append({
                    "step": i,
                    "rule": step_rule,
                    "from": step_from,
                    "to": step.get("to", ""),
                    "valid": False,
                    "error": reference_error,
                })
                continue

            result = self.verify_proof_step({
                "from": resolved_premises,
                "rule": step_rule,
                "to": step.get("to", ""),
            })

            step_result = {
                "step": i,
                "rule": step_rule,
                "from": step_from,
                "to": step.get("to", ""),
                "valid": result.get("step_valid", False),
            }
            if not result.get("step_valid", False):
                step_result["error"] = result.get("error", "步骤无效")
            else:
                derived.append(step.get("to", ""))

            step_results.append(step_result)

        # 整体判断
        all_valid = all(sr.get("valid", False) for sr in step_results)
        conclusion_reached = False
        if final_conclusion and derived:
            try:
                final_expr = _expr_from_str(final_conclusion)
                last_expr = _expr_from_str(derived[-1])
                conclusion_reached = bool(valid(Equivalent(final_expr, last_expr)))
            except Exception:
                conclusion_reached = derived[-1] == final_conclusion

        return {
            "success": True,
            "proof_valid": all_valid,
            "steps": step_results,
            "total_steps": len(steps),
            "valid_steps": sum(1 for sr in step_results if sr.get("valid", False)),
            "invalid_steps": sum(1 for sr in step_results if not sr.get("valid", False)),
            "derived_expressions": derived[len(premises):],
            "all_derived": derived,
            "final_conclusion": final_conclusion,
            "conclusion_reached": conclusion_reached if final_conclusion else None,
        }

    # ── 知识库推理 ──────────────────────────────────────

    def kb_tell(self, expression: str) -> Dict[str, Any]:
        """向全局知识库添加一条知识（命题）。

        :param expression: 逻辑表达式
        :return: 操作结果
        """
        global _global_kb
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expression)
            _global_kb.tell(expr)
            return {
                "success": True,
                "operation": "tell",
                "expression": expression,
                "kb_size": len(_global_kb.clauses) if hasattr(_global_kb, 'clauses') else 'N/A',
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def kb_ask(self, expression: str) -> Dict[str, Any]:
        """查询全局知识库是否能推导出某命题。

        :param expression: 要查询的逻辑表达式
        :return: 查询结果
        """
        global _global_kb
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expression)
            result = _global_kb.ask(expr)
            return {
                "success": True,
                "operation": "ask",
                "expression": expression,
                "entailed": bool(result),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def kb_retract(self, expression: str) -> Dict[str, Any]:
        """从全局知识库中撤回一条知识。

        :param expression: 要撤回的逻辑表达式
        :return: 操作结果
        """
        global _global_kb
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            expr = _expr_from_str(expression)
            _global_kb.retract(expr)
            return {
                "success": True,
                "operation": "retract",
                "expression": expression,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def kb_status(self) -> Dict[str, Any]:
        """返回全局知识库的当前状态。

        :return: 知识库状态信息
        """
        global _global_kb
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        try:
            clauses = getattr(_global_kb, 'clauses', [])
            return {
                "success": True,
                "clause_count": len(clauses),
                "clauses": [str(c) for c in clauses],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def kb_reset(self) -> Dict[str, Any]:
        """重置全局知识库（清空所有知识）。

        :return: 操作结果
        """
        global _global_kb
        if not _HAS_SYMPY_LOGIC:
            return {"success": False, "error": "sympy.logic 不可用"}
        _global_kb = PropKB()
        return {"success": True, "operation": "reset", "message": "知识库已清空"}


# ============================================================
#  自检入口
# ============================================================

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 60)
    print("  逻辑证明与推演验证工具 — 自检")
    print("=" * 60)

    if not _HAS_SYMPY_LOGIC:
        print("⚠  sympy.logic 不可用，部分功能受限")
    else:
        print("✓  sympy.logic 引擎可用")

    mgr = LogicProofToolManager()

    # 测试 1: 解析
    print("\n--- 测试 1: 表达式解析 ---")
    r = mgr.parse_expression("P → Q")
    print(f"  P → Q: {r.get('expression_str', 'FAIL')}")

    # 测试 2: 真值表
    print("\n--- 测试 2: 真值表 ---")
    r = mgr.truth_table("P & Q")
    print(f"  行数: {r.get('row_count', 0)}")
    print(f"  重言式: {r.get('is_tautology')}")

    # 测试 3: 重言式检查
    print("\n--- 测试 3: 重言式检查 ---")
    r = mgr.check_tautology("P | ~P")
    print(f"  P ∨ ¬P: {r.get('is_tautology')}")

    # 测试 4: 可满足性
    print("\n--- 测试 4: 可满足性 ---")
    r = mgr.check_satisfiability("P & Q")
    print(f"  可满足: {r.get('is_satisfiable')}, 赋值: {r.get('example_assignment')}")

    # 测试 5: 推演验证
    print("\n--- 测试 5: 推演验证 ---")
    r = mgr.deduction_check(["P → Q", "P"], "Q")
    print(f"  有效推演: {r.get('is_valid_deduction')}")

    # 测试 6: 逻辑等价
    print("\n--- 测试 6: 逻辑等价 ---")
    r = mgr.logical_equivalence("P → Q", "~P | Q")
    print(f"  等价: {r.get('are_equivalent')}")

    # 测试 7: 化简
    print("\n--- 测试 7: 化简 ---")
    r = mgr.simplify("(P & Q) | (P & ~Q)")
    print(f"  化简: {r.get('simplified')}")

    # 测试 8: 推理规则
    print("\n--- 测试 8: 推理规则 ---")
    r = mgr.list_inference_rules()
    print(f"  可用规则: {r.get('count', 0)} 条")

    # 测试 9: 应用规则
    print("\n--- 测试 9: Modus Ponens ---")
    r = mgr.apply_rule("modus_ponens", ["P → Q", "P"])
    print(f"  有效: {r.get('valid')}, 结论: {r.get('conclusion', 'N/A')}")

    # 测试 10: 完整证明验证
    print("\n--- 测试 10: 证明验证 ---")
    r = mgr.verify_proof(
        premises=["P → Q", "P"],
        steps=[{"from": [0, 1], "rule": "modus_ponens", "to": "Q"}],
        final_conclusion="Q"
    )
    print(f"  证明有效: {r.get('proof_valid')}, 结论达成: {r.get('conclusion_reached')}")

    print("\n" + "=" * 60)
    print("  自检完成")
    print("=" * 60)
