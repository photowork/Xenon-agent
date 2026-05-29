#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Chart 图表生成工具
根据结构化数据生成静态图表图片文件，支持论文/报告场景
重点解决中文显示问题，输出可直接交给 Word 工具插入文档
"""

import json
import os
import sys
import time
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# --- matplotlib 检测与导入 ---
try:
    import matplotlib
    matplotlib.use("Agg")  # 非交互式后端，必须先于 pyplot 导入
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  中文字体回退逻辑
# ══════════════════════════════════════════════════════════════

# 优先尝试的中文字体列表（按顺序回退）
_CJK_FONT_CANDIDATES = [
    "Microsoft YaHei",       # 微软雅黑 (Windows)
    "SimHei",                # 黑体 (Windows)
    "SimSun",                # 宋体 (Windows)
    "Noto Sans CJK SC",     # 思源黑体 (Linux/跨平台)
    "Source Han Sans SC",    # 思源黑体 (Adobe版)
    "PingFang SC",           # 苹方 (macOS)
    "WenQuanYi Zen Hei",     # 文泉驿正黑 (Linux)
    "Arial Unicode MS",      # 跨平台备选
]

_font_initialized = False
_cjk_font_available = False
_cjk_font_warning = ""
_detected_cjk_font = None


def _init_cjk_font() -> Tuple[bool, str, Optional[str]]:
    """
    初始化中文字体设置。
    返回: (是否成功, 警告信息, 检测到的字体名)
    """
    global _font_initialized, _cjk_font_available, _cjk_font_warning, _detected_cjk_font

    if _font_initialized:
        return _cjk_font_available, _cjk_font_warning, _detected_cjk_font

    _font_initialized = True

    if not MATPLOTLIB_AVAILABLE:
        _cjk_font_warning = "matplotlib 未安装，无法设置中文字体"
        return False, _cjk_font_warning, None

    # 获取系统所有可用字体名
    available_fonts = set()
    try:
        for f in fm.fontManager.ttflist:
            available_fonts.add(f.name)
    except Exception:
        # 某些环境下 fontManager 可能异常，尝试重建
        try:
            fm._load_fontmanager(try_read_cache=False)
            for f in fm.fontManager.ttflist:
                available_fonts.add(f.name)
        except Exception as e:
            _cjk_font_warning = f"无法扫描系统字体: {e}"
            return False, _cjk_font_warning, None

    # 按优先级回退查找
    found_font = None
    for candidate in _CJK_FONT_CANDIDATES:
        if candidate in available_fonts:
            found_font = candidate
            break

    if found_font:
        plt.rcParams["font.sans-serif"] = [found_font] + plt.rcParams.get("font.sans-serif", [])
        plt.rcParams["axes.unicode_minus"] = False
        _cjk_font_available = True
        _detected_cjk_font = found_font
        logger.info(f"中文字体已设置: {found_font}")
    else:
        _cjk_font_available = False
        _cjk_font_warning = (
            "当前环境未检测到中文字体（已尝试: " +
            "、".join(_CJK_FONT_CANDIDATES) +
            "），图表中的中文标题、坐标轴、图例可能显示为方框。"
            "建议安装 Microsoft YaHei 或 SimHei 字体。"
        )
        logger.warning(_cjk_font_warning)

    return _cjk_font_available, _cjk_font_warning, _detected_cjk_font


# ══════════════════════════════════════════════════════════════
#  ChartHandler — 内部业务逻辑
# ══════════════════════════════════════════════════════════════

class ChartHandler:
    """图表处理器 — 包含具体业务逻辑"""

    SUPPORTED_CHART_TYPES = {"line", "bar", "scatter", "pie", "histogram"}
    SUPPORTED_FORMATS = {"png", "jpg", "jpeg", "svg", "pdf", "tiff", "bmp"}

    def __init__(self):
        # 每次实例化时确保字体已初始化
        _init_cjk_font()

    # ───────── 数据校验 ─────────

    @staticmethod
    def _validate_xy_series_data(data: Dict, chart_type: str) -> Dict[str, Any]:
        """校验 line/bar/scatter 数据格式"""
        if not isinstance(data, dict):
            return {"valid": False, "error": f"data 必须是字典，实际类型: {type(data).__name__}"}

        if "x" not in data:
            return {"valid": False, "error": f"{chart_type} 图表数据缺少 'x' 字段"}
        if "series" not in data:
            return {"valid": False, "error": f"{chart_type} 图表数据缺少 'series' 字段"}

        x = data["x"]
        series = data["series"]

        if not isinstance(x, list):
            return {"valid": False, "error": "'x' 必须是列表"}
        if not isinstance(series, list):
            return {"valid": False, "error": "'series' 必须是列表"}

        if len(x) == 0:
            return {"valid": False, "error": "'x' 不能为空列表"}

        for i, s in enumerate(series):
            if not isinstance(s, dict):
                return {"valid": False, "error": f"series[{i}] 必须是字典"}
            if "name" not in s:
                return {"valid": False, "error": f"series[{i}] 缺少 'name' 字段"}
            if "y" not in s:
                return {"valid": False, "error": f"series[{i}] 缺少 'y' 字段"}
            if not isinstance(s["y"], list):
                return {"valid": False, "error": f"series[{i}].y 必须是列表"}
            if len(s["y"]) != len(x):
                return {"valid": False, "error": f"series[{i}].y 长度({len(s['y'])})与 x 长度({len(x)})不一致"}

            # 校验 y 中的每个值必须是有效数值
            for j, v in enumerate(s["y"]):
                try:
                    float(v)
                except (TypeError, ValueError):
                    return {"valid": False, "error": f"series[{i}].y[{j}] = '{v}' 不是有效数值"}

        return {"valid": True}

    @staticmethod
    def _validate_pie_data(data: Dict) -> Dict[str, Any]:
        """校验 pie 数据格式"""
        if not isinstance(data, dict):
            return {"valid": False, "error": f"data 必须是字典，实际类型: {type(data).__name__}"}

        if "labels" not in data:
            return {"valid": False, "error": "饼图数据缺少 'labels' 字段"}
        if "values" not in data:
            return {"valid": False, "error": "饼图数据缺少 'values' 字段"}

        labels = data["labels"]
        values = data["values"]

        if not isinstance(labels, list):
            return {"valid": False, "error": "'labels' 必须是列表"}
        if not isinstance(values, list):
            return {"valid": False, "error": "'values' 必须是列表"}
        if len(labels) != len(values):
            return {"valid": False, "error": f"'labels' 长度({len(labels)})与 'values' 长度({len(values)})不一致"}
        if len(values) == 0:
            return {"valid": False, "error": "'values' 不能为空列表"}

        # 检查数值有效性
        for i, v in enumerate(values):
            try:
                float(v)
            except (TypeError, ValueError):
                return {"valid": False, "error": f"values[{i}] = '{v}' 不是有效数值"}

        return {"valid": True}

    @staticmethod
    def _validate_histogram_data(data: Dict) -> Dict[str, Any]:
        """校验 histogram 数据格式"""
        if not isinstance(data, dict):
            return {"valid": False, "error": f"data 必须是字典，实际类型: {type(data).__name__}"}

        if "values" not in data:
            return {"valid": False, "error": "直方图数据缺少 'values' 字段"}

        values = data["values"]
        if not isinstance(values, list):
            return {"valid": False, "error": "'values' 必须是列表"}
        if len(values) == 0:
            return {"valid": False, "error": "'values' 不能为空列表"}

        # 检查数值有效性
        for i, v in enumerate(values):
            try:
                float(v)
            except (TypeError, ValueError):
                return {"valid": False, "error": f"values[{i}] = '{v}' 不是有效数值"}

        return {"valid": True}

    def _validate_data(self, chart_type: str, data: Dict) -> Dict[str, Any]:
        """统一数据校验入口"""
        if chart_type in ("line", "bar", "scatter"):
            return self._validate_xy_series_data(data, chart_type)
        elif chart_type == "pie":
            return self._validate_pie_data(data)
        elif chart_type == "histogram":
            return self._validate_histogram_data(data)
        else:
            return {"valid": False, "error": f"不支持的图表类型: {chart_type}，支持: {', '.join(sorted(self.SUPPORTED_CHART_TYPES))}"}

    # ───────── 统一响应 ─────────

    @staticmethod
    def _format_response(success: bool, **kwargs) -> Dict[str, Any]:
        """统一返回格式"""
        result = {"success": success}
        result.update(kwargs)
        return result

    # ───────── 图表绘制 ─────────

    def _draw_line(self, ax, data: Dict, **options):
        """绘制折线图"""
        x = data["x"]
        series = data["series"]
        marker = options.get("marker", "o")
        for s in series:
            ax.plot(x, s["y"], marker=marker, label=s["name"])

    def _draw_bar(self, ax, data: Dict, **options):
        """绘制柱状图"""
        import numpy as np
        x = data["x"]
        series = data["series"]
        n_series = len(series)
        bar_width = options.get("bar_width", 0.8 / max(n_series, 1))
        x_indices = np.arange(len(x))

        for i, s in enumerate(series):
            offset = (i - n_series / 2 + 0.5) * bar_width
            ax.bar(x_indices + offset, s["y"], width=bar_width, label=s["name"])

        ax.set_xticks(x_indices)
        ax.set_xticklabels([str(v) for v in x])

    def _draw_scatter(self, ax, data: Dict, **options):
        """绘制散点图"""
        series = data["series"]
        for s in series:
            ax.scatter(data["x"], s["y"], label=s["name"], alpha=0.7)

    def _draw_pie(self, ax, data: Dict, **options):
        """绘制饼图"""
        labels = data["labels"]
        values = data["values"]
        # 处理 autopct
        autopct = options.get("autopct", "%1.1f%%")
        ax.pie(values, labels=labels, autopct=autopct, startangle=90)
        ax.axis("equal")

    def _draw_histogram(self, ax, data: Dict, **options):
        """绘制直方图"""
        values = data["values"]
        bins = data.get("bins", 10)
        ax.hist(values, bins=bins, edgecolor="black", alpha=0.7)

    # ───────── 主创建方法 ─────────

    def create_chart(
        self,
        chart_type: str,
        data: Dict,
        output_path: str,
        title: str = "",
        x_label: str = "",
        y_label: str = "",
        width: int = 8,
        height: int = 5,
        dpi: int = 300,
        grid: bool = True,
        format: str = "png",
    ) -> Dict[str, Any]:
        """
        创建图表并保存到文件

        :param chart_type: 图表类型，支持 line/bar/scatter/pie/histogram
        :param data: 图表数据，格式因图表类型而异
        :param output_path: 输出文件路径（不含扩展名时自动追加）
        :param title: 图表标题，支持中文
        :param x_label: X轴标签，支持中文
        :param y_label: Y轴标签，支持中文
        :param width: 图表宽度（英寸），默认8
        :param height: 图表高度（英寸），默认5
        :param dpi: 分辨率，默认300（适合论文/印刷）
        :param grid: 是否显示网格线，默认True
        :param format: 输出格式，支持 png/jpg/svg/pdf/tiff/bmp，默认png
        :return: 包含 success/output_path/absolute_path/chart_type/title/width/height/dpi/format/file_size/message 的字典
        """
        start_time = time.time()

        if not MATPLOTLIB_AVAILABLE:
            return self._format_response(
                False,
                error="matplotlib 未安装，请运行: pip install matplotlib",
                message="依赖缺失",
            )

        # --- 校验 chart_type ---
        chart_type = chart_type.lower().strip()
        if chart_type not in self.SUPPORTED_CHART_TYPES:
            return self._format_response(
                False,
                error=f"不支持的图表类型: '{chart_type}'，支持: {', '.join(sorted(self.SUPPORTED_CHART_TYPES))}",
                message="参数错误",
            )

        # --- 校验 format ---
        fmt = format.lower().strip()
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in self.SUPPORTED_FORMATS:
            return self._format_response(
                False,
                error=f"不支持的输出格式: '{format}'，支持: {', '.join(sorted(self.SUPPORTED_FORMATS))}",
                message="参数错误",
            )

        # --- 校验数据 ---
        validation = self._validate_data(chart_type, data)
        if not validation["valid"]:
            return self._format_response(False, error=validation["error"], message="数据校验失败")

        # --- 处理输出路径 ---
        output = Path(output_path)
        # 如果用户没给扩展名，自动追加
        if output.suffix.lower().lstrip(".") not in self.SUPPORTED_FORMATS:
            ext = "png" if fmt == "png" else fmt
            output = output.with_suffix(f".{ext}")

        # 自动创建输出目录
        output.parent.mkdir(parents=True, exist_ok=True)

        # --- 绘图 ---
        try:
            fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)

            draw_methods = {
                "line": self._draw_line,
                "bar": self._draw_bar,
                "scatter": self._draw_scatter,
                "pie": self._draw_pie,
                "histogram": self._draw_histogram,
            }

            draw_methods[chart_type](ax, data)

            # 设置标题、轴标签
            if title:
                ax.set_title(title, fontsize=14, fontweight="bold")
            if x_label and chart_type != "pie":
                ax.set_xlabel(x_label, fontsize=12)
            if y_label and chart_type != "pie":
                ax.set_ylabel(y_label, fontsize=12)

            # 图例（pie 图用 labels 不用 legend，除非多 series）
            if chart_type != "pie" and len(data.get("series", [])) > 1:
                ax.legend(loc="best", fontsize=10)

            # 网格
            if grid and chart_type != "pie":
                ax.grid(True, alpha=0.3, linestyle="--")

            # 紧凑布局
            fig.tight_layout()

            # 保存
            fig.savefig(str(output), format=fmt, dpi=dpi, bbox_inches="tight")
            plt.close(fig)

        except Exception as e:
            plt.close("all")
            return self._format_response(
                False,
                error=f"图表生成失败: {str(e)}",
                message="绘图异常",
            )

        # --- 构建返回结果 ---
        absolute_path = str(output.resolve())
        file_size = output.stat().st_size if output.exists() else 0
        elapsed = round(time.time() - start_time, 3)

        result = self._format_response(
            True,
            output_path=str(output),
            absolute_path=absolute_path,
            chart_type=chart_type,
            title=title,
            width=width,
            height=height,
            dpi=dpi,
            format=fmt,
            file_size=file_size,
            message=f"图表已生成: {output.name} ({file_size} bytes, {elapsed}s)",
        )

        # 中文字体警告
        cjk_ok, cjk_warn, _ = _init_cjk_font()
        if not cjk_ok:
            result["warning"] = cjk_warn

        return result

    def create_chart_for_word(
        self,
        chart_type: str,
        data: Dict,
        output_path: str,
        title: str = "",
        x_label: str = "",
        y_label: str = "",
        width: int = 8,
        height: int = 5,
        dpi: int = 300,
        grid: bool = True,
        format: str = "png",
    ) -> Dict[str, Any]:
        """
        生成图表并返回可直接交给 Word 插图工具使用的结果

        :param chart_type: 图表类型，支持 line/bar/scatter/pie/histogram
        :param data: 图表数据
        :param output_path: 输出文件路径
        :param title: 图表标题
        :param x_label: X轴标签
        :param y_label: Y轴标签
        :param width: 图表宽度（英寸）
        :param height: 图表高度（英寸）
        :param dpi: 分辨率
        :param grid: 是否显示网格
        :param format: 输出格式，默认png（Word兼容性最好）
        :return: 包含 chart_path/absolute_path/file_size/chart_type/title/suggested_word_action 的字典
        """
        # 先生成图表
        chart_result = self.create_chart(
            chart_type=chart_type,
            data=data,
            output_path=output_path,
            title=title,
            x_label=x_label,
            y_label=y_label,
            width=width,
            height=height,
            dpi=dpi,
            grid=grid,
            format=format,
        )

        if not chart_result.get("success"):
            return chart_result

        # 附加 Word 相关信息
        chart_result["chart_path"] = chart_result["output_path"]

        # width 英寸 → 磅（1 英寸 = 72 磅），限制不超过 A4 可打印宽度
        width_emu = min(width, 6.0) * 72

        chart_result["suggested_word_action"] = {
            "tool": "word_handler_Word_insert_picture_with_wrap",
            "arguments": {
                "word_path": "<目标Word文档路径>",
                "image_path": chart_result["absolute_path"],
                "wrap_type": "嵌入型",
                "width": width_emu,
            },
            "description": (
                "可调用 word_handler_Word_insert_picture_with_wrap 工具，"
                f"传入 image_path='{chart_result['absolute_path']}'，"
                f"wrap_type='嵌入型'，width={width_emu} "
                "将此图表插入 Word 文档。"
                "word_path 需替换为实际目标文档路径。"
            ),
        }

        # 如果有标题，额外推荐带题注版本
        if title:
            chart_result["suggested_word_action_with_caption"] = {
                "tool": "word_handler_Word_insert_picture_with_caption",
                "arguments": {
                    "word_path": "<目标Word文档路径>",
                    "image_path": chart_result["absolute_path"],
                    "caption_text": title,
                    "wrap_type": "嵌入型",
                    "width": width_emu,
                },
                "description": (
                    "可调用 word_handler_Word_insert_picture_with_caption 工具，"
                    f"传入 image_path='{chart_result['absolute_path']}'，"
                    f"caption_text='{title}'，wrap_type='嵌入型'，width={width_emu} "
                    "将此图表插入 Word 文档并自动添加题注。"
                    "word_path 需替换为实际目标文档路径。"
                ),
            }

        return chart_result

    def create_multiple_charts(
        self,
        charts: List[Dict],
        output_dir: str,
    ) -> Dict[str, Any]:
        """
        批量生成多个图表

        :param charts: 图表配置列表，每个元素为含 chart_type/data/output_path/title 等键的字典
        :param output_dir: 输出目录（所有图表保存到此目录下）
        :return: 包含 results(各图表结果)/success_count/fail_count 的字典
        """
        if not isinstance(charts, list):
            return self._format_response(False, error="charts 必须是列表", message="参数错误")

        if len(charts) == 0:
            return self._format_response(False, error="charts 不能为空列表", message="参数错误")

        # 确保输出目录存在
        dir_path = Path(output_dir)
        dir_path.mkdir(parents=True, exist_ok=True)

        results = []
        success_count = 0
        fail_count = 0

        for idx, chart_cfg in enumerate(charts):
            if not isinstance(chart_cfg, dict):
                results.append(self._format_response(False, error=f"charts[{idx}] 必须是字典"))
                fail_count += 1
                continue

            # 提取参数
            ct = chart_cfg.get("chart_type", "")
            d = chart_cfg.get("data", {})
            filename = chart_cfg.get("output_path", f"chart_{idx + 1}")
            t = chart_cfg.get("title", "")
            xl = chart_cfg.get("x_label", "")
            yl = chart_cfg.get("y_label", "")
            w = chart_cfg.get("width", 8)
            h = chart_cfg.get("height", 5)
            d_val = chart_cfg.get("dpi", 300)
            g = chart_cfg.get("grid", True)
            f = chart_cfg.get("format", "png")

            # 拼接完整输出路径
            full_path = str(dir_path / filename)

            result = self.create_chart(
                chart_type=ct,
                data=d,
                output_path=full_path,
                title=t,
                x_label=xl,
                y_label=yl,
                width=w,
                height=h,
                dpi=d_val,
                grid=g,
                format=f,
            )

            results.append(result)

            if result.get("success"):
                success_count += 1
            else:
                fail_count += 1

        overall_success = fail_count == 0
        return self._format_response(
            overall_success,
            results=results,
            success_count=success_count,
            fail_count=fail_count,
            total=len(charts),
            output_dir=str(dir_path.resolve()),
            message=f"批量生成完成: {success_count} 成功, {fail_count} 失败",
        )

    def suggest_chart_type(self, data: Dict) -> Dict[str, Any]:
        """
        根据数据结构自动推荐最合适的图表类型

        :param data: 待可视化的数据
        :return: 包含 suggested_type/reason/all_compatible_types 的字典
        """
        if not isinstance(data, dict):
            return self._format_response(
                False,
                error=f"data 必须是字典，实际类型: {type(data).__name__}",
            )

        compatible = []
        reason_parts = []

        # 检测 line/bar/scatter 格式
        has_x = "x" in data and isinstance(data.get("x"), list)
        has_series = "series" in data and isinstance(data.get("series"), list)

        if has_x and has_series:
            series = data["series"]
            if len(series) > 0 and all("y" in s for s in series):
                compatible.extend(["line", "bar", "scatter"])
                n_series = len(series)
                if n_series == 1:
                    reason_parts.append(f"单系列数据({n_series}组)，适合展示趋势或对比")
                else:
                    reason_parts.append(f"多系列数据({n_series}组)，适合对比展示")

                # 进一步细化推荐
                x = data["x"]
                if all(isinstance(v, (int, float)) for v in x if v is not None):
                    if n_series <= 2:
                        compatible.insert(0, "scatter")  # 数值X轴优先散点
                        reason_parts.append("X轴为连续数值，散点图可展示相关性")
                    else:
                        compatible.insert(0, "line")
                        reason_parts.append("多系列连续数据，折线图更适合趋势对比")
                else:
                    compatible.insert(0, "bar")
                    reason_parts.append("X轴为分类数据，柱状图更适合分类对比")

        # 检测 pie 格式
        has_labels = "labels" in data and isinstance(data.get("labels"), list)
        has_values = "values" in data and isinstance(data.get("values"), list)

        if has_labels and has_values and len(data.get("labels", [])) == len(data.get("values", [])):
            n_items = len(data["labels"])
            if n_items <= 10:
                compatible.append("pie")
                reason_parts.append(f"包含标签和数值({n_items}项)，适合饼图展示占比")
            else:
                reason_parts.append(f"数据项过多({n_items}项)，饼图可读性差，不推荐")

        # 检测 histogram 格式
        has_hist_values = "values" in data and isinstance(data.get("values"), list)
        if has_hist_values:
            vals = data["values"]
            if len(vals) > 0 and all(isinstance(v, (int, float)) for v in vals if v is not None):
                compatible.append("histogram")
                if not (has_x and has_series):
                    reason_parts.append("单组连续数值数据，直方图可展示分布特征")
                    compatible.insert(0, "histogram")

        if not compatible:
            return self._format_response(
                False,
                error="无法识别数据格式。line/bar/scatter 需要 {x:[], series:[{name:'', y:[]}]}；pie 需要 {labels:[], values:[]}；histogram 需要 {values:[], bins:N}",
                message="数据格式不匹配任何已知图表类型",
            )

        # 去重并保持顺序
        seen = set()
        unique_compatible = []
        for ct in compatible:
            if ct not in seen:
                seen.add(ct)
                unique_compatible.append(ct)

        suggested = unique_compatible[0]
        reason = "；".join(reason_parts) if reason_parts else "基于数据结构分析"

        return self._format_response(
            True,
            suggested_type=suggested,
            reason=reason,
            all_compatible_types=unique_compatible,
        )


# ══════════════════════════════════════════════════════════════
#  ChartToolManager — 对外管理器（自动发现注册）
# ══════════════════════════════════════════════════════════════

class ChartToolManager:
    """图表工具管理器 — Xenon 自动发现注册入口"""

    def __init__(self):
        self._handler = ChartHandler()

    def create_chart(
        self,
        chart_type: str,
        data: Dict,
        output_path: str,
        title: str = "",
        x_label: str = "",
        y_label: str = "",
        width: int = 8,
        height: int = 5,
        dpi: int = 300,
        grid: bool = True,
        format: str = "png",
    ) -> Dict[str, Any]:
        """
        创建图表并保存到文件。支持 line/bar/scatter/pie/histogram 五种类型。

        :param chart_type: 图表类型，支持 line/bar/scatter/pie/histogram
        :param data: 图表数据。line/bar/scatter格式: {"x":[...], "series":[{"name":"A","y":[...]}]}；pie格式: {"labels":[...], "values":[...]}；histogram格式: {"values":[...], "bins":10}
        :param output_path: 输出文件路径，不含扩展名时自动追加
        :param title: 图表标题，支持中文
        :param x_label: X轴标签，支持中文
        :param y_label: Y轴标签，支持中文
        :param width: 图表宽度（英寸），默认8
        :param height: 图表高度（英寸），默认5
        :param dpi: 分辨率，默认300（适合论文印刷）
        :param grid: 是否显示网格线，默认True
        :param format: 输出格式，支持 png/jpg/svg/pdf/tiff/bmp，默认png
        """
        return self._handler.create_chart(
            chart_type=chart_type,
            data=data,
            output_path=output_path,
            title=title,
            x_label=x_label,
            y_label=y_label,
            width=width,
            height=height,
            dpi=dpi,
            grid=grid,
            format=format,
        )

    def create_chart_for_word(
        self,
        chart_type: str,
        data: Dict,
        output_path: str,
        title: str = "",
        x_label: str = "",
        y_label: str = "",
        width: int = 8,
        height: int = 5,
        dpi: int = 300,
        grid: bool = True,
        format: str = "png",
    ) -> Dict[str, Any]:
        """
        生成图表并返回可直接交给 Word 插图工具使用的结果。额外返回 chart_path、absolute_path、file_size、suggested_word_action 等字段。

        :param chart_type: 图表类型，支持 line/bar/scatter/pie/histogram
        :param data: 图表数据
        :param output_path: 输出文件路径
        :param title: 图表标题
        :param x_label: X轴标签
        :param y_label: Y轴标签
        :param width: 图表宽度（英寸）
        :param height: 图表高度（英寸）
        :param dpi: 分辨率
        :param grid: 是否显示网格
        :param format: 输出格式，默认png（Word兼容性最好）
        """
        return self._handler.create_chart_for_word(
            chart_type=chart_type,
            data=data,
            output_path=output_path,
            title=title,
            x_label=x_label,
            y_label=y_label,
            width=width,
            height=height,
            dpi=dpi,
            grid=grid,
            format=format,
        )

    def create_multiple_charts(
        self,
        charts: List[Dict],
        output_dir: str,
    ) -> Dict[str, Any]:
        """
        批量生成多个图表。每个图表配置为字典，含 chart_type/data/output_path/title 等键。

        :param charts: 图表配置列表，每个元素为字典，如 [{"chart_type":"line", "data":{...}, "output_path":"chart1", "title":"趋势图"}]
        :param output_dir: 输出目录，所有图表保存到此目录下
        """
        return self._handler.create_multiple_charts(charts=charts, output_dir=output_dir)

    def suggest_chart_type(self, data: Dict) -> Dict[str, Any]:
        """
        根据数据结构自动推荐最合适的图表类型。返回建议类型、推荐理由、所有兼容类型。

        :param data: 待可视化的数据，格式与 create_chart 的 data 参数相同
        """
        return self._handler.suggest_chart_type(data=data)


# ══════════════════════════════════════════════════════════════
#  CLI 入口（独立测试用）
# ══════════════════════════════════════════════════════════════

def main():
    """命令行入口，通过 STDIN 传入 JSON 参数"""
    if not MATPLOTLIB_AVAILABLE:
        print(json.dumps({"success": False, "error": "matplotlib 未安装"}, ensure_ascii=False))
        sys.exit(1)

    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "用法: python chart_handler.py <action> [json_args或STDIN]"}, ensure_ascii=False))
        sys.exit(1)

    action = sys.argv[1]
    manager = ChartToolManager()

    # 获取参数
    input_json = {}
    if not sys.stdin.isatty():
        try:
            input_json = json.loads(sys.stdin.read())
        except Exception:
            pass
    elif len(sys.argv) > 2:
        try:
            input_json = json.loads(sys.argv[2])
        except Exception:
            pass

    try:
        if action == "create":
            result = manager.create_chart(
                chart_type=input_json.get("chart_type", ""),
                data=input_json.get("data", {}),
                output_path=input_json.get("output_path", "chart_output"),
                title=input_json.get("title", ""),
                x_label=input_json.get("x_label", ""),
                y_label=input_json.get("y_label", ""),
                width=input_json.get("width", 8),
                height=input_json.get("height", 5),
                dpi=input_json.get("dpi", 300),
                grid=input_json.get("grid", True),
                format=input_json.get("format", "png"),
            )
        elif action == "create_for_word":
            result = manager.create_chart_for_word(
                chart_type=input_json.get("chart_type", ""),
                data=input_json.get("data", {}),
                output_path=input_json.get("output_path", "chart_output"),
                title=input_json.get("title", ""),
                x_label=input_json.get("x_label", ""),
                y_label=input_json.get("y_label", ""),
                width=input_json.get("width", 8),
                height=input_json.get("height", 5),
                dpi=input_json.get("dpi", 300),
                grid=input_json.get("grid", True),
                format=input_json.get("format", "png"),
            )
        elif action == "suggest":
            result = manager.suggest_chart_type(data=input_json.get("data", {}))
        elif action == "batch":
            result = manager.create_multiple_charts(
                charts=input_json.get("charts", []),
                output_dir=input_json.get("output_dir", "charts_output"),
            )
        else:
            result = {"success": False, "error": f"未知操作: {action}，支持: create/create_for_word/suggest/batch"}

        print(json.dumps(result, ensure_ascii=False, default=str))

    except Exception as e:
        print(json.dumps({"success": False, "error": f"执行异常: {str(e)}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
