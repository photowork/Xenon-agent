#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Xenon 计算引擎 — Computational Engine
======================================
为 Xenon 提供数学计算能力，整合符号计算、数值计算、
高精度计算、统计分析等功能。

引擎模块：
  1. 安全表达式求值 — 自定义 AST 遍历器，不依赖 eval()
  2. 符号计算 — sympy 驱动的符号微分、积分、方程求解、化简、级数展开
  3. 矩阵/线性代数 — numpy + sympy 双引擎矩阵运算
  4. 高精度计算 — mpmath 驱动的任意精度算术（默认 50 位）
  5. 统计分析 — 描述性统计、概率分布、回归分析
  6. 傅里叶分析 — numpy FFT 和频谱分析
  7. 优化求解 — 基于符号梯度的梯度下降
  8. 数论工具 — 质数、因数分解、常用数论函数

设计哲学：
  - 安全第一：表达式求值不使用 eval()，自定义 AST 白名单遍历
  - 结果结构化：计算结果使用适合工具调用的字典返回
  - 精度可控：高精度计算可指定有效位数
  - 渐进式：从简单数值到符号推导，按需升级复杂度
"""

import ast
import json
import math
import operator
import re
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal, getcontext as _decimal_context
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# 第三方依赖
# ---------------------------------------------------------------------------
import numpy as np
import sympy as sp
from sympy import (
    symbols, Symbol, diff, integrate, limit, solve, simplify,
    expand, factor, apart, together, collect, cancel,
    Matrix, oo, pi, E, I, GoldenRatio,
    sin, cos, tan, log, exp, sqrt,
    Rational, Integer, Float,
    series, Sum, Product,
)
import mpmath as mp


# ============================================================================
#  安全符号表达式解析器
# ============================================================================

_SYMPY_FUNCTIONS: Dict[str, Callable] = {
    "sin": sp.sin, "cos": sp.cos, "tan": sp.tan,
    "asin": sp.asin, "acos": sp.acos, "atan": sp.atan,
    "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh,
    "log": sp.log, "ln": sp.log, "exp": sp.exp, "sqrt": sp.sqrt,
    "abs": sp.Abs, "Abs": sp.Abs, "floor": sp.floor, "ceil": sp.ceiling,
    "factorial": sp.factorial, "gamma": sp.gamma, "erf": sp.erf, "erfc": sp.erfc,
    "min": sp.Min, "max": sp.Max,
}

_SYMPY_CONSTANTS: Dict[str, Any] = {
    "pi": sp.pi, "E": sp.E, "e": sp.E, "I": sp.I, "oo": sp.oo, "inf": sp.oo,
}


def _json_safe_numeric(value: Any) -> Any:
    """Convert NumPy and complex numbers into JSON-compatible values."""
    if isinstance(value, np.ndarray):
        return [_json_safe_numeric(item) for item in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_json_safe_numeric(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_numeric(item) for key, item in value.items()}
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, complex):
        if abs(value.imag) <= 1e-15:
            return _json_safe_numeric(float(value.real))
        return {
            "real": _json_safe_numeric(float(value.real)),
            "imag": _json_safe_numeric(float(value.imag)),
        }
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _coerce_int(
    value: Any,
    name: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Convert integer-like input without silently truncating fractional values."""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    if isinstance(value, str):
        if not re.fullmatch(r"[+-]?\d+", value.strip()):
            raise ValueError(f"{name} 必须是整数")
        result = int(value)
    elif isinstance(value, (float, np.floating)):
        if not math.isfinite(float(value)) or not float(value).is_integer():
            raise ValueError(f"{name} 必须是整数")
        result = int(value)
    else:
        result = int(value)
        try:
            if value != result:
                raise ValueError(f"{name} 必须是整数")
        except TypeError:
            raise ValueError(f"{name} 必须是整数")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} 必须至少为 {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} 不能超过 {maximum}")
    return result


def _safe_sympify(expression: Any, local_symbols: Optional[Dict[str, sp.Symbol]] = None):
    """Parse a mathematical expression without SymPy's eval-based sympify."""
    if not isinstance(expression, str):
        if isinstance(expression, bool) or expression is None:
            raise ValueError(f"不支持的常量类型: {type(expression).__name__}")
        if isinstance(expression, int):
            return sp.Integer(expression)
        if isinstance(expression, float):
            return sp.Float(str(expression))
        if isinstance(expression, complex):
            return sp.Float(str(expression.real)) + sp.Float(str(expression.imag)) * sp.I
        raise ValueError(f"不支持的表达式类型: {type(expression).__name__}")

    if not expression.strip():
        raise ValueError("expression 不能为空")
    if len(expression) > 10_000:
        raise ValueError("expression 长度不能超过 10000")
    tree = ast.parse(expression.strip(), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 1_000:
        raise ValueError("表达式 AST 节点不能超过 1000")
    symbols_by_name = dict(local_symbols or {})
    binops: Dict[type, Callable] = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    }
    unary_ops: Dict[type, Callable] = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    def convert(node):
        if isinstance(node, ast.Expression):
            return convert(node.body)
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or value is None or isinstance(value, str):
                raise ValueError(f"不支持的常量类型: {type(value).__name__}")
            if isinstance(value, int):
                return sp.Integer(value)
            if isinstance(value, float):
                return sp.Float(str(value))
            if isinstance(value, complex):
                return sp.Float(str(value.real)) + sp.Float(str(value.imag)) * sp.I
            raise ValueError(f"不支持的常量类型: {type(value).__name__}")
        if isinstance(node, ast.Name):
            if node.id in _SYMPY_CONSTANTS:
                return _SYMPY_CONSTANTS[node.id]
            return symbols_by_name.setdefault(node.id, sp.Symbol(node.id))
        if isinstance(node, ast.BinOp):
            operation = binops.get(type(node.op))
            if operation is None:
                raise ValueError(f"不支持的符号运算: {type(node.op).__name__}")
            left = convert(node.left)
            right = convert(node.right)
            if isinstance(node.op, ast.Pow) and right.is_number and right.is_real:
                if abs(right) > 100_000:
                    raise ValueError("符号幂指数绝对值不能超过 100000")
            return operation(left, right)
        if isinstance(node, ast.UnaryOp):
            operation = unary_ops.get(type(node.op))
            if operation is None:
                raise ValueError(f"不支持的符号一元运算: {type(node.op).__name__}")
            return operation(convert(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _SYMPY_FUNCTIONS:
                name = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
                raise ValueError(f"不允许的符号函数调用: {name}")
            if node.keywords:
                raise ValueError("符号函数调用不支持关键字参数")
            return _SYMPY_FUNCTIONS[node.func.id](*(convert(arg) for arg in node.args))
        raise ValueError(f"不支持的符号 AST 节点: {type(node).__name__}")

    return convert(tree)


# ============================================================================
#  安全表达式求值器 (基于 AST 白名单)
# ============================================================================

class SafeEvaluator:
    """自定义 AST 遍历求值器 — 安全、透明、可扩展。

    不使用 eval/exec，只解释白名单内的 AST 节点，
    防止注入攻击，同时支持丰富数学函数。
    """

    _MAX_EXPRESSION_LENGTH = 10_000
    _MAX_AST_NODES = 1_000
    _MAX_POWER_EXPONENT = 100_000
    _MAX_SHIFT_COUNT = 1_000_000
    _MAX_FACTORIAL_INPUT = 100_000
    _MAX_INTEGER_BITS = 12_000

    # 白名单运算符
    _BINOPS: Dict[type, Callable] = {
        ast.Add:      lambda a, b: a + b,
        ast.Sub:      lambda a, b: a - b,
        ast.Mult:     lambda a, b: a * b,
        ast.Div:      lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod:      lambda a, b: a % b,
        ast.Pow:      lambda a, b: a ** b,
        ast.LShift:   lambda a, b: a << b if isinstance(a, int) and isinstance(b, int) else int(a) << int(b),
        ast.RShift:   lambda a, b: a >> b if isinstance(a, int) and isinstance(b, int) else int(a) >> int(b),
        ast.BitAnd:   lambda a, b: int(a) & int(b),
        ast.BitOr:    lambda a, b: int(a) | int(b),
        ast.BitXor:   lambda a, b: int(a) ^ int(b),
    }

    _UNOPS: Dict[type, Callable] = {
        ast.USub:   lambda a: -a,
        ast.UAdd:   lambda a: +a,
        ast.Invert: lambda a: ~int(a),
        ast.Not:    lambda a: not a,
    }

    _CMPOPS: Dict[type, Callable] = {
        ast.Eq:    lambda a, b: a == b,
        ast.NotEq: lambda a, b: a != b,
        ast.Lt:    lambda a, b: a < b,
        ast.LtE:   lambda a, b: a <= b,
        ast.Gt:    lambda a, b: a > b,
        ast.GtE:   lambda a, b: a >= b,
    }

    # 白名单函数
    _FUNCTIONS: Dict[str, Callable] = {
        # 基本数学
        "abs":           abs,
        "round":         round,
        "int":           int,
        "float":         float,
        "complex":       complex,
        "bool":          bool,
        "min":           min,
        "max":           max,
        "sum":           sum,
        "len":           len,
        "pow":           pow,
        "divmod":        divmod,
        # math 函数
        "sqrt":          math.sqrt,
        "cbrt":          math.cbrt,
        "exp":           math.exp,
        "log":           math.log,
        "log2":          math.log2,
        "log10":         math.log10,
        "sin":           math.sin,
        "cos":           math.cos,
        "tan":           math.tan,
        "asin":          math.asin,
        "acos":          math.acos,
        "atan":          math.atan,
        "atan2":         math.atan2,
        "sinh":          math.sinh,
        "cosh":          math.cosh,
        "tanh":          math.tanh,
        "degrees":       math.degrees,
        "radians":       math.radians,
        "hypot":         math.hypot,
        "floor":         math.floor,
        "ceil":          math.ceil,
        "trunc":         math.trunc,
        "factorial":     math.factorial,
        "gcd":           math.gcd,
        "lcm":           math.lcm,
        "perm":          math.perm,
        "comb":          math.comb,
        "gamma":         math.gamma,
        "lgamma":        math.lgamma,
        "erf":           math.erf,
        "erfc":          math.erfc,
        "isclose":       math.isclose,
        "isfinite":      math.isfinite,
        "isinf":         math.isinf,
        "isnan":         math.isnan,
    }

    # 内置常量
    _CONSTANTS: Dict[str, Union[float, complex]] = {
        "pi":     math.pi,
        "e":      math.e,
        "tau":    math.tau,
        "inf":    float("inf"),
        "nan":    float("nan"),
        "true":   True,
        "false":  False,
        "none":   None,
    }

    def evaluate(
        self,
        expression: str,
        variables: Optional[Dict[str, Any]] = None,
        functions: Optional[Dict[str, Callable]] = None,
        constants: Optional[Dict[str, Any]] = None,
        constant_converter: Optional[Callable[[Any], Any]] = None,
    ) -> Dict[str, Any]:
        """安全求值一个数学表达式。

        Args:
            expression: 数学表达式字符串，如 "sqrt(x**2 + y**2) * 3"
            variables: 变量绑定字典，如 {"x": 5, "y": 12}

        Returns:
            {"result": 值, "type": 类型字符串, "variables_used": 使用的变量列表}
        """
        if variables is None:
            variables = {}
        if not isinstance(expression, str):
            return {"error": "expression 必须是字符串", "result": None}
        if not expression.strip():
            return {"error": "expression 不能为空", "result": None}
        if len(expression) > self._MAX_EXPRESSION_LENGTH:
            return {"error": f"expression 长度不能超过 {self._MAX_EXPRESSION_LENGTH}", "result": None}

        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as e:
            return {"error": f"语法错误: {e}", "result": None}
        if sum(1 for _ in ast.walk(tree)) > self._MAX_AST_NODES:
            return {"error": f"表达式 AST 节点不能超过 {self._MAX_AST_NODES}", "result": None}

        allowed_functions = {**self._FUNCTIONS, **(functions or {})}
        env = {**self._CONSTANTS, **(constants or {}), **allowed_functions, **variables}
        used_vars: List[str] = []

        def _check_result_size(value):
            if isinstance(value, int) and not isinstance(value, bool):
                if value.bit_length() > self._MAX_INTEGER_BITS:
                    raise ValueError(
                        f"整数结果过大，不能超过 {self._MAX_INTEGER_BITS} bit"
                    )
            elif isinstance(value, (list, tuple)):
                for item in value:
                    _check_result_size(item)
            elif isinstance(value, dict):
                for item in value.values():
                    _check_result_size(item)
            return value

        def _eval(node):
            if isinstance(node, ast.Expression):
                return _eval(node.body)
            elif isinstance(node, ast.Constant):
                if not isinstance(node.value, (int, float, complex, bool)) and node.value is not None:
                    raise ValueError(f"不支持的常量类型: {type(node.value).__name__}")
                return constant_converter(node.value) if constant_converter else node.value
            elif isinstance(node, ast.Name):
                if node.id in env:
                    if node.id in variables:
                        used_vars.append(node.id)
                    return env[node.id]
                raise NameError(f"未定义变量或函数: '{node.id}'")
            elif isinstance(node, ast.BinOp):
                op_type = type(node.op)
                if op_type not in self._BINOPS:
                    raise ValueError(f"不支持的二元运算符: {op_type.__name__}")
                left = _eval(node.left)
                right = _eval(node.right)
                if op_type is ast.Pow and isinstance(right, (int, float, np.number, mp.mpf)):
                    if abs(right) > self._MAX_POWER_EXPONENT:
                        raise ValueError(f"幂指数绝对值不能超过 {self._MAX_POWER_EXPONENT}")
                if op_type in (ast.LShift, ast.RShift) and isinstance(right, (int, float, np.number)):
                    if right < 0 or right > self._MAX_SHIFT_COUNT:
                        raise ValueError(f"移位位数必须位于 [0, {self._MAX_SHIFT_COUNT}]")
                return _check_result_size(self._BINOPS[op_type](left, right))
            elif isinstance(node, ast.UnaryOp):
                op_type = type(node.op)
                if op_type not in self._UNOPS:
                    raise ValueError(f"不支持的一元运算符: {op_type.__name__}")
                return self._UNOPS[op_type](_eval(node.operand))
            elif isinstance(node, ast.Compare):
                left = _eval(node.left)
                for op, comp in zip(node.ops, node.comparators):
                    op_type = type(op)
                    if op_type not in self._CMPOPS:
                        raise ValueError(f"不支持的比较运算符: {op_type.__name__}")
                    right = _eval(comp)
                    if not self._CMPOPS[op_type](left, right):
                        return False
                    left = right
                return True
            elif isinstance(node, ast.BoolOp):
                if isinstance(node.op, ast.And):
                    for v in node.values:
                        if not _eval(v):
                            return False
                    return True
                elif isinstance(node.op, ast.Or):
                    for v in node.values:
                        if _eval(v):
                            return True
                    return False
            elif isinstance(node, ast.Call):
                func_name = node.func.id if isinstance(node.func, ast.Name) else None
                if func_name not in allowed_functions:
                    raise ValueError(f"不允许的函数调用: '{func_name}'")
                args = [_eval(a) for a in node.args]
                if func_name == "pow" and len(args) >= 2:
                    exponent = args[1]
                    if isinstance(exponent, (int, float, np.number, mp.mpf)):
                        if abs(exponent) > self._MAX_POWER_EXPONENT:
                            raise ValueError(f"幂指数绝对值不能超过 {self._MAX_POWER_EXPONENT}")
                if func_name == "factorial" and args:
                    value = args[0]
                    if isinstance(value, (int, float, np.number, mp.mpf)):
                        if value < 0 or value > self._MAX_FACTORIAL_INPUT:
                            raise ValueError(
                                f"factorial 输入必须位于 [0, {self._MAX_FACTORIAL_INPUT}]"
                            )
                if any(kw.arg is None for kw in node.keywords):
                    raise ValueError("不允许使用 **kwargs 展开")
                kwargs = {kw.arg: _eval(kw.value) for kw in node.keywords}
                return _check_result_size(allowed_functions[func_name](*args, **kwargs))
            elif isinstance(node, ast.IfExp):
                test = _eval(node.test)
                return _eval(node.body) if test else _eval(node.orelse)
            elif isinstance(node, ast.List):
                return [_eval(e) for e in node.elts]
            elif isinstance(node, ast.Tuple):
                return tuple(_eval(e) for e in node.elts)
            elif isinstance(node, ast.Subscript):
                obj = _eval(node.value)
                key = _eval(node.slice) if isinstance(node.slice, ast.Index) else _eval(node.slice)
                return obj[key]
            else:
                raise ValueError(f"不支持的 AST 节点: {type(node).__name__}")

        try:
            result = _check_result_size(_eval(tree))
        except Exception as e:
            return {"error": str(e), "result": None}

        result_type = type(result).__name__
        return {
            "result": _json_safe_numeric(result),
            "type": result_type,
            "variables_used": list(dict.fromkeys(used_vars)),  # 去重保序
        }


# ============================================================================
#  主计算引擎
# ============================================================================

class ComputationalEngine:
    """Xenon 计算引擎 — 统一数学计算入口。

    用法:
        engine = ComputationalEngine()
        result = engine.evaluate("sqrt(2) * pi")
        result = engine.symbolic_diff("x**3 + 2*x", "x")
        result = engine.matrix_det([[1, 2], [3, 4]])
        result = engine.high_precision_pi(100)
        result = engine.descriptive_stats([1, 2, 3, 4, 5])
    """

    _MAX_SYMBOLIC_MATRIX_DIM = 8
    _MAX_SYMBOLIC_EIG_DIM = 4
    _MAX_RREF_ELEMENTS = 2_500
    _MAX_MPMATH_PRECISION = 100_000
    _MAX_GRADIENT_ITERATIONS = 100_000

    def __init__(self, mp_dps: int = 50):
        """
        Args:
            mp_dps: mpmath 默认精度（有效位数），默认 50
        """
        self._evaluator = SafeEvaluator()
        self.mp_dps = mp_dps

    @staticmethod
    def _mp_environment() -> Tuple[Dict[str, Callable], Dict[str, Any]]:
        functions = {
            name: getattr(mp, name)
            for name in (
                "sqrt", "cbrt", "exp", "log", "sin", "cos", "tan",
                "asin", "acos", "atan", "sinh", "cosh", "tanh",
                "gamma", "factorial", "erf", "erfc", "floor", "ceil",
                "zeta", "besselj", "bessely", "airyai", "airybi",
            )
            if hasattr(mp, name)
        }
        functions.update({
            "ln": mp.log,
            "abs": abs,
            "pow": lambda a, b: a ** b,
            "min": min,
            "max": max,
        })
        constants = {"pi": mp.pi, "e": mp.e, "tau": 2 * mp.pi, "inf": mp.inf}
        return functions, constants

    @staticmethod
    def _normalize_mp_variables(variables: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for name, value in (variables or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                normalized[name] = mp.mpf(str(value))
            elif isinstance(value, complex):
                normalized[name] = mp.mpc(value)
            else:
                normalized[name] = value
        return normalized

    @staticmethod
    def _format_mp_result(result: Any, precision: int) -> Dict[str, Any]:
        if isinstance(result, mp.mpf):
            return {
                "result": _json_safe_numeric(float(result)),
                "result_str": mp.nstr(result, precision),
                "type": type(result).__name__,
                "precision": precision,
            }
        if isinstance(result, mp.mpc):
            return {
                "result": str(result),
                "result_str": mp.nstr(result, precision),
                "type": type(result).__name__,
                "precision": precision,
            }
        return {
            "result": _json_safe_numeric(result),
            "type": type(result).__name__,
            "precision": precision,
        }

    # ------------------------------------------------------------------ #
    #  1. 安全表达式求值
    # ------------------------------------------------------------------ #

    def evaluate(self, expression: str, variables: Optional[Dict[str, Any]] = None,
                 high_precision: bool = False, precision: Optional[int] = None) -> Dict[str, Any]:
        """安全求值数学表达式。

        Args:
            expression: 数学表达式，如 "sqrt(x**2 + y**2)"
            variables: 变量字典，如 {"x": 3, "y": 4}
            high_precision: 是否使用高精度 (mpmath) 求值
            precision: 高精度时的有效位数，默认使用 self.mp_dps

        Returns:
            包含 result, type, variables_used 的字典

        Examples:
            >>> engine.evaluate("sqrt(2) + log(e)")
            {'result': 2.414213562373095, ...}
            >>> engine.evaluate("x**2 + y**2", {"x": 3, "y": 4})
            {'result': 25, ...}
        """
        if high_precision:
            return self._evaluate_high_precision(expression, variables, precision)
        return self._evaluator.evaluate(expression, variables)

    def _evaluate_high_precision(self, expression: str, variables: Optional[Dict[str, Any]] = None,
                                  precision: Optional[int] = None) -> Dict[str, Any]:
        """使用 mpmath 进行高精度求值。"""
        try:
            target_precision = _coerce_int(
                self.mp_dps if precision is None else precision,
                "precision",
                2,
                self._MAX_MPMATH_PRECISION,
            )
        except ValueError as e:
            return {"error": str(e), "result": None}

        with mp.workdps(target_precision):
            functions, constants = self._mp_environment()
            evaluated = self._evaluator.evaluate(
                expression,
                self._normalize_mp_variables(variables),
                functions=functions,
                constants=constants,
                constant_converter=lambda value: (
                    mp.mpf(str(value))
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    else value
                ),
            )
            if "error" in evaluated:
                return evaluated
            result = self._format_mp_result(evaluated["result"], target_precision)
            result["variables_used"] = evaluated.get("variables_used", [])
            return result

    # ------------------------------------------------------------------ #
    #  2. 符号计算 (sympy)
    # ------------------------------------------------------------------ #

    def symbolic_diff(self, expression: str, variable: str = "x",
                       order: int = 1) -> Dict[str, Any]:
        """符号微分。

        Args:
            expression: 表达式字符串，如 "x**3 + 2*x + sin(x)"
            variable: 微分的变量
            order: 微分阶数

        Returns:
            {"derivative": 导数字符串, "expression": 原始表达式, "latex": LaTeX 形式}
        """
        try:
            if not isinstance(order, int) or isinstance(order, bool) or order < 0:
                return {"error": "order 必须是非负整数", "expression": expression}
            x = sp.Symbol(variable)
            expr = _safe_sympify(expression, {variable: x})
            deriv = diff(expr, x, order)
            return {
                "expression": expression,
                "variable": variable,
                "order": order,
                "derivative": str(deriv),
                "derivative_simplified": str(simplify(deriv)),
                "latex": sp.latex(deriv),
            }
        except Exception as e:
            return {"error": str(e), "expression": expression}

    def symbolic_integrate(self, expression: str, variable: str = "x",
                            definite: bool = False, lower=None, upper=None) -> Dict[str, Any]:
        """符号积分。

        Args:
            expression: 被积函数
            variable: 积分变量
            definite: 是否定积分
            lower: 积分下限（定积分时）
            upper: 积分上限（定积分时）

        Returns:
            {"integral": 积分结果, "latex": LaTeX 形式}
        """
        try:
            x = sp.Symbol(variable)
            expr = _safe_sympify(expression, {variable: x})
            if definite:
                if lower is None or upper is None:
                    return {"error": "定积分必须同时提供 lower 和 upper", "expression": expression}
                result = integrate(expr, (x, _safe_sympify(lower), _safe_sympify(upper)))
            else:
                result = integrate(expr, x)
            return {
                "expression": expression,
                "variable": variable,
                "definite": definite,
                "lower": lower,
                "upper": upper,
                "integral": str(result),
                "latex": sp.latex(result),
            }
        except Exception as e:
            return {"error": str(e), "expression": expression}

    def symbolic_limit(self, expression: str, variable: str = "x",
                        point=None, direction: str = "+") -> Dict[str, Any]:
        """符号极限。

        Args:
            expression: 表达式
            variable: 变量
            point: 趋近点（可以是数值、"oo"表示正无穷、"-oo"表示负无穷）
            direction: "+" 或 "-"

        Returns:
            {"limit": 极限值, "latex": LaTeX 形式}
        """
        try:
            if point is None:
                return {"error": "point 不能为空", "expression": expression}
            if direction not in ("+", "-", "+-"):
                return {"error": "direction 必须是 '+'、'-' 或 '+-'", "expression": expression}
            x = sp.Symbol(variable)
            expr = _safe_sympify(expression, {variable: x})

            # 解析 point 字符串
            if isinstance(point, str):
                if point.lower() in ("oo", "inf", "+oo", "+inf"):
                    pt = oo
                elif point.lower() in ("-oo", "-inf"):
                    pt = -oo
                else:
                    pt = _safe_sympify(point)
            else:
                pt = point

            result = limit(expr, x, pt, dir=direction)
            return {
                "expression": expression,
                "variable": variable,
                "point": str(pt),
                "direction": direction,
                "limit": str(result),
                "latex": sp.latex(result),
            }
        except Exception as e:
            return {"error": str(e), "expression": expression}

    def symbolic_solve(self, equation: str, variable: str = "x",
                        domain: str = "real") -> Dict[str, Any]:
        """符号求解方程。

        Args:
            equation: 方程，如 "x**2 - 4 = 0" 或 "x**2 - 4"（默认 =0）
            variable: 求解变量
            domain: "real" 或 "complex"

        Returns:
            {"solutions": 解列表, "count": 解的数量}
        """
        try:
            x = sp.Symbol(variable)
            # 如果没有等号，默认 =0
            if "=" in equation:
                left, right = equation.split("=", 1)
                eq = sp.Eq(
                    _safe_sympify(left.strip(), {variable: x}),
                    _safe_sympify(right.strip(), {variable: x}),
                )
            else:
                eq = sp.Eq(_safe_sympify(equation, {variable: x}), 0)

            if domain not in ("real", "complex"):
                return {"error": "domain 必须是 real 或 complex", "equation": equation}
            solutions = solve(eq, x)
            if domain == "real":
                solutions = [solution for solution in solutions if solution.is_real is not False]

            numeric_solutions = [
                float(solution.evalf())
                if solution.is_number and solution.is_real is True
                else str(solution.evalf()) if solution.is_number else str(solution)
                for solution in solutions
            ]
            return {
                "equation": str(eq),
                "variable": variable,
                "domain": domain,
                "solutions": [str(s) for s in solutions],
                "solutions_numeric": numeric_solutions,
                "count": len(solutions),
            }
        except Exception as e:
            return {"error": str(e), "equation": equation}

    def symbolic_simplify(self, expression: str) -> Dict[str, Any]:
        """符号化简表达式。

        Args:
            expression: 表达式字符串

        Returns:
            {"simplified": 化简结果, "steps": 中间步骤（如果有）}
        """
        try:
            expr = _safe_sympify(expression)
            simplified = simplify(expr)
            return {
                "expression": expression,
                "simplified": str(simplified),
                "latex": sp.latex(simplified),
                "expanded": str(expand(expr)),
                "factored": str(factor(expr)) if _is_factorable(expr) else "不可因式分解",
            }
        except Exception as e:
            return {"error": str(e), "expression": expression}

    def symbolic_series(self, expression: str, variable: str = "x",
                         point: float = 0, order: int = 6) -> Dict[str, Any]:
        """泰勒/麦克劳林级数展开。

        Args:
            expression: 表达式
            variable: 变量
            point: 展开点
            order: 展开阶数

        Returns:
            {"series": 级数字符串, "latex": LaTeX 形式}
        """
        try:
            if not isinstance(order, int) or isinstance(order, bool) or order < 1:
                return {"error": "order 必须是正整数", "expression": expression}
            x = sp.Symbol(variable)
            expr = _safe_sympify(expression, {variable: x})
            ser = series(expr, x, point, order)
            return {
                "expression": expression,
                "variable": variable,
                "point": point,
                "order": order,
                "series": str(ser),
                "latex": sp.latex(ser),
            }
        except Exception as e:
            return {"error": str(e), "expression": expression}

    # ------------------------------------------------------------------ #
    #  3. 矩阵与线性代数
    # ------------------------------------------------------------------ #

    def matrix_operations(self, matrix_data: List[List[float]],
                           operation: str,
                           norm_type: str = "fro") -> Dict[str, Any]:
        """矩阵运算统一接口。

        Args:
            matrix_data: 二维列表表示的矩阵
            operation: 运算名 —
                "det" 行列式, "inv" 逆矩阵, "eig" 特征值/特征向量,
                "rank" 秩, "trace" 迹, "transpose" 转置,
                "svd" 奇异值分解, "norm" 范数, "rref" 行简化阶梯形
            norm_type: norm 运算使用的范数类型，默认 "fro"

        Returns:
            对应运算结果
        """
        try:
            # 检查矩阵是否为矩形
            if not matrix_data or not all(isinstance(row, list) for row in matrix_data):
                return {"error": "矩阵数据必须是非空二维列表"}
            row_len = len(matrix_data[0])
            if row_len == 0:
                return {"error": "矩阵每行至少需要一个元素"}
            if not all(len(row) == row_len for row in matrix_data):
                return {"error": f"矩阵必须是矩形：各行长度不一致（第1行={row_len}，但发现不等长行）"}
            A_np = np.array(matrix_data, dtype=float)
            if A_np.ndim != 2:
                return {"error": "矩阵元素必须是数值，且矩阵必须是二维结构"}
            if not np.all(np.isfinite(A_np)):
                return {"error": "矩阵元素必须全部为有限数值"}
            rows, columns = A_np.shape
            use_symbolic = max(rows, columns) <= self._MAX_SYMBOLIC_MATRIX_DIM
            symbolic_skip_note = (
                f"为避免高阶符号计算耗时，超过 {self._MAX_SYMBOLIC_MATRIX_DIM} 阶时跳过"
            )

            if operation == "det":
                if A_np.shape[0] != A_np.shape[1]:
                    return {"error": "矩阵必须是方阵才能计算行列式"}
                symbolic_result = str(sp.Matrix(matrix_data).det()) if use_symbolic else symbolic_skip_note
                return {"operation": "det", "result": float(np.linalg.det(A_np)),
                         "result_symbolic": symbolic_result}
            elif operation == "inv":
                if A_np.shape[0] != A_np.shape[1]:
                    return {"error": "矩阵必须是方阵才能求逆"}
                symbolic_result = str(sp.Matrix(matrix_data).inv()) if use_symbolic else symbolic_skip_note
                return {"operation": "inv", "result": np.linalg.inv(A_np).tolist(),
                         "result_symbolic": symbolic_result}
            elif operation == "eig":
                if A_np.shape[0] != A_np.shape[1]:
                    return {"error": "矩阵必须是方阵才能计算特征值"}
                eigenvalues, eigenvectors = np.linalg.eig(A_np)
                symbolic_eigenvalues = (
                    [str(ev) for ev in sp.Matrix(matrix_data).eigenvals().keys()]
                    if rows <= self._MAX_SYMBOLIC_EIG_DIM
                    else [f"为避免高阶符号计算耗时，超过 {self._MAX_SYMBOLIC_EIG_DIM} 阶时跳过"]
                )
                return {
                    "operation": "eig",
                    "eigenvalues": _json_safe_numeric(eigenvalues),
                    "eigenvectors": _json_safe_numeric(eigenvectors),
                    "eigenvalues_symbolic": symbolic_eigenvalues,
                }
            elif operation == "rank":
                symbolic_result = sp.Matrix(matrix_data).rank() if use_symbolic else symbolic_skip_note
                return {"operation": "rank", "result": int(np.linalg.matrix_rank(A_np)),
                         "result_symbolic": symbolic_result}
            elif operation == "trace":
                if A_np.shape[0] != A_np.shape[1]:
                    return {"error": "矩阵必须是方阵才能计算迹"}
                symbolic_result = str(sp.Matrix(matrix_data).trace()) if use_symbolic else symbolic_skip_note
                return {"operation": "trace", "result": float(np.trace(A_np)),
                         "result_symbolic": symbolic_result}
            elif operation == "transpose":
                return {"operation": "transpose", "result": A_np.T.tolist()}
            elif operation == "svd":
                U, s, Vt = np.linalg.svd(A_np)
                return {"operation": "svd", "U": U.tolist(), "singular_values": s.tolist(), "Vt": Vt.tolist()}
            elif operation == "norm":
                return {"operation": "norm", "type": norm_type, "result": float(np.linalg.norm(A_np, ord=norm_type))}
            elif operation == "rref":
                if rows * columns > self._MAX_RREF_ELEMENTS:
                    return {
                        "error": (
                            f"rref 最多支持 {self._MAX_RREF_ELEMENTS} 个矩阵元素，"
                            f"当前为 {rows * columns}"
                        )
                    }
                A_sp = sp.Matrix(matrix_data)
                rref_result = A_sp.rref()
                return {"operation": "rref", "result": [[float(v) for v in row] for row in rref_result[0].tolist()],
                         "pivot_columns": list(rref_result[1])}
            else:
                return {"error": f"未知运算: {operation}",
                        "available": ["det", "inv", "eig", "rank", "trace", "transpose", "svd", "norm", "rref"]}
        except np.linalg.LinAlgError as e:
            return {"error": f"线性代数错误: {e}"}
        except Exception as e:
            return {"error": str(e)}

    def matrix_det(self, matrix_data: List[List[float]]) -> Dict[str, Any]:
        """行列式（便捷方法）。"""
        return self.matrix_operations(matrix_data, "det")

    def matrix_inv(self, matrix_data: List[List[float]]) -> Dict[str, Any]:
        """逆矩阵（便捷方法）。"""
        return self.matrix_operations(matrix_data, "inv")

    def matrix_eig(self, matrix_data: List[List[float]]) -> Dict[str, Any]:
        """特征值/特征向量（便捷方法）。"""
        return self.matrix_operations(matrix_data, "eig")

    # ------------------------------------------------------------------ #
    #  4. 高精度计算 (mpmath)
    # ------------------------------------------------------------------ #

    def high_precision_eval(self, expression: str, precision: int = 50) -> Dict[str, Any]:
        """高精度求值任意表达式。

        Args:
            expression: 表达式字符串
            precision: 有效位数

        Returns:
            {"result": 浮点值, "result_str": 完整数字字符串, "precision": 精度}
        """
        result = self._evaluate_high_precision(expression, precision=precision)
        result["expression"] = expression
        return result

    def high_precision_pi(self, digits: int = 100) -> Dict[str, Any]:
        """计算 π 到指定小数位数。

        Args:
            digits: π 的小数位数（不含整数部分）

        Returns:
            {"pi": π 字符串, "digits": 位数}
        """
        try:
            digits = _coerce_int(digits, "digits", 1, self._MAX_MPMATH_PRECISION)
        except ValueError as e:
            return {"error": str(e)}
        with mp.workdps(digits + 4):
            pi_str = mp.nstr(mp.pi, digits + 1)
            return {"pi": pi_str, "digits": digits}

    def high_precision_e(self, digits: int = 100) -> Dict[str, Any]:
        """计算 e 到指定小数位数。"""
        try:
            digits = _coerce_int(digits, "digits", 1, self._MAX_MPMATH_PRECISION)
        except ValueError as e:
            return {"error": str(e)}
        with mp.workdps(digits + 4):
            e_str = mp.nstr(mp.e, digits + 1)
            return {"e": e_str, "digits": digits}

    def high_precision_function(self, function_name: str, args: List[Any],
                                  precision: int = 50) -> Dict[str, Any]:
        """高精度调用 mpmath 函数。

        Args:
            function_name: mpmath 函数名，如 "zeta", "gamma", "besselj", "airyai"
            args: 函数参数列表
            precision: 精度

        Returns:
            {"function": 函数名, "args": 参数列表, "result": ...}
        """
        try:
            precision = _coerce_int(precision, "precision", 2, self._MAX_MPMATH_PRECISION)
        except ValueError as e:
            return {"error": str(e), "function": function_name}
        if function_name.startswith("_"):
            return {"error": f"不允许的 mpmath 函数: {function_name}"}
        try:
            with mp.workdps(precision):
                functions, _ = self._mp_environment()
                func = functions.get(function_name)
                if func is None:
                    return {
                        "error": f"未知或不允许的 mpmath 函数: {function_name}",
                        "available": sorted(functions),
                    }
                normalized_args = [
                    mp.mpf(str(arg)) if isinstance(arg, float) else mp.mpc(arg) if isinstance(arg, complex) else arg
                    for arg in args
                ]
                result = func(*normalized_args)
                formatted = self._format_mp_result(result, precision)
                formatted.update({"function": function_name, "args": _json_safe_numeric(list(args))})
                return formatted
        except Exception as e:
            return {"error": str(e), "function": function_name}

    # ------------------------------------------------------------------ #
    #  5. 统计分析
    # ------------------------------------------------------------------ #

    def descriptive_stats(self, data: List[float],
                           population: bool = False) -> Dict[str, Any]:
        """计算描述性统计量。

        Args:
            data: 数值列表
            population: True=总体统计, False=样本统计

        Returns:
            包含 mean, median, mode, variance, std, min, max, range, q1, q3, iqr, skewness, kurtosis 的字典
        """
        try:
            if not data:
                return {"error": "数据列表不能为空"}
            arr = np.array(data, dtype=float)
            n = len(arr)
            if not population and n < 2:
                return {"error": "样本统计至少需要 2 个数据点"}
            if not np.all(np.isfinite(arr)):
                return {"error": "数据必须全部为有限数值"}

            # 基本统计
            mean_val = float(np.mean(arr))
            median_val = float(np.median(arr))

            # 众数
            from collections import Counter as _Counter
            counter = _Counter(data)
            max_count = max(counter.values())
            modes = [k for k, v in counter.items() if v == max_count]
            if max_count == 1:
                mode_val = None
            else:
                mode_val = modes if len(modes) > 1 else modes[0]

            # 方差 / 标准差
            ddof = 0 if population else 1
            var_val = float(np.var(arr, ddof=ddof))
            std_val = float(np.std(arr, ddof=ddof))

            # 分位数
            q1 = float(np.percentile(arr, 25))
            q3 = float(np.percentile(arr, 75))

            # 偏度 / 峰度。样本统计使用无偏修正，避免混用样本标准差与总体中心矩。
            centered = arr - mean_val
            moment2 = float(np.mean(centered**2))
            if moment2 <= 1e-30:
                skew = 0.0
                kurt = 0.0
            else:
                moment3 = float(np.mean(centered**3))
                moment4 = float(np.mean(centered**4))
                population_skew = moment3 / (moment2 ** 1.5)
                population_kurt = moment4 / (moment2 ** 2) - 3
                if population:
                    skew = float(population_skew)
                    kurt = float(population_kurt)
                else:
                    skew = (
                        float(math.sqrt(n * (n - 1)) / (n - 2) * population_skew)
                        if n >= 3 else None
                    )
                    kurt = (
                        float((n - 1) / ((n - 2) * (n - 3)) * ((n + 1) * population_kurt + 6))
                        if n >= 4 else None
                    )

            return {
                "count": n,
                "mean": mean_val,
                "median": median_val,
                "mode": mode_val,
                "mode_count": max_count,
                "variance": var_val,
                "std": std_val,
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "range": float(np.max(arr) - np.min(arr)),
                "sum": float(np.sum(arr)),
                "q1": q1,
                "q3": q3,
                "iqr": q3 - q1,
                "skewness": skew,
                "kurtosis": kurt,
                "type": "population" if population else "sample",
            }
        except Exception as e:
            return {"error": str(e)}

    def linear_regression(self, x: List[float], y: List[float]) -> Dict[str, Any]:
        """简单线性回归 y = a*x + b。

        Returns:
            {"slope": a, "intercept": b, "r_squared": R², "predictions": 预测值列表}
        """
        try:
            x_arr = np.array(x, dtype=float)
            y_arr = np.array(y, dtype=float)
            n = len(x_arr)

            if n != len(y_arr):
                return {"error": "x 和 y 长度必须相同"}
            if n < 2:
                return {"error": "线性回归至少需要 2 对数据点"}
            if not np.all(np.isfinite(x_arr)) or not np.all(np.isfinite(y_arr)):
                return {"error": "x 和 y 必须全部为有限数值"}

            x_mean = np.mean(x_arr)
            y_mean = np.mean(y_arr)

            x_variance_sum = np.sum((x_arr - x_mean)**2)
            if x_variance_sum <= 1e-15:
                return {"error": "x 数据不能全部相同"}
            slope = np.sum((x_arr - x_mean) * (y_arr - y_mean)) / x_variance_sum
            intercept = y_mean - slope * x_mean

            predictions = slope * x_arr + intercept
            ss_res = np.sum((y_arr - predictions)**2)
            ss_tot = np.sum((y_arr - y_mean)**2)
            r_squared = 1 - ss_res / ss_tot if ss_tot > 1e-15 else 1.0

            return {
                "slope": float(slope),
                "intercept": float(intercept),
                "r_squared": float(r_squared),
                "equation": f"y = {slope:.6g} * x + {intercept:.6g}",
                "predictions": predictions.tolist(),
            }
        except Exception as e:
            return {"error": str(e)}

    def probability_distribution(self, dist_name: str, params: Dict[str, float],
                                   x_values: Optional[List[float]] = None,
                                   operation: str = "pdf") -> Dict[str, Any]:
        """概率分布计算。

        支持的分布: normal, uniform, exponential, poisson, binomial, chi2, t, f

        Args:
            dist_name: 分布名称
            params: 分布参数字典（如 {"loc": 0, "scale": 1} for normal）
            x_values: 计算点列表
            operation: "pdf", "cdf", "ppf"（分位函数）, "rvs"（生成随机数）

        Returns:
            计算结果
        """
        try:
            if not isinstance(dist_name, str):
                return {"error": "dist_name 必须是字符串"}
            if params is None:
                params = {}
            if not isinstance(params, dict):
                return {"error": "params 必须是对象"}
            if not isinstance(operation, str):
                return {"error": "operation 必须是字符串"}
            operation = operation.lower()
            distribution_map = {
                "normal": ("norm", {"loc": 0, "scale": 1}, False),
                "uniform": ("uniform", {"loc": 0, "scale": 1}, False),
                "exponential": ("expon", {"loc": 0, "scale": 1}, False),
                "poisson": ("poisson", {"mu": 1, "loc": 0}, True),
                "binomial": ("binom", {"n": 1, "p": 0.5, "loc": 0}, True),
                "chi2": ("chi2", {"df": 1, "loc": 0, "scale": 1}, False),
                "t": ("t", {"df": 1, "loc": 0, "scale": 1}, False),
                "f": ("f", {"dfn": 1, "dfd": 1, "loc": 0, "scale": 1}, False),
            }

            normalized_name = dist_name.lower()
            if normalized_name not in distribution_map:
                return {"error": f"未知分布: {dist_name}",
                        "available": list(distribution_map.keys())}

            try:
                from scipy import stats
            except ImportError:
                return {
                    "error": "概率分布计算需要 SciPy；请安装 requirements.txt 中声明的 scipy>=1.10.0",
                    "dependency": "scipy",
                }

            dist_module_name, defaults, is_discrete = distribution_map[normalized_name]
            dist_module = getattr(stats, dist_module_name, None)
            if dist_module is None:
                return {"error": f"无法加载分布模块: {dist_module_name}"}

            allowed_params = set(defaults)
            if operation == "rvs":
                allowed_params.add("size")
            unknown_params = sorted(set(params) - allowed_params)
            if unknown_params:
                return {
                    "error": f"未知分布参数: {', '.join(unknown_params)}",
                    "available_params": sorted(allowed_params),
                }

            dist_params = {name: params.get(name, default) for name, default in defaults.items()}
            numeric_params = np.array(list(dist_params.values()), dtype=float)
            if not np.all(np.isfinite(numeric_params)):
                return {"error": "分布参数必须全部为有限数值"}
            if "scale" in dist_params and dist_params["scale"] <= 0:
                return {"error": "scale 必须为正数"}
            if normalized_name == "poisson" and dist_params["mu"] < 0:
                return {"error": "poisson 的 mu 不能为负数"}
            if normalized_name == "binomial":
                try:
                    dist_params["n"] = _coerce_int(dist_params["n"], "binomial 的 n", 0)
                except ValueError as e:
                    return {"error": str(e)}
                if not 0 <= dist_params["p"] <= 1:
                    return {"error": "binomial 的 p 必须位于 [0, 1]"}
            for name in ("df", "dfn", "dfd"):
                if name in dist_params and dist_params[name] <= 0:
                    return {"error": f"{name} 必须为正数"}
            if "loc" in dist_params and is_discrete:
                try:
                    dist_params["loc"] = _coerce_int(dist_params["loc"], "离散分布的 loc")
                except ValueError as e:
                    return {"error": str(e)}

            if operation == "rvs":
                try:
                    size = _coerce_int(params.get("size", 10), "size", 1)
                except ValueError as e:
                    return {"error": str(e)}
                if size > 10_000:
                    return {"error": "size 不能超过 10000"}
                samples = dist_module.rvs(**dist_params, size=size)
                return {
                    "distribution": normalized_name,
                    "operation": "rvs",
                    "samples": _json_safe_numeric(np.asarray(samples).tolist()),
                }

            if x_values is None:
                x_values = (
                    np.linspace(0.01, 0.99, 99).tolist()
                    if operation == "ppf"
                    else np.linspace(-5, 5, 100).tolist()
                )
            if not isinstance(x_values, list) or not x_values:
                return {"error": "x_values 必须是非空数值列表"}
            if len(x_values) > 10_000:
                return {"error": "x_values 最多支持 10000 个计算点"}

            x_arr = np.array(x_values, dtype=float)
            if not np.all(np.isfinite(x_arr)):
                return {"error": "x_values 必须全部为有限数值"}
            if operation == "ppf" and np.any((x_arr < 0) | (x_arr > 1)):
                return {"error": "ppf 的 x_values 必须位于 [0, 1]"}

            if operation == "pdf":
                actual_operation = "pmf" if is_discrete else "pdf"
                results = getattr(dist_module, actual_operation)(x_arr, **dist_params)
            elif operation == "cdf":
                actual_operation = "cdf"
                results = dist_module.cdf(x_arr, **dist_params)
            elif operation == "ppf":
                actual_operation = "ppf"
                results = dist_module.ppf(x_arr, **dist_params)
            else:
                return {"error": f"未知操作: {operation}", "available": ["pdf", "cdf", "ppf", "rvs"]}

            return {
                "distribution": normalized_name,
                "operation": operation,
                "actual_operation": actual_operation,
                "x_values": x_values,
                "results": _json_safe_numeric(results.tolist()),
                "full_length": len(results),
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------ #
    #  6. 傅里叶分析
    # ------------------------------------------------------------------ #

    def fft_analysis(self, signal: List[float], sample_rate: float = 1.0) -> Dict[str, Any]:
        """快速傅里叶变换分析。

        Args:
            signal: 时域信号
            sample_rate: 采样率 (Hz)

        Returns:
            {"frequencies": 频率数组, "magnitudes": 幅度谱, "phases": 相位谱, "dominant_freq": 主频率}
        """
        try:
            n = len(signal)
            if n == 0:
                return {"error": "signal 不能为空"}
            if sample_rate <= 0:
                return {"error": "sample_rate 必须为正数"}
            signal_arr = np.array(signal, dtype=float)
            if not np.all(np.isfinite(signal_arr)):
                return {"error": "signal 必须全部为有限数值"}

            fft_result = np.fft.rfft(signal_arr)
            freqs_positive = np.fft.rfftfreq(n, d=1/sample_rate)
            magnitudes = np.abs(fft_result) / n * 2
            magnitudes[0] /= 2  # DC 分量不需要乘 2
            if n % 2 == 0 and len(magnitudes) > 1:
                magnitudes[-1] /= 2  # Nyquist 分量也不需要乘 2
            phases = np.angle(fft_result)

            # 主频率
            if len(magnitudes) > 1:
                dominant_idx = np.argmax(magnitudes[1:]) + 1  # 跳过 DC
                dominant_freq = float(freqs_positive[dominant_idx])
                dominant_mag = float(magnitudes[dominant_idx])
            else:
                dominant_freq = 0.0
                dominant_mag = 0.0

            return {
                "frequencies": freqs_positive.tolist()[:20],
                "magnitudes": magnitudes.tolist()[:20],
                "phases": phases.tolist()[:20],
                "dominant_frequency": dominant_freq,
                "dominant_magnitude": dominant_mag,
                "n_samples": n,
                "sample_rate": sample_rate,
                "_note": "仅显示前 20 个频率分量",
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------ #
    #  7. 优化求解
    # ------------------------------------------------------------------ #

    def gradient_descent(self, func_expr: str, variables: Dict[str, float],
                          learning_rate: float = 0.01, max_iter: int = 1000,
                          tolerance: float = 1e-6) -> Dict[str, Any]:
        """梯度下降优化（使用 sympy 符号微分计算梯度）。

        Args:
            func_expr: 目标函数表达式，如 "(x-3)**2 + (y+2)**2"
            variables: 初始变量值，如 {"x": 0, "y": 0}
            learning_rate: 学习率
            max_iter: 最大迭代次数
            tolerance: 收敛容差

        Returns:
            {"optimal_variables": ..., "optimal_value": ..., "iterations": ..., "converged": ...}
        """
        try:
            if not variables:
                return {"error": "variables 不能为空"}
            try:
                max_iter = _coerce_int(
                    max_iter,
                    "max_iter",
                    1,
                    self._MAX_GRADIENT_ITERATIONS,
                )
            except ValueError as e:
                return {"error": str(e)}
            if (
                not math.isfinite(float(learning_rate))
                or not math.isfinite(float(tolerance))
                or learning_rate <= 0
                or tolerance <= 0
            ):
                return {"error": "learning_rate、max_iter 和 tolerance 必须为正数"}
            syms = {name: sp.Symbol(name) for name in variables}
            expr = _safe_sympify(func_expr, syms)

            # 计算梯度
            grads = {name: sp.diff(expr, sym) for name, sym in syms.items()}

            # 将 sympy 表达式转为可调用的数值函数
            grad_funcs = {}
            for name, grad_expr in grads.items():
                grad_funcs[name] = sp.lambdify(list(syms.values()), grad_expr, "numpy")
            func = sp.lambdify(list(syms.values()), expr, "numpy")

            current = np.array([variables[name] for name in variables], dtype=float)
            var_names = list(variables.keys())
            if not np.all(np.isfinite(current)):
                return {"error": "变量初值必须全部为有限数值"}

            for i in range(max_iter):
                with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                    grad_values = np.array([
                        float(grad_funcs[name](*current)) for name in var_names
                    ])
                if not np.all(np.isfinite(grad_values)):
                    return {
                        "error": "梯度下降发生数值发散",
                        "iterations": i + 1,
                        "last_finite_variables": {
                            name: float(current[j]) for j, name in enumerate(var_names)
                        },
                    }

                if np.linalg.norm(grad_values) < tolerance:
                    optimal_value = float(func(*current))
                    if not math.isfinite(optimal_value):
                        return {"error": "目标函数结果不是有限数值", "iterations": i + 1}
                    return {
                        "optimal_variables": {name: float(current[j]) for j, name in enumerate(var_names)},
                        "optimal_value": optimal_value,
                        "iterations": i + 1,
                        "converged": True,
                        "gradient_norm": float(np.linalg.norm(grad_values)),
                    }

                with np.errstate(over="ignore", invalid="ignore"):
                    current = current - learning_rate * grad_values
                if not np.all(np.isfinite(current)):
                    return {
                        "error": "梯度下降发生数值发散",
                        "iterations": i + 1,
                    }

            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                optimal_value = float(func(*current))
                final_grad_values = np.array([
                    float(grad_funcs[name](*current)) for name in var_names
                ])
            if not math.isfinite(optimal_value) or not np.all(np.isfinite(final_grad_values)):
                return {"error": "梯度下降发生数值发散", "iterations": max_iter}
            final_gradient_norm = float(np.linalg.norm(final_grad_values))
            return {
                "optimal_variables": {name: float(current[j]) for j, name in enumerate(var_names)},
                "optimal_value": optimal_value,
                "iterations": max_iter,
                "converged": final_gradient_norm < tolerance,
                "gradient_norm": final_gradient_norm,
                **(
                    {}
                    if final_gradient_norm < tolerance
                    else {"warning": "达到最大迭代次数，可能未完全收敛"}
                ),
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------ #
    #  8. 数论工具
    # ------------------------------------------------------------------ #

    def prime_factors(self, n: int) -> Dict[str, Any]:
        """质因数分解。

        Returns:
            {"factors": {质数: 指数}, "factorization": "分解字符串"}
        """
        try:
            n = _coerce_int(n, "n", 1)
            if n <= 0:
                return {"error": "请输入正整数"}
            if n == 1:
                return {"factors": {}, "factorization": "1", "n": 1}

            factors = sp.factorint(n)
            factor_str = " × ".join(
                f"{p}^{e}" if e > 1 else str(p)
                for p, e in sorted(factors.items())
            )
            return {
                "n": n,
                "factors": {int(k): int(v) for k, v in factors.items()},
                "factorization": factor_str,
                "is_prime": len(factors) == 1 and list(factors.values())[0] == 1,
            }
        except Exception as e:
            return {"error": str(e)}

    def is_prime(self, n: int) -> Dict[str, Any]:
        """判断是否为质数。"""
        try:
            n = _coerce_int(n, "n")
            return {"n": n, "is_prime": sp.isprime(n)}
        except Exception as e:
            return {"error": str(e)}

    def number_theory_info(self, n: int) -> Dict[str, Any]:
        """综合数论信息。

        Returns:
            包含质因数、欧拉函数、约数个数、约数和、莫比乌斯函数等
        """
        try:
            n = _coerce_int(n, "n", 1)
            if n <= 0:
                return {"error": "请输入正整数"}

            factors = sp.factorint(n)

            # 欧拉函数 φ(n)
            phi = n
            for p in factors:
                phi = phi * (p - 1) // p

            # 约数个数 d(n)
            divisor_count = 1
            for exp in factors.values():
                divisor_count *= (exp + 1)

            # 约数和 σ(n)
            divisor_sum = 1
            for p, e in factors.items():
                divisor_sum *= (p ** (e + 1) - 1) // (p - 1)

            # 莫比乌斯函数 μ(n)
            mobius = 0
            if all(e == 1 for e in factors.values()):
                mobius = (-1) ** len(factors)
            elif any(e > 1 for e in factors.values()):
                mobius = 0

            return {
                "n": n,
                "prime_factors": {int(k): int(v) for k, v in factors.items()},
                "is_prime": len(factors) == 1 and list(factors.values())[0] == 1,
                "euler_totient": phi,
                "divisor_count": divisor_count,
                "divisor_sum": divisor_sum,
                "mobius_function": mobius,
            }
        except Exception as e:
            return {"error": str(e)}

    def prime_range(self, start: int, end: int) -> Dict[str, Any]:
        """列出区间内的所有质数。"""
        try:
            start = _coerce_int(start, "start")
            end = _coerce_int(end, "end")
            if start > end:
                return {"error": "start 不能大于 end"}
            primes = list(sp.primerange(start, end + 1))
            return {
                "range": f"[{start}, {end}]",
                "count": len(primes),
                "primes": primes if len(primes) <= 50 else primes[:50] + ["..."],
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------ #
    #  9. 综合计算 (一次性运行多个运算)
    # ------------------------------------------------------------------ #

    def compute(self, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量计算接口 — 一次请求执行多个计算任务。

        Args:
            requests: 计算请求列表，每个请求包含：
                {
                    "id": "请求标识",
                    "type": "evaluate|diff|integrate|limit|solve|simplify|series|matrix|stats|...",
                    "params": {...}
                }

        Returns:
            {"results": {id: 结果, ...}, "summary": 汇总}
        """
        if not isinstance(requests, list):
            return {
                "error": "requests 必须是请求对象列表",
                "results": {},
                "summary": {"total": 0, "success": 0, "errors": 0},
            }

        results = {}
        errors = 0

        handlers: Dict[str, Callable] = {
            "evaluate":   lambda p: self.evaluate(
                p["expression"],
                p.get("variables"),
                p.get("high_precision", False),
                p.get("precision"),
            ),
            "diff":       lambda p: self.symbolic_diff(p["expression"], p.get("variable", "x"), p.get("order", 1)),
            "integrate":  lambda p: self.symbolic_integrate(p["expression"], p.get("variable", "x"),
                                                             p.get("definite", False), p.get("lower"), p.get("upper")),
            "limit":      lambda p: self.symbolic_limit(p["expression"], p.get("variable", "x"),
                                                         p.get("point"), p.get("direction", "+")),
            "solve":      lambda p: self.symbolic_solve(p["equation"], p.get("variable", "x"), p.get("domain", "real")),
            "simplify":   lambda p: self.symbolic_simplify(p["expression"]),
            "series":     lambda p: self.symbolic_series(p["expression"], p.get("variable", "x"),
                                                          p.get("point", 0), p.get("order", 6)),
            "matrix":     lambda p: self.matrix_operations(
                p["matrix_data"],
                p["operation"],
                p.get("kwargs", {}).get("norm_type", "fro"),
            ),
            "stats":      lambda p: self.descriptive_stats(p["data"], p.get("population", False)),
            "regression": lambda p: self.linear_regression(p["x"], p["y"]),
            "probability": lambda p: self.probability_distribution(
                p["dist_name"], p.get("params", {}), p.get("x_values"), p.get("operation", "pdf")
            ),
            "gradient":   lambda p: self.gradient_descent(
                p["func_expr"],
                p["variables"],
                p.get("learning_rate", 0.01),
                p.get("max_iter", 1000),
                p.get("tolerance", 1e-6),
            ),
            "high_precision_eval": lambda p: self.high_precision_eval(
                p["expression"], p.get("precision", self.mp_dps)
            ),
            "prime_factors": lambda p: self.prime_factors(p["n"]),
            "is_prime":   lambda p: self.is_prime(p["n"]),
            "ntheory":    lambda p: self.number_theory_info(p["n"]),
            "prime_range": lambda p: self.prime_range(p["start"], p["end"]),
            "fft":        lambda p: self.fft_analysis(p["signal"], p.get("sample_rate", 1.0)),
        }

        for index, req in enumerate(requests):
            if not isinstance(req, dict):
                results[str(index)] = {"error": "每个请求都必须是对象"}
                errors += 1
                continue

            req_id = str(req.get("id", index))
            if req_id in results:
                duplicate_id = f"{req_id}#{index}"
                results[duplicate_id] = {"error": f"重复的请求 id: {req_id}"}
                errors += 1
                continue
            req_type = req.get("type", "evaluate")
            params = req.get("params", {})
            if not isinstance(params, dict):
                results[req_id] = {"error": "params 必须是对象"}
                errors += 1
                continue

            if req_type in handlers:
                try:
                    results[req_id] = handlers[req_type](params)
                    if isinstance(results[req_id], dict) and "error" in results[req_id]:
                        errors += 1
                except Exception as e:
                    results[req_id] = {"error": str(e)}
                    errors += 1
            else:
                results[req_id] = {"error": f"未知计算类型: {req_type}",
                                   "available": list(handlers.keys())}
                errors += 1

        return {
            "results": results,
            "summary": {
                "total": len(requests),
                "success": len(requests) - errors,
                "errors": errors,
            }
        }


# Xenon dynamic tool runtime discovers classes ending in ToolManager or Manager.
class ComputationalEngineToolManager(ComputationalEngine):
    """Xenon 可动态加载的计算引擎工具入口。"""


# ============================================================================
#  辅助工具
# ============================================================================

def _is_factorable(expr) -> bool:
    """检查表达式是否可以因式分解。"""
    try:
        factored = factor(expr)
        return str(factored) != str(expr)
    except Exception:
        return False


# ============================================================================
#  演示 / 自检
# ============================================================================

def demo():
    """运行完整功能演示。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    engine = ComputationalEngine()
    print("=" * 70)
    print("  Xenon 计算引擎 — 功能演示")
    print("=" * 70)

    # 1. 安全求值
    print("\n[1] 安全表达式求值:")
    r = engine.evaluate("sqrt(3**2 + 4**2) * pi / 2")
    print(f"    sqrt(3²+4²) × π ÷ 2 = {r}")

    r = engine.evaluate("sin(pi/6) + cos(pi/3)")
    print(f"    sin(π/6) + cos(π/3) = {r}")

    # 2. 符号微分
    print("\n[2] 符号微分:")
    r = engine.symbolic_diff("x**4 + 3*x**2 + sin(x)", "x", order=2)
    print(f"    d²/dx² (x⁴+3x²+sin(x)) = {r['derivative']}")

    # 3. 符号积分
    print("\n[3] 符号积分:")
    r = engine.symbolic_integrate("exp(-x**2)", "x", definite=True, lower="-oo", upper="oo")
    print(f"    ∫₋∞^∞ e^(-x²) dx = {r['integral']}")

    # 4. 方程求解
    print("\n[4] 方程求解:")
    r = engine.symbolic_solve("x**3 - 6*x**2 + 11*x - 6 = 0")
    print(f"    x³-6x²+11x-6=0 的解: {r['solutions']}")

    # 5. 矩阵运算
    print("\n[5] 矩阵运算:")
    r = engine.matrix_eig([[1, 2], [2, 1]])
    print(f"    [[1,2],[2,1]] 特征值: {r['eigenvalues']}")

    # 6. 高精度
    print("\n[6] 高精度计算:")
    r = engine.high_precision_pi(50)
    print(f"    π (50位): {r['pi']}")

    # 7. 统计分析
    print("\n[7] 统计分析:")
    r = engine.descriptive_stats([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    print(f"    mean={r['mean']}, median={r['median']}, std={r['std']:.3f}")

    # 8. 数论
    print("\n[8] 数论:")
    r = engine.prime_factors(2024)
    print(f"    2024 = {r['factorization']}")

    # 9. 批量计算
    print("\n[9] 批量计算:")
    r = engine.compute([
        {"id": "a", "type": "evaluate", "params": {"expression": "2**10"}},
        {"id": "b", "type": "diff", "params": {"expression": "x**3", "variable": "x"}},
        {"id": "c", "type": "prime_factors", "params": {"n": 999}},
    ])
    print(f"    结果: {r['summary']}")
    for k, v in r['results'].items():
        print(f"      [{k}] {v}")

    print("\n" + "=" * 70)
    print("  演示完成 — 所有计算引擎正常工作")
    print("=" * 70)


if __name__ == "__main__":
    demo()
