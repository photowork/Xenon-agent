#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Xenon 仿真/模拟环境 — 完整的单文件工具实现

从跨域映射验证尾翼升级为通用仿真框架，提供：
- 系统动力学建模（ODE / 差分 / 元胞自动机）
- 时间步进求解器（Euler / RK4）
- 参数扫描与灵敏度分析
- 稳定性与收敛性验证
- 负样本蒙特卡洛检验（跨域映射专用，保持向后兼容）

工具清单:
    list_simulation_models / build_custom_model
    run_ode_simulation / run_discrete_simulation / run_cellular_automaton
    run_parameter_sweep / run_multi_parameter_sweep
    run_sensitivity_analysis / run_convergence_analysis
    run_difference_dynamics / run_null_model_test
    run_perturbation_stability_test / validate_cross_domain_candidate
"""

from __future__ import annotations

import ast
import itertools
import math
import random
import statistics
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# ═══════════════════════════════════════════════════════════════════════
#  第1部分：工具函数
# ═══════════════════════════════════════════════════════════════════════

StateVector = List[float]
SystemDynamics = Callable[[float, StateVector, Dict[str, float]], StateVector]

_CUSTOM_FUNCS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "exp": math.exp,
    "log": math.log,
    "sqrt": math.sqrt,
    "abs": abs,
    "pow": pow,
}
_CUSTOM_CONSTANTS = {"pi": math.pi, "e": math.e}


def _safe_eval_expression(expr: str, env: Dict[str, float]) -> float:
    tree = ast.parse(expr, mode="eval")

    def eval_node(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in env:
                return float(env[node.id])
            raise NameError(f"未知变量: {node.id}")
        if isinstance(node, ast.UnaryOp):
            value = eval_node(node.operand)
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.USub):
                return -value
        if isinstance(node, ast.BinOp):
            left = eval_node(node.left)
            right = eval_node(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                return left ** right
            if isinstance(node.op, ast.Mod):
                return left % right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.keywords:
                raise ValueError("自定义方程不支持关键字参数")
            fn = _CUSTOM_FUNCS.get(node.func.id)
            if fn is None:
                raise NameError(f"未知函数: {node.func.id}")
            return float(fn(*(eval_node(arg) for arg in node.args)))
        raise ValueError(f"不支持的表达式: {ast.dump(node, include_attributes=False)}")

    return float(eval_node(tree))


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return round(ordered[index], 4)


def _summary_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "mean": round(statistics.fmean(values), 4),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 4),
    }


def _round_finite(value: Any, digits: int = 6) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number):
        return str(number)
    return round(number, digits)


def _compact_state(values: List[float]) -> List[Any]:
    return [_round_finite(value) for value in values]


def _sample_indices(length: int, limit: int) -> List[int]:
    if length <= 0 or limit <= 0:
        return []
    if length <= limit:
        return list(range(length))
    if limit == 1:
        return [length - 1]
    return sorted({round(i * (length - 1) / (limit - 1)) for i in range(limit)})


def _summarize_variable_series(
    times: List[float],
    series: List[float],
) -> Dict[str, Any]:
    finite_values = [float(value) for value in series if math.isfinite(float(value))]
    if not series or not finite_values:
        return {
            "points": len(series),
            "finite_points": len(finite_values),
            "start": None,
            "final": None,
            "min": None,
            "max": None,
            "mean": None,
            "amplitude": None,
        }

    min_value = min(finite_values)
    max_value = max(finite_values)
    summary: Dict[str, Any] = {
        "points": len(series),
        "finite_points": len(finite_values),
        "start": _round_finite(series[0]),
        "final": _round_finite(series[-1]),
        "min": _round_finite(min_value),
        "max": _round_finite(max_value),
        "mean": _round_finite(statistics.fmean(finite_values)),
        "amplitude": _round_finite(max_value - min_value),
    }

    if times and len(times) == len(series):
        try:
            min_index = min(range(len(series)), key=lambda i: series[i])
            max_index = max(range(len(series)), key=lambda i: series[i])
            summary["min_time"] = _round_finite(times[min_index])
            summary["max_time"] = _round_finite(times[max_index])
        except Exception:
            pass
    return summary


def _build_ode_summary(
    result: Dict[str, object],
    *,
    model_name: str,
    params: Dict[str, float],
    initial_state: List[float],
    variable_names: List[str],
    equations: Optional[List[str]] = None,
    include_series: bool = False,
    max_series_points: int = 25,
) -> Dict[str, Any]:
    times = list(result.get("t") or [])
    variables = result.get("variables") or {}
    max_series_points = max(1, min(50, int(max_series_points or 25)))

    variable_summaries: Dict[str, Any] = {}
    for index, name in enumerate(variable_names):
        series = list(variables.get(f"x{index}", [])) if isinstance(variables, dict) else []
        variable_summaries[name] = _summarize_variable_series(times, series)

    summary: Dict[str, Any] = {
        "success": bool(result.get("success")),
        "model_name": model_name,
        "solver": result.get("solver"),
        "dt": result.get("dt"),
        "steps": result.get("steps"),
        "t_span": result.get("t_span"),
        "n_vars": result.get("n_vars"),
        "variable_names": list(variable_names),
        "initial_state": _compact_state(initial_state),
        "final_state": _compact_state(list(result.get("final_state") or [])),
        "params": {key: _round_finite(value) for key, value in params.items()},
        "variable_summaries": variable_summaries,
        "series_points_summarized": len(times),
        "series_returned": "omitted",
        "output_mode": "summary",
        "message": "ODE simulation completed; raw time-series omitted to keep context small.",
    }
    if equations:
        summary["equations"] = list(equations)

    if include_series:
        indices = _sample_indices(len(times), max_series_points)
        points = []
        for idx in indices:
            state = {}
            for var_index, name in enumerate(variable_names):
                series = variables.get(f"x{var_index}", []) if isinstance(variables, dict) else []
                if idx < len(series):
                    state[name] = _round_finite(series[idx])
            points.append({"t": _round_finite(times[idx]), "state": state})
        summary["sampled_series"] = {
            "points": points,
            "returned_points": len(points),
            "max_returned_points": max_series_points,
            "sampled_from_points": len(times),
        }
        summary["series_returned"] = "sampled"

    return summary


def _tool_error(exc: Exception) -> Dict[str, Any]:
    return {"success": False, "error": str(exc)}


def _coerce_seeds(seeds: Any) -> List[int]:
    if isinstance(seeds, str):
        parts = [p.strip() for p in seeds.replace("，", ",").split(",") if p.strip()]
        values = [int(p) for p in parts]
    elif isinstance(seeds, (list, tuple)):
        values = [int(v) for v in seeds]
    else:
        raise ValueError("seeds 必须是整数列表或逗号分隔字符串")
    result = sorted(set(v for v in values if v > 0))
    if len(result) < 2:
        raise ValueError("seeds 至少需要 2 个正整数")
    if len(result) > 20:
        raise ValueError("seeds 不能超过 20 个，避免仿真成本失控")
    return result


def _safe_trials(value: int, default: int, maximum: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = default
    return max(1, min(maximum, n))


def _random_seed_set(rng: random.Random, seeds: List[int]) -> List[int]:
    count = len(seeds)
    lo, hi = min(seeds), max(seeds)
    if hi - lo + 1 >= count:
        return sorted(rng.sample(range(lo, hi + 1), count))
    result: set = set()
    while len(result) < count:
        result.add(max(1, rng.randint(lo, hi)))
    return sorted(result)


# ═══════════════════════════════════════════════════════════════════════
#  第2部分：求解器
# ═══════════════════════════════════════════════════════════════════════

def _euler_step(
    f: SystemDynamics, t: float, x: StateVector, dt: float, params: Dict[str, float],
) -> StateVector:
    dx = f(t, x, params)
    return [x[i] + dt * dx[i] for i in range(len(x))]


def _rk4_step(
    f: SystemDynamics, t: float, x: StateVector, dt: float, params: Dict[str, float],
) -> StateVector:
    n = len(x)
    k1 = f(t, x, params)
    x2 = [x[i] + 0.5 * dt * k1[i] for i in range(n)]
    k2 = f(t + 0.5 * dt, x2, params)
    x3 = [x[i] + 0.5 * dt * k2[i] for i in range(n)]
    k3 = f(t + 0.5 * dt, x3, params)
    x4 = [x[i] + dt * k3[i] for i in range(n)]
    k4 = f(t + dt, x4, params)
    return [x[i] + (dt / 6.0) * (k1[i] + 2.0 * k2[i] + 2.0 * k3[i] + k4[i]) for i in range(n)]


_SOLVERS = {"euler": _euler_step, "rk4": _rk4_step}


def _integrate_ode(
    f: SystemDynamics,
    x0: StateVector,
    t_span: Tuple[float, float],
    dt: float,
    params: Optional[Dict[str, float]] = None,
    solver: str = "rk4",
    max_steps: int = 100_000,
) -> Dict[str, object]:
    step_fn = _SOLVERS.get(solver)
    if step_fn is None:
        raise ValueError(f"未知求解器: {solver}，可选: {list(_SOLVERS.keys())}")
    dt = float(dt)
    if not math.isfinite(dt) or abs(dt) <= 0.0:
        raise ValueError("dt 必须是非零有限数")
    max_steps = max(0, int(max_steps))
    params = params or {}
    t0, t_end = t_span
    t0, t_end = float(t0), float(t_end)
    dt = abs(dt) if t_end >= t0 else -abs(dt)
    n_vars = len(x0)
    times: List[float] = [t0]
    trajectory: List[StateVector] = [list(x0)]
    t, x = t0, list(x0)
    steps = 0

    while (t_end >= t0 and t + dt <= t_end) or (t_end < t0 and t + dt >= t_end):
        if steps >= max_steps:
            break
        try:
            x = step_fn(f, t, x, dt, params)
        except (OverflowError, ValueError, ZeroDivisionError):
            break
        t += dt
        times.append(t)
        trajectory.append(list(x))
        steps += 1

    n_points = len(times)
    if n_points > 2000:
        step = n_points // 2000
        indices = sorted(set([0] + list(range(step, n_points - 1, step)) + [n_points - 1]))
        times_ds = [times[i] for i in indices]
        var_series = {f"x{vi}": [trajectory[i][vi] for i in indices] for vi in range(n_vars)}
    else:
        times_ds = times
        var_series = {f"x{vi}": [trajectory[i][vi] for i in range(n_points)] for vi in range(n_vars)}

    return {
        "success": True, "solver": solver, "dt": abs(dt), "steps": steps,
        "t_span": [t0, t_end], "t": times_ds, "variables": var_series,
        "final_state": list(trajectory[-1]) if trajectory else list(x0), "n_vars": n_vars,
    }


def _iterate_discrete(
    f: Callable[[StateVector, Dict[str, float]], StateVector],
    x0: StateVector,
    n_steps: int,
    params: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    n_steps = int(n_steps)
    if n_steps < 0:
        raise ValueError("n_steps 必须大于等于 0")
    params = params or {}
    x = list(x0)
    trajectory: List[StateVector] = [list(x)]
    n_vars = len(x0)
    for _ in range(n_steps):
        try:
            x = f(x, params)
        except (OverflowError, ValueError, ZeroDivisionError):
            break
        trajectory.append(list(x))
        if all(math.isnan(v) or math.isinf(v) for v in x):
            break
    n_points = len(trajectory)
    if n_points > 2000:
        step = n_points // 2000
        indices = sorted(set([0] + list(range(step, n_points - 1, step)) + [n_points - 1]))
        var_series = {f"x{vi}": [trajectory[i][vi] for i in indices] for vi in range(n_vars)}
    else:
        var_series = {f"x{vi}": [trajectory[i][vi] for i in range(n_points)] for vi in range(n_vars)}
    return {"success": True, "solver": "discrete_map", "steps": len(trajectory) - 1,
            "variables": var_series, "final_state": list(trajectory[-1]) if trajectory else list(x0), "n_vars": n_vars}


_WOLFRAM_RULES: Dict[int, List[int]] = {}
for _r in range(256):
    _WOLFRAM_RULES[_r] = [(_r >> b) & 1 for b in range(8)]


def _elementary_ca(rule: int, width: int, steps: int, init: Optional[str] = None) -> Dict[str, object]:
    rule = int(rule)
    width = int(width)
    steps = int(steps)
    if not (0 <= rule <= 255):
        raise ValueError("rule 必须在 0-255 之间")
    if width <= 0:
        raise ValueError("width 必须大于 0")
    if steps < 0:
        raise ValueError("steps 必须大于等于 0")
    rule_bits = _WOLFRAM_RULES[rule]
    if init is not None:
        init = init.strip()
        if any(c not in {"0", "1"} for c in init):
            raise ValueError("init 只能包含 0 和 1")
        cells = [1 if c == "1" else 0 for c in init]
        if len(cells) != width:
            raise ValueError(f"init 长度 {len(cells)} 与 width {width} 不符")
    else:
        cells = [0] * width
        cells[width // 2] = 1
    history = ["".join(str(c) for c in cells)]
    for _ in range(steps):
        new_cells = [0] * width
        for i in range(width):
            left = cells[i - 1] if i > 0 else cells[-1]
            center = cells[i]
            right = cells[i + 1] if i < width - 1 else cells[0]
            idx = (left << 2) | (center << 1) | right
            new_cells[i] = rule_bits[idx]
        cells = new_cells
        history.append("".join(str(c) for c in cells))
    ones_per_step = [row.count("1") for row in history]
    return {"success": True, "rule": rule, "width": width, "steps": steps,
            "history": history, "ones_per_step": ones_per_step,
            "final_config": history[-1], "density_final": ones_per_step[-1] / width if width > 0 else 0.0}


# ═══════════════════════════════════════════════════════════════════════
#  第3部分：预置模型
# ═══════════════════════════════════════════════════════════════════════

def _logistic_ode(t: float, x: StateVector, p: Dict[str, float]) -> StateVector:
    r = p.get("r", 1.0)
    k = p.get("K", 1.0)
    return [r * x[0] * (1.0 - x[0] / k)]


def _lotka_volterra(t: float, x: StateVector, p: Dict[str, float]) -> StateVector:
    alpha = p.get("alpha", 0.1)
    beta = p.get("beta", 0.02)
    gamma = p.get("gamma", 0.1)
    delta = p.get("delta", 0.01)
    return [alpha * x[0] - beta * x[0] * x[1], delta * x[0] * x[1] - gamma * x[1]]


def _sir_ode(t: float, x: StateVector, p: Dict[str, float]) -> StateVector:
    beta = p.get("beta", 0.3)
    gamma = p.get("gamma", 0.1)
    n = p.get("N", sum(x)) or 1.0
    s, i, r = x[0], x[1], x[2]
    return [-beta * s * i / n, beta * s * i / n - gamma * i, gamma * i]


def _lorenz_ode(t: float, x: StateVector, p: Dict[str, float]) -> StateVector:
    sigma = p.get("sigma", 10.0)
    rho = p.get("rho", 28.0)
    beta = p.get("beta", 8.0 / 3.0)
    return [sigma * (x[1] - x[0]), x[0] * (rho - x[2]) - x[1], x[0] * x[1] - beta * x[2]]


def _neural_population(t: float, x: StateVector, p: Dict[str, float]) -> StateVector:
    w_ee, w_ei = p.get("w_ee", 10.0), p.get("w_ei", 5.0)
    w_ie, w_ii = p.get("w_ie", 8.0), p.get("w_ii", 4.0)
    theta_e, theta_i = p.get("theta_e", 0.0), p.get("theta_i", 0.0)

    def sigmoid(u: float) -> float:
        return 1.0 / (1.0 + math.exp(-max(min(u, 50.0), -50.0)))

    e, i = x[0], x[1]
    return [-e + sigmoid(w_ee * e - w_ei * i + theta_e), -i + sigmoid(w_ie * e - w_ii * i + theta_i)]


def _logistic_map(x: StateVector, p: Dict[str, float]) -> StateVector:
    return [p.get("r", 3.8) * x[0] * (1.0 - x[0])]


def _henon_map(x: StateVector, p: Dict[str, float]) -> StateVector:
    a, b = p.get("a", 1.4), p.get("b", 0.3)
    return [x[1] - a * x[0] * x[0], b * x[0]]


ODE_MODELS: Dict[str, Dict] = {
    "logistic_growth": {"fn": _logistic_ode, "description": "单变量 Logistic 增长 dx/dt = r·x·(1-x/K)",
                        "params": {"r": 1.0, "K": 1.0}, "n_vars": 1, "default_x0": [0.1]},
    "lotka_volterra": {"fn": _lotka_volterra, "description": "捕食者-猎物双变量振荡系统",
                       "params": {"alpha": 0.1, "beta": 0.02, "gamma": 0.1, "delta": 0.01},
                       "n_vars": 2, "default_x0": [40.0, 9.0]},
    "sir_epidemic": {"fn": _sir_ode, "description": "SIR 流行病模型 (S→I→R)",
                     "params": {"beta": 0.3, "gamma": 0.1, "N": 1000.0},
                     "n_vars": 3, "default_x0": [990.0, 10.0, 0.0]},
    "lorenz_chaos": {"fn": _lorenz_ode, "description": "Lorenz 混沌系统 (σ=10, ρ=28, β=8/3)",
                     "params": {"sigma": 10.0, "rho": 28.0, "beta": 8.0 / 3.0},
                     "n_vars": 3, "default_x0": [1.0, 1.0, 1.0]},
    "neural_population": {"fn": _neural_population, "description": "双神经元 Wilson-Cowan 平均场模型",
                          "params": {"w_ee": 10.0, "w_ei": 5.0, "w_ie": 8.0, "w_ii": 4.0, "theta_e": 0.0, "theta_i": 0.0},
                          "n_vars": 2, "default_x0": [0.1, 0.1]},
}

DISCRETE_MODELS: Dict[str, Dict] = {
    "logistic_map": {"fn": _logistic_map, "description": "Logistic Map x_{n+1}=r·x·(1-x)",
                     "params": {"r": 3.8}, "n_vars": 1, "default_x0": [0.5]},
    "henon_map": {"fn": _henon_map, "description": "Henon Map (a=1.4, b=0.3)",
                  "params": {"a": 1.4, "b": 0.3}, "n_vars": 2, "default_x0": [0.0, 0.0]},
}


def _list_models() -> Dict[str, List[Dict[str, object]]]:
    return {
        "ode_models": [{"name": n, "description": m["description"], "params": dict(m["params"]),
                        "n_vars": m["n_vars"], "default_x0": list(m["default_x0"])} for n, m in ODE_MODELS.items()],
        "discrete_models": [{"name": n, "description": m["description"], "params": dict(m["params"]),
                             "n_vars": m["n_vars"], "default_x0": list(m["default_x0"])} for n, m in DISCRETE_MODELS.items()],
    }


# ═══════════════════════════════════════════════════════════════════════
#  第4部分：分析工具
# ═══════════════════════════════════════════════════════════════════════

def _parameter_sweep(
    model_fn: SystemDynamics, x0: StateVector, t_span: Tuple[float, float], dt: float,
    param_name: str, param_values: List[float], fixed_params: Optional[Dict[str, float]] = None,
    solver: str = "rk4", measure: str = "final", var_index: int = 0,
) -> Dict[str, Any]:
    fixed = dict(fixed_params or {})
    results: List[Dict[str, float]] = []
    for val in param_values:
        p = dict(fixed)
        p[param_name] = val
        sim = _integrate_ode(model_fn, x0, t_span, dt, p, solver)
        series = sim["variables"].get(f"x{var_index}", [])
        if not series:
            results.append({param_name: val, "measure": 0.0, "status": "failed"})
            continue
        if measure == "final":
            m = series[-1]
        elif measure == "max":
            m = max(series)
        elif measure == "min":
            m = min(series)
        elif measure == "mean":
            m = statistics.fmean(series) if len(series) > 1 else series[0]
        elif measure == "amplitude":
            m = max(series) - min(series) if len(series) > 1 else 0.0
        else:
            m = series[-1]
        results.append({param_name: val, "measure": round(m, 6), "status": "ok"})
    return {"success": True, "param_name": param_name, "param_values": param_values,
            "measure": measure, "var_index": var_index, "results": results}


def _multi_parameter_sweep(
    model_fn: SystemDynamics, x0: StateVector, t_span: Tuple[float, float], dt: float,
    param_grid: Dict[str, List[float]], fixed_params: Optional[Dict[str, float]] = None,
    solver: str = "rk4", measure: str = "final", var_index: int = 0, max_combinations: int = 500,
) -> Dict[str, Any]:
    keys = list(param_grid.keys())
    values_lists = [param_grid[k] for k in keys]
    total = 1
    for vl in values_lists:
        total *= len(vl)
    if total > max_combinations:
        return {"success": False, "error": f"参数组合数 {total} 超过上限 {max_combinations}",
                "param_grid": param_grid, "total_combinations": total}
    fixed = dict(fixed_params or {})
    all_results: List[Dict[str, float]] = []
    for combo in itertools.product(*values_lists):
        p = dict(fixed)
        combo_dict: Dict[str, float] = {}
        for i, k in enumerate(keys):
            p[k] = combo[i]
            combo_dict[k] = combo[i]
        sim = _integrate_ode(model_fn, x0, t_span, dt, p, solver)
        series = sim["variables"].get(f"x{var_index}", [])
        if not series:
            combo_dict["measure"] = 0.0
            combo_dict["status"] = "failed"
        else:
            if measure == "final":
                m = series[-1]
            elif measure == "max":
                m = max(series)
            elif measure == "min":
                m = min(series)
            elif measure == "mean":
                m = statistics.fmean(series) if len(series) > 1 else series[0]
            elif measure == "amplitude":
                m = max(series) - min(series) if len(series) > 1 else 0.0
            else:
                m = series[-1]
            combo_dict["measure"] = round(m, 6)
            combo_dict["status"] = "ok"
        all_results.append(combo_dict)
    return {"success": True, "param_grid": param_grid, "combinations": len(all_results),
            "measure": measure, "var_index": var_index, "results": all_results}


def _local_sensitivity(
    model_fn: SystemDynamics, x0: StateVector, t_span: Tuple[float, float], dt: float,
    params: Dict[str, float], param_names: Optional[List[str]] = None,
    epsilon: float = 0.01, solver: str = "rk4", var_index: int = 0,
) -> Dict[str, Any]:
    if param_names is None:
        param_names = list(params.keys())
    base = _integrate_ode(model_fn, x0, t_span, dt, params, solver)
    baseline = base["variables"].get(f"x{var_index}", [])
    if not baseline:
        return {"success": False, "error": "基准仿真失败"}
    baseline_final = baseline[-1]
    sensitivities: List[Dict[str, float]] = []
    for name in param_names:
        if name not in params:
            continue
        p0 = params[name]
        delta = epsilon if abs(p0) < 1e-15 else epsilon * abs(p0)
        p_plus = dict(params)
        p_plus[name] = p0 + delta
        sp = _integrate_ode(model_fn, x0, t_span, dt, p_plus, solver)
        ps = sp["variables"].get(f"x{var_index}", [])
        plus_val = ps[-1] if ps else baseline_final
        p_minus = dict(params)
        p_minus[name] = p0 - delta
        sm = _integrate_ode(model_fn, x0, t_span, dt, p_minus, solver)
        ms = sm["variables"].get(f"x{var_index}", [])
        minus_val = ms[-1] if ms else baseline_final
        sens = (plus_val - minus_val) / (2.0 * delta) if abs(2.0 * delta) > 1e-30 else 0.0
        elasticity = sens * p0 / baseline_final if abs(baseline_final) > 1e-15 and abs(p0) > 1e-15 else 0.0
        sensitivities.append({"param": name, "baseline_value": p0, "sensitivity": round(sens, 6),
                              "elasticity": round(elasticity, 6), "delta": round(delta, 8)})
    sensitivities.sort(key=lambda s: abs(s["sensitivity"]), reverse=True)
    return {"success": True, "solver": solver, "epsilon": epsilon,
            "baseline_final": round(baseline_final, 6), "var_index": var_index,
            "sensitivities": sensitivities}


def _convergence_analysis(
    model_fn: SystemDynamics, x0: StateVector, t_span: Tuple[float, float],
    params: Optional[Dict[str, float]] = None, dt_values: Optional[List[float]] = None,
    var_index: int = 0,
) -> Dict[str, Any]:
    if dt_values is None:
        dt_values = [0.1, 0.05, 0.02, 0.01, 0.005]
    params = params or {}
    convergence: List[Dict[str, float]] = []
    for dt in dt_values:
        sim = _integrate_ode(model_fn, x0, t_span, dt, params, "rk4")
        series = sim["variables"].get(f"x{var_index}", [])
        final = series[-1] if series else 0.0
        convergence.append({"dt": dt, "final_value": round(final, 6), "steps": sim["steps"]})
    ratios = []
    for i in range(2, len(convergence)):
        denom = abs(convergence[i - 1]["final_value"] - convergence[i - 2]["final_value"])
        if denom > 1e-15:
            ratio = abs(convergence[i]["final_value"] - convergence[i - 1]["final_value"]) / denom
            ratios.append(round(ratio, 4))
        else:
            ratios.append(0.0)
    return {"success": True, "t_span": list(t_span), "var_index": var_index,
            "convergence": convergence, "convergence_rate_estimates": ratios,
            "converged": max(ratios[-3:]) < 1.0 if len(ratios) >= 3 else None}


# ═══════════════════════════════════════════════════════════════════════
#  第5部分：跨域映射验证
# ═══════════════════════════════════════════════════════════════════════

_has_mapper = False
_CrossDomainMapper = None  # type: ignore
try:
    from Tools.cross_domain_mapping.engine import CrossDomainMapper as _CrossDomainMapper
    _has_mapper = True
except (ImportError, ModuleNotFoundError):
    pass


class CrossDomainValidator:
    """跨域映射结构的统计验证器。"""

    def __init__(self):
        if not _has_mapper:
            raise RuntimeError("cross_domain_mapping 模块不可用")
        self.mapper = _CrossDomainMapper()

    def _mapping_observation(self, seeds: List[int], target_domain: Optional[str] = None) -> Dict[str, Any]:
        report = self.mapper.map(seeds)
        matches = report.top_matches or []
        if target_domain:
            match = next((m for m in matches if m.get("domain") == target_domain), None)
        else:
            match = matches[0] if matches else None
        confidence = float(match.get("confidence", 0.0)) if match else 0.0
        diversity = float(match.get("evidence_diversity", 0.0)) if match else 0.0
        return {
            "status": report.status, "encoding": report.encoding_used, "seeds": list(report.seeds),
            "domain": match.get("domain") if match else None,
            "domain_label": match.get("domain_label") if match else None,
            "confidence": round(confidence, 4), "evidence_diversity": round(diversity, 4),
            "verification_score": round(float(report.verification.get("score", 0.0)), 4) if report.verification else 0.0,
            "nontriviality": round(float(report.meta.get("nontriviality", 0.0)), 4) if report.meta else 0.0,
            "top_matches": [{"domain": m.get("domain"), "label": m.get("domain_label"),
                             "confidence": m.get("confidence"), "diversity": m.get("evidence_diversity"),
                             "type": m.get("match_type")} for m in matches[:5]],
        }

    def run_difference_dynamics(self, seeds: List[int], max_rounds: int = 12) -> Dict[str, Any]:
        values = _coerce_seeds(seeds)
        rounds = _safe_trials(max_rounds, 12, 50)
        seq = sorted(set(values))
        path_summary = [seq[:12]]
        converged, point = False, None
        for r in range(rounds):
            if len(seq) <= 1:
                converged = True
                point = seq[0] if seq else None
                return {"success": True, "converged": converged, "point": point, "rounds": r, "path_summary": path_summary[:6]}
            seq = sorted(set(abs(b - a) for a, b in zip(seq, seq[1:])))
            seq = [x for x in seq if x > 0]
            if len(path_summary) < 6:
                path_summary.append(seq[:12])
            if not seq:
                break
        if len(seq) == 1:
            converged, point = True, seq[0]
        return {"success": True, "converged": converged, "point": point,
                "rounds": min(rounds, len(path_summary) - 1), "path_summary": path_summary[:6],
                "final_sequence_sample": seq[:12]}

    def run_null_model_test(self, seeds: List[int], trials: int = 200, random_seed: int = 42, target_domain: str = "") -> Dict[str, Any]:
        values = _coerce_seeds(seeds)
        n = _safe_trials(trials, 200, 2000)
        rng = random.Random(int(random_seed))
        domain = target_domain.strip() or None
        observed = self._mapping_observation(values, domain)
        null_scores: List[float] = []
        null_diversities: List[float] = []
        null_status_counts: Dict[str, int] = {}
        exceed = 0
        for _ in range(n):
            sample = _random_seed_set(rng, values)
            obs = self._mapping_observation(sample, domain)
            score = float(obs["confidence"])
            diversity = float(obs["evidence_diversity"])
            null_scores.append(score)
            null_diversities.append(diversity)
            null_status_counts[obs["status"]] = null_status_counts.get(obs["status"], 0) + 1
            if score >= observed["confidence"] and diversity >= observed["evidence_diversity"]:
                exceed += 1
        empirical_p = (exceed + 1) / (n + 1)
        return {"success": True, "test": "null_model_monte_carlo", "observed": observed,
                "null_trials": n, "empirical_p_value": round(empirical_p, 4),
                "null_confidence": _summary_stats(null_scores), "null_diversity": _summary_stats(null_diversities),
                "null_status_counts": null_status_counts,
                "interpretation": "候选明显强于随机负样本" if empirical_p <= 0.05 else
                "候选未明显强于随机负样本" if empirical_p > 0.2 else "候选略强于随机负样本，仍需更多验证"}

    def run_perturbation_stability_test(self, seeds: List[int], trials: int = 100, perturbation_radius: int = 3,
                                        random_seed: int = 42, target_domain: str = "") -> Dict[str, Any]:
        values = _coerce_seeds(seeds)
        n = _safe_trials(trials, 100, 1000)
        radius = max(0, min(1000, int(perturbation_radius)))
        rng = random.Random(int(random_seed))
        domain = target_domain.strip() or None
        observed = self._mapping_observation(values, domain)
        observed_domain = observed["domain"]
        same_domain, non_rejected = 0, 0
        scores: List[float] = []
        examples: List[List[int]] = []
        for _ in range(n):
            perturbed = sorted(set(max(1, v + rng.randint(-radius, radius)) for v in values))
            while len(perturbed) < len(values):
                perturbed.append(max(1, values[len(perturbed)] + rng.randint(-radius, radius)))
                perturbed = sorted(set(perturbed))
            obs = self._mapping_observation(perturbed, domain)
            scores.append(float(obs["confidence"]))
            if obs["status"] != "no_structure_found":
                non_rejected += 1
            if observed_domain and obs["domain"] == observed_domain:
                same_domain += 1
            if len(examples) < 5:
                examples.append(perturbed)
        sr = same_domain / n
        nr = non_rejected / n
        return {"success": True, "test": "perturbation_stability", "observed": observed,
                "trials": n, "perturbation_radius": radius, "same_domain_rate": round(sr, 4),
                "non_rejected_rate": round(nr, 4), "confidence_under_perturbation": _summary_stats(scores),
                "sampled_perturbations": examples,
                "interpretation": "候选对局部扰动稳定" if sr >= 0.6 and nr >= 0.6 else "候选对局部扰动不稳定，可能依赖精确数值巧合"}

    def validate_cross_domain_candidate(self, seeds: List[int], target_domain: str = "",
                                        null_trials: int = 200, perturbation_trials: int = 100,
                                        perturbation_radius: int = 3, random_seed: int = 42) -> Dict[str, Any]:
        values = _coerce_seeds(seeds)
        domain = target_domain.strip()
        dynamics = self.run_difference_dynamics(values)
        null_result = self.run_null_model_test(values, trials=null_trials, random_seed=random_seed, target_domain=domain)
        stability = self.run_perturbation_stability_test(values, trials=perturbation_trials,
                                                         perturbation_radius=perturbation_radius,
                                                         random_seed=random_seed + 17, target_domain=domain)
        observed = null_result["observed"]
        p_value = null_result["empirical_p_value"]
        stable = stability["same_domain_rate"]
        non_rejected = stability["non_rejected_rate"]
        confidence = observed["confidence"]
        diversity = observed["evidence_diversity"]

        if observed["status"] == "no_structure_found":
            verdict, reason = "rejected", "跨域映射引擎本身未发现可靠候选"
        elif p_value <= 0.05 and stable >= 0.6 and non_rejected >= 0.6:
            verdict, reason = "simulation_supported", "候选强于随机负样本，并且对局部扰动稳定"
        elif p_value <= 0.2 and confidence >= 0.34 and diversity >= 0.5:
            verdict, reason = "provisional", "候选有一定统计优势，但稳定性或显著性仍不足"
        else:
            verdict, reason = "not_supported", "候选未通过负样本或稳定性检验"

        return {"success": True, "tool": "simulation_validator", "seeds": values,
                "target_domain": domain or observed.get("domain"), "verdict": verdict, "reason": reason,
                "observed": observed, "difference_dynamics": dynamics,
                "null_model": {"trials": null_result["null_trials"], "empirical_p_value": p_value,
                               "null_confidence": null_result["null_confidence"],
                               "null_status_counts": null_result["null_status_counts"]},
                "perturbation_stability": {"trials": stability["trials"], "same_domain_rate": stable,
                                           "non_rejected_rate": non_rejected,
                                           "confidence_under_perturbation": stability["confidence_under_perturbation"]},
                "next_steps": ["若 verdict 为 simulation_supported，可进入文献检索和因果机制验证",
                               "若 verdict 为 provisional，增加负样本次数或限定目标域后重测",
                               "若 verdict 为 not_supported/rejected，应降低候选优先级"]}


# ═══════════════════════════════════════════════════════════════════════
#  第6部分：主管理器类
# ═══════════════════════════════════════════════════════════════════════

try:
    _validator_instance = CrossDomainValidator()
except (ImportError, RuntimeError):
    _validator_instance = None


class SimulationValidatorToolManager:
    """仿真与模拟环境工具管理器

    工具发现机制自动开放本类的公开方法为 Xenon 工具。
    保持与旧版 simulation_validator.py 完全向后兼容。
    """

    # ── 模型管理 ────────────────────────────────────────────────────

    @staticmethod
    def list_simulation_models() -> Dict[str, Any]:
        """列出所有可用的预置仿真模型。"""
        return _list_models()

    @staticmethod
    def build_custom_model(equations: List[str], var_names: Optional[List[str]] = None) -> Dict[str, Any]:
        """从数学表达式构建自定义 ODE 模型。"""
        try:
            if not isinstance(equations, (list, tuple)) or not equations:
                return {"success": False, "error": "equations 必须是非空字符串列表"}
            equations = [eq.strip() for eq in equations if isinstance(eq, str) and eq.strip()]
            if not equations:
                return {"success": False, "error": "equations 必须包含至少一个非空表达式"}
            n = len(equations)
            vnames = var_names or [f"x{i}" for i in range(n)]
            if not isinstance(vnames, (list, tuple)) or not all(isinstance(name, str) for name in vnames):
                return {"success": False, "error": "var_names 必须是字符串列表"}
            if len(vnames) != n:
                return {"success": False, "error": "var_names 长度必须与 equations 一致"}
            reserved_names = set(_CUSTOM_FUNCS) | set(_CUSTOM_CONSTANTS) | {"t"}
            if len(set(vnames)) != len(vnames):
                return {"success": False, "error": "var_names 不能重复"}
            invalid_names = [name for name in vnames if not name.isidentifier() or name in reserved_names]
            if invalid_names:
                return {"success": False, "error": f"非法变量名: {invalid_names}"}

            def _custom_fn(t: float, x: StateVector, p: Dict[str, float]) -> StateVector:
                try:
                    env = dict(_CUSTOM_CONSTANTS)
                    env["t"] = float(t)
                    env.update({k: float(v) for k, v in p.items()})
                    for i, name in enumerate(vnames):
                        env[name] = float(x[i])
                    return [_safe_eval_expression(eq, env) for eq in equations]
                except Exception as e:
                    raise RuntimeError(f"模型求值失败: {e}") from e

            return {"success": True, "n_vars": n, "equations": equations, "var_names": vnames,
                    "fn": _custom_fn, "note": "用 run_ode_simulation(model_name='custom', equations=...) 调用"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── ODE 仿真 ────────────────────────────────────────────────────

    def run_ode_simulation(
        self,
        model_name: str = "lotka_volterra",
        x0: Optional[List[float]] = None,
        t_start: float = 0.0,
        t_end: float = 100.0,
        dt: float = 0.1,
        params: Optional[Dict[str, float]] = None,
        solver: str = "rk4",
        equations: Optional[List[str]] = None,
        var_names: Optional[List[str]] = None,
        max_steps: int = 100_000,
        include_series: bool = False,
        max_series_points: int = 25,
    ) -> Dict[str, Any]:
        """ODE 系统时间积分仿真。

        :param model_name: 预置模型名或 'custom'
        :param x0: 初始状态向量
        :param t_start: 起始时间
        :param t_end: 结束时间
        :param dt: 时间步长
        :param params: 参数字典（覆盖预置默认值）
        :param solver: 求解器 'euler' | 'rk4'
        :param equations: model_name='custom' 时必须传入表达式列表
        :param var_names: 自定义变量名
        :param max_steps: 最大步数保护
        """
        if model_name == "custom":
            if not equations:
                return {"success": False, "error": "自定义模型必须提供 equations 参数"}
            try:
                r = self.build_custom_model(equations, var_names)
                if not r.get("success"):
                    return r
                fn = r["fn"]
                n_vars = r["n_vars"]
            except Exception as e:
                return {"success": False, "error": f"模型构建失败: {e}"}
        else:
            meta = ODE_MODELS.get(model_name)
            if meta is None:
                return {"success": False, "error": f"未知模型 '{model_name}'。可用: {list(ODE_MODELS.keys())}"}
            fn = meta["fn"]
            n_vars = meta["n_vars"]

        if x0 is None:
            if model_name != "custom":
                x0 = list(ODE_MODELS[model_name]["default_x0"])
            else:
                x0 = [0.1] * n_vars
        if len(x0) != n_vars:
            return {"success": False, "error": f"x0 长度 {len(x0)} 与模型变量数 {n_vars} 不匹配"}

        merged_params = dict(params or {})
        if model_name != "custom" and model_name in ODE_MODELS:
            for k, v in ODE_MODELS[model_name]["params"].items():
                merged_params.setdefault(k, v)

        try:
            result = _integrate_ode(fn, list(x0), (t_start, t_end), dt, merged_params, solver, max_steps)
        except Exception as e:
            return _tool_error(e)
        resolved_var_names = list(var_names or [f"x{i}" for i in range(n_vars)])
        if len(resolved_var_names) != n_vars:
            resolved_var_names = [f"x{i}" for i in range(n_vars)]
        return _build_ode_summary(
            result,
            model_name=model_name,
            params=merged_params,
            initial_state=list(x0),
            variable_names=resolved_var_names,
            equations=equations if model_name == "custom" else None,
            include_series=include_series,
            max_series_points=max_series_points,
        )

    # ── 离散仿真 ────────────────────────────────────────────────────

    def run_discrete_simulation(
        self,
        model_name: str = "logistic_map",
        x0: Optional[List[float]] = None,
        n_steps: int = 100,
        params: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """离散映射迭代仿真。"""
        meta = DISCRETE_MODELS.get(model_name)
        if meta is None:
            return {"success": False, "error": f"未知离散模型 '{model_name}'。可用: {list(DISCRETE_MODELS.keys())}"}
        if x0 is None:
            x0 = list(meta["default_x0"])
        if len(x0) != meta["n_vars"]:
            return {"success": False, "error": f"x0 长度 {len(x0)} 与模型变量数 {meta['n_vars']} 不匹配"}
        merged_params = dict(meta["params"])
        merged_params.update(params or {})
        try:
            result = _iterate_discrete(meta["fn"], list(x0), n_steps, merged_params)
        except Exception as e:
            return _tool_error(e)
        result["model_name"] = model_name
        result["params"] = merged_params
        result["initial_state"] = list(x0)
        return result

    # ── 元胞自动机 ──────────────────────────────────────────────────

    @staticmethod
    def run_cellular_automaton(rule: int = 110, width: int = 50, steps: int = 50, init: Optional[str] = None) -> Dict[str, Any]:
        """运行 1D 初等元胞自动机 (Wolfram 规则 0-255)。"""
        try:
            return _elementary_ca(rule, width, steps, init)
        except Exception as e:
            return _tool_error(e)

    # ── 参数扫描 ────────────────────────────────────────────────────

    def run_parameter_sweep(
        self,
        model_name: str = "logistic_growth",
        param_name: str = "r",
        param_values: Optional[List[float]] = None,
        x0: Optional[List[float]] = None,
        t_start: float = 0.0,
        t_end: float = 100.0,
        dt: float = 0.1,
        params: Optional[Dict[str, float]] = None,
        solver: str = "rk4",
        measure: str = "final",
        var_index: int = 0,
    ) -> Dict[str, Any]:
        """单参数扫描。"""
        meta = ODE_MODELS.get(model_name)
        if meta is None:
            return {"success": False, "error": f"未知模型 '{model_name}'。可用: {list(ODE_MODELS.keys())}"}
        if x0 is None:
            x0 = list(meta["default_x0"])
        merged_params = dict(meta["params"])
        merged_params.update(params or {})
        if param_values is None:
            base_val = merged_params.get(param_name, 1.0)
            span = max(abs(base_val) * 0.5, 0.1)
            param_values = [round(base_val - span + (2 * span * i) / 9, 4) for i in range(10)]
        try:
            return _parameter_sweep(meta["fn"], list(x0), (t_start, t_end), dt,
                                    param_name, param_values, merged_params, solver, measure, var_index)
        except Exception as e:
            return _tool_error(e)

    def run_multi_parameter_sweep(
        self,
        model_name: str = "lotka_volterra",
        param_grid: Optional[Dict[str, List[float]]] = None,
        x0: Optional[List[float]] = None,
        t_start: float = 0.0,
        t_end: float = 100.0,
        dt: float = 0.1,
        params: Optional[Dict[str, float]] = None,
        solver: str = "rk4",
        measure: str = "final",
        var_index: int = 0,
        max_combinations: int = 500,
    ) -> Dict[str, Any]:
        """多参数网格扫描。"""
        meta = ODE_MODELS.get(model_name)
        if meta is None:
            return {"success": False, "error": f"未知模型 '{model_name}'。可用: {list(ODE_MODELS.keys())}"}
        if x0 is None:
            x0 = list(meta["default_x0"])
        merged_params = dict(meta["params"])
        merged_params.update(params or {})
        if param_grid is None:
            return {"success": False, "error": "必须提供 param_grid"}
        try:
            return _multi_parameter_sweep(meta["fn"], list(x0), (t_start, t_end), dt, param_grid,
                                          merged_params, solver, measure, var_index, max_combinations)
        except Exception as e:
            return _tool_error(e)

    # ── 灵敏度分析 ──────────────────────────────────────────────────

    def run_sensitivity_analysis(
        self,
        model_name: str = "lotka_volterra",
        x0: Optional[List[float]] = None,
        t_start: float = 0.0,
        t_end: float = 100.0,
        dt: float = 0.1,
        params: Optional[Dict[str, float]] = None,
        param_names: Optional[List[str]] = None,
        epsilon: float = 0.01,
        solver: str = "rk4",
        var_index: int = 0,
    ) -> Dict[str, Any]:
        """局部灵敏度分析（有限差分法）。"""
        meta = ODE_MODELS.get(model_name)
        if meta is None:
            return {"success": False, "error": f"未知模型 '{model_name}'。可用: {list(ODE_MODELS.keys())}"}
        if x0 is None:
            x0 = list(meta["default_x0"])
        merged_params = dict(meta["params"])
        merged_params.update(params or {})
        try:
            return _local_sensitivity(meta["fn"], list(x0), (t_start, t_end), dt,
                                      merged_params, param_names, epsilon, solver, var_index)
        except Exception as e:
            return _tool_error(e)

    # ── 收敛性分析 ──────────────────────────────────────────────────

    def run_convergence_analysis(
        self,
        model_name: str = "lotka_volterra",
        x0: Optional[List[float]] = None,
        t_start: float = 0.0,
        t_end: float = 100.0,
        params: Optional[Dict[str, float]] = None,
        dt_values: Optional[List[float]] = None,
        var_index: int = 0,
    ) -> Dict[str, Any]:
        """时间步长收敛性分析。"""
        meta = ODE_MODELS.get(model_name)
        if meta is None:
            return {"success": False, "error": f"未知模型 '{model_name}'。可用: {list(ODE_MODELS.keys())}"}
        if x0 is None:
            x0 = list(meta["default_x0"])
        merged_params = dict(meta["params"])
        merged_params.update(params or {})
        try:
            return _convergence_analysis(meta["fn"], list(x0), (t_start, t_end), merged_params, dt_values, var_index)
        except Exception as e:
            return _tool_error(e)

    # ── 跨域映射验证（旧版向后兼容）───────────────────────────────

    def run_difference_dynamics(self, seeds: List[int], max_rounds: int = 12) -> Dict[str, Any]:
        """迭代绝对差分动力学（跨域映射验证用）。"""
        if _validator_instance is None:
            return {"success": False, "error": "cross_domain_mapping 不可用"}
        try:
            return _validator_instance.run_difference_dynamics(seeds, max_rounds)
        except Exception as e:
            return _tool_error(e)

    def run_null_model_test(self, seeds: List[int], trials: int = 200, random_seed: int = 42, target_domain: str = "") -> Dict[str, Any]:
        """负样本蒙特卡洛检验。"""
        if _validator_instance is None:
            return {"success": False, "error": "cross_domain_mapping 不可用"}
        try:
            return _validator_instance.run_null_model_test(seeds, trials, random_seed, target_domain)
        except Exception as e:
            return _tool_error(e)

    def run_perturbation_stability_test(self, seeds: List[int], trials: int = 100, perturbation_radius: int = 3,
                                        random_seed: int = 42, target_domain: str = "") -> Dict[str, Any]:
        """局部扰动稳定性检验。"""
        if _validator_instance is None:
            return {"success": False, "error": "cross_domain_mapping 不可用"}
        try:
            return _validator_instance.run_perturbation_stability_test(seeds, trials, perturbation_radius, random_seed, target_domain)
        except Exception as e:
            return _tool_error(e)

    def validate_cross_domain_candidate(self, seeds: List[int], target_domain: str = "",
                                        null_trials: int = 200, perturbation_trials: int = 100,
                                        perturbation_radius: int = 3, random_seed: int = 42) -> Dict[str, Any]:
        """一键验证跨域映射候选（完全向后兼容）。"""
        if _validator_instance is None:
            return {"success": False, "error": "cross_domain_mapping 不可用"}
        try:
            return _validator_instance.validate_cross_domain_candidate(seeds, target_domain, null_trials,
                                                                       perturbation_trials, perturbation_radius, random_seed)
        except Exception as e:
            return _tool_error(e)
