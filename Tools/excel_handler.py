import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional

import pandas as pd

try:
    import numpy as np
except ImportError:
    np = None


NUMPY_INTEGER_TYPES = (np.integer,) if np is not None else ()
NUMPY_FLOAT_TYPES = (np.floating,) if np is not None else ()
NUMPY_BOOL_TYPES = (np.bool_,) if np is not None else ()


class ExcelHandler:
    def __init__(self, file_path: Optional[str]):
        self.file_path = file_path
        self._excel_file = None
        self._cached_data: Dict[str, pd.DataFrame] = {}
        self.sheet_names: List[str] = []
        self._load_error: str = ""

        if self.file_path:
            self._refresh_excel_file()

    def _close_excel_file(self) -> None:
        if self._excel_file is None:
            return

        close = getattr(self._excel_file, "close", None)
        if callable(close):
            close()
        self._excel_file = None

    def _refresh_excel_file(self) -> None:
        self._close_excel_file()
        self.sheet_names = []
        self._load_error = ""

        if not self.file_path or not os.path.exists(self.file_path):
            return

        try:
            self._excel_file = pd.ExcelFile(self.file_path)
            self.sheet_names = self._excel_file.sheet_names
        except Exception as exc:
            self._load_error = str(exc)

    def _normalize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.columns = df.columns.astype(str).str.strip()
        return df

    def _build_dataframe(
        self,
        rows: Optional[Any] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        normalized_columns = [str(col).strip() for col in (columns or [])]

        if rows is None:
            rows = []
        elif isinstance(rows, dict):
            rows = [rows]
        elif not isinstance(rows, list):
            raise ValueError("rows 必须是列表、字典或为空")

        if rows:
            df = pd.DataFrame(rows)
            df = self._normalize_df(df)
            if normalized_columns:
                for col in normalized_columns:
                    if col not in df.columns:
                        df[col] = None
                extra_cols = [col for col in df.columns if col not in normalized_columns]
                df = df[normalized_columns + extra_cols]
        else:
            df = pd.DataFrame(columns=normalized_columns)

        return self._normalize_df(df)

    def _get_sheet_df(self, sheet_name: str) -> pd.DataFrame:
        """获取工作表数据，支持懒加载和缓存。"""
        if not sheet_name:
            raise ValueError("缺少 sheet 参数")

        if sheet_name in self._cached_data:
            return self._cached_data[sheet_name]

        if sheet_name not in self.sheet_names:
            raise ValueError(f"工作表 '{sheet_name}' 不存在")

        df = pd.read_excel(self.file_path, sheet_name=sheet_name)
        self._cached_data[sheet_name] = self._normalize_df(df)
        return self._cached_data[sheet_name]

    def _load_all_sheets_for_save(self) -> Dict[str, pd.DataFrame]:
        sheets_to_save: Dict[str, pd.DataFrame] = {}

        if self.file_path and os.path.exists(self.file_path):
            if not self.sheet_names:
                self._refresh_excel_file()

            for sheet_name in self.sheet_names:
                if sheet_name in self._cached_data:
                    sheets_to_save[sheet_name] = self._cached_data[sheet_name]
                else:
                    df = pd.read_excel(self.file_path, sheet_name=sheet_name)
                    sheets_to_save[sheet_name] = self._normalize_df(df)

        for sheet_name, df in self._cached_data.items():
            sheets_to_save[sheet_name] = self._normalize_df(df)

        return sheets_to_save

    def _clean_record(self, record: Any) -> Any:
        """
        递归清理数据，将 Pandas/Numpy 类型转换为 Python 原生类型，
        并处理 NaN/NaT，确保可以 json.dumps。
        """
        if isinstance(record, dict):
            return {k: self._clean_record(v) for k, v in record.items()}
        if isinstance(record, list):
            return [self._clean_record(v) for v in record]
        if isinstance(record, tuple):
            return [self._clean_record(v) for v in record]
        if isinstance(record, pd.Timestamp):
            return record.isoformat()
        if isinstance(record, NUMPY_BOOL_TYPES):
            return bool(record)
        if isinstance(record, bool):
            return record
        if isinstance(record, NUMPY_INTEGER_TYPES):
            return int(record)
        if isinstance(record, NUMPY_FLOAT_TYPES):
            if math.isnan(float(record)):
                return None
            return float(record)
        if isinstance(record, float):
            if math.isnan(record):
                return None
            return record
        if isinstance(record, int):
            return int(record)
        if pd.isna(record):
            return None
        return record

    def _format_response(
        self,
        success: bool,
        data: Any = None,
        message: str = "",
        error: str = "",
        execution_time: float = 0.0,
    ) -> Dict[str, Any]:
        """统一的响应格式。"""
        return {
            "success": success,
            "data": self._clean_record(data) if data is not None else None,
            "message": message,
            "error": error,
            "execution_time": round(execution_time, 4),
        }

    def _merge_save_result(self, result: Dict[str, Any], save_result: Dict[str, Any]) -> Dict[str, Any]:
        base_message = result.get("message", "")
        save_message = save_result.get("message", "")

        if save_result.get("success"):
            combined = " | ".join(part for part in [base_message, f"保存状态: {save_message}"] if part)
            result["message"] = combined
            return result

        result["success"] = False
        result["error"] = save_result.get("error", "保存失败")
        if base_message:
            result["message"] = f"{base_message} | 自动保存失败"
        else:
            result["message"] = "自动保存失败"
        return result

    def load_excel_info(self) -> Dict[str, Any]:
        """仅加载文件元信息，不加载全部数据。"""
        start_time = time.time()
        try:
            if self.sheet_names:
                return self._format_response(
                    True,
                    data={
                        "sheets": self.sheet_names,
                        "sheet_count": len(self.sheet_names),
                    },
                    message=f"文件就绪，包含 {len(self.sheet_names)} 个工作表。",
                    execution_time=time.time() - start_time,
                )

            error = self._load_error or "文件加载失败或格式不支持"
            return self._format_response(False, error=error, execution_time=time.time() - start_time)
        except Exception as exc:
            return self._format_response(False, error=str(exc), execution_time=time.time() - start_time)

    def get_sheet_data(
        self,
        sheet_name: str,
        start_row: int = 0,
        limit: int = 1000,
        filters: Optional[Dict[str, Any]] = None,
        columns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """读取工作表数据，支持过滤和分页。"""
        start_time = time.time()
        try:
            start_row = max(0, int(start_row))
            limit = max(1, int(limit))

            df = self._get_sheet_df(sheet_name).copy()
            total_rows = len(df)

            if filters:
                for col, condition in filters.items():
                    if col not in df.columns:
                        continue

                    if isinstance(condition, dict):
                        if "eq" in condition:
                            df = df[df[col] == condition["eq"]]
                        elif "ne" in condition:
                            df = df[df[col] != condition["ne"]]
                        elif "gt" in condition:
                            df = df[df[col] > condition["gt"]]
                        elif "lt" in condition:
                            df = df[df[col] < condition["lt"]]
                        elif "gte" in condition:
                            df = df[df[col] >= condition["gte"]]
                        elif "lte" in condition:
                            df = df[df[col] <= condition["lte"]]
                        elif "contains" in condition:
                            pattern = "" if condition["contains"] is None else str(condition["contains"])
                            df = df[df[col].astype(str).str.contains(pattern, case=False, na=False, regex=False)]
                    else:
                        df = df[df[col] == condition]

            filtered_count = len(df)

            if columns:
                if isinstance(columns, str):
                    columns = [columns]
                requested_cols = [str(c).strip() for c in columns]
                valid_cols = [c for c in requested_cols if c in df.columns]
                if not valid_cols:
                    return self._format_response(False, error="指定的列均不存在")
                df = df[valid_cols]

            end_row = start_row + limit
            df_slice = df.iloc[start_row:end_row]

            return self._format_response(
                True,
                data={
                    "total_rows": total_rows,
                    "filtered_rows": filtered_count,
                    "returned_rows": len(df_slice),
                    "headers": list(df_slice.columns),
                    "rows": df_slice.to_dict(orient="records"),
                    "page_info": {
                        "current": start_row,
                        "next": end_row if end_row < filtered_count else None,
                        "has_more": end_row < filtered_count,
                    },
                },
                execution_time=time.time() - start_time,
            )
        except Exception as exc:
            return self._format_response(
                False,
                error=f"读取数据失败: {str(exc)}",
                execution_time=time.time() - start_time,
            )

    def update_records(self, sheet_name: str, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        批量更新支持。
        updates: [{"index": 0, "data": {"Col1": "NewVal"}}, ...]
        """
        start_time = time.time()
        try:
            if isinstance(updates, dict):
                updates = [updates]
            if not isinstance(updates, list):
                return self._format_response(False, error="updates 必须是列表或字典", execution_time=time.time() - start_time)

            df = self._get_sheet_df(sheet_name)
            modified_count = 0
            skipped: List[Dict[str, Any]] = []

            for item in updates:
                if not isinstance(item, dict):
                    skipped.append({"reason": "update 项不是字典", "item": item})
                    continue

                idx = item.get("index")
                data = item.get("data")
                if idx is None or not data:
                    skipped.append({"reason": "缺少 index 或 data", "item": item})
                    continue

                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    skipped.append({"reason": "index 不是有效整数", "index": idx})
                    continue

                if not isinstance(data, dict):
                    skipped.append({"reason": "data 必须是字典", "index": idx})
                    continue

                if 0 <= idx < len(df):
                    for col, val in data.items():
                        if col in df.columns:
                            df.at[idx, col] = val
                            modified_count += 1
                        else:
                            skipped.append({"reason": "列不存在", "index": idx, "column": col})
                else:
                    skipped.append({"reason": "index 超出范围", "index": idx})
                    print(f"Warning: Index {idx} out of bounds", file=sys.stderr)

            self._cached_data[sheet_name] = df
            return self._format_response(
                True,
                data={"modified_cells": modified_count, "skipped": skipped},
                message=f"已在内存中修改 {modified_count} 个单元格。请调用 save_excel 保存。",
                execution_time=time.time() - start_time,
            )
        except Exception as exc:
            return self._format_response(False, error=str(exc), execution_time=time.time() - start_time)

    def add_row(self, sheet_name: str, row_data: Dict[str, Any]) -> Dict[str, Any]:
        start_time = time.time()
        try:
            if not isinstance(row_data, dict):
                return self._format_response(False, error="data 必须是字典", execution_time=time.time() - start_time)

            df = self._get_sheet_df(sheet_name)
            for col in row_data:
                if col not in df.columns:
                    df[col] = None

            new_row = {col: row_data.get(col, None) for col in df.columns}
            new_df = pd.DataFrame([new_row])
            self._cached_data[sheet_name] = pd.concat([df, new_df], ignore_index=True)

            return self._format_response(
                True,
                message="新行已添加到内存",
                execution_time=time.time() - start_time,
            )
        except Exception as exc:
            return self._format_response(False, error=str(exc), execution_time=time.time() - start_time)

    def create_workbook(
        self,
        output_path: Optional[str] = None,
        sheet_name: str = "Sheet1",
        columns: Optional[List[str]] = None,
        rows: Optional[Any] = None,
        sheets: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """创建新的工作簿并立即保存。"""
        start_time = time.time()
        try:
            path = output_path or self.file_path
            if not path:
                return self._format_response(False, error="create 操作需要提供 file 或 output_path")

            workbook_data: Dict[str, pd.DataFrame] = {}

            if sheets:
                for idx, sheet in enumerate(sheets, start=1):
                    name = sheet.get("name") or f"Sheet{idx}"
                    workbook_data[name] = self._build_dataframe(
                        rows=sheet.get("rows"),
                        columns=sheet.get("columns"),
                    )
            else:
                workbook_data[sheet_name or "Sheet1"] = self._build_dataframe(rows=rows, columns=columns)

            self.file_path = path
            self._cached_data = workbook_data
            self.sheet_names = list(workbook_data.keys())
            self._excel_file = None
            self._load_error = ""

            save_result = self.save_excel(path)
            if not save_result["success"]:
                return save_result

            save_result["data"] = {
                "sheets": self.sheet_names,
                "sheet_count": len(self.sheet_names),
                "path": path,
            }
            save_result["execution_time"] = round(time.time() - start_time, 4)
            return save_result
        except Exception as exc:
            return self._format_response(False, error=str(exc), execution_time=time.time() - start_time)

    def save_excel(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """将缓存的数据保存到文件。"""
        start_time = time.time()
        path = output_path or self.file_path

        try:
            if not path:
                return self._format_response(False, error="未指定保存路径")

            if os.path.splitext(path)[1].lower() == ".xls":
                return self._format_response(False, error="暂不支持保存为 .xls，请改用 .xlsx")

            sheets_to_save = self._load_all_sheets_for_save()
            if not sheets_to_save:
                if self.sheet_names:
                    for name in self.sheet_names:
                        sheets_to_save[name] = pd.DataFrame()
                else:
                    return self._format_response(False, error="没有可保存的数据")

            self._close_excel_file()
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                for sheet_name, df in sheets_to_save.items():
                    self._normalize_df(df).to_excel(writer, sheet_name=sheet_name, index=False)

            self.file_path = path
            self._cached_data = {name: self._normalize_df(df) for name, df in sheets_to_save.items()}
            self.sheet_names = list(sheets_to_save.keys())
            self._refresh_excel_file()

            return self._format_response(
                True,
                data={"sheets": self.sheet_names, "sheet_count": len(self.sheet_names), "path": path},
                message=f"文件已保存至: {path}（数据表模式保存：不保留原工作簿样式、公式、图表和合并单元格）",
                execution_time=time.time() - start_time,
            )
        except Exception as exc:
            return self._format_response(
                False,
                error=f"保存失败: {str(exc)}",
                execution_time=time.time() - start_time,
            )

    def get_stats(self, sheet_name: str) -> Dict[str, Any]:
        """获取工作表的统计信息。"""
        start_time = time.time()
        try:
            df = self._get_sheet_df(sheet_name)
            desc = df.describe(include="all").to_dict()
            return self._format_response(True, data=desc, execution_time=time.time() - start_time)
        except Exception as exc:
            return self._format_response(False, error=str(exc), execution_time=time.time() - start_time)

    def convert_format(self, output_path: str, target_format: str = "csv") -> Dict[str, Any]:
        """格式转换，例如转 CSV。"""
        start_time = time.time()
        try:
            if not output_path:
                return self._format_response(False, error="缺少 output_path")

            sheets_to_export = self._load_all_sheets_for_save()
            if not sheets_to_export:
                return self._format_response(False, error="没有可导出的数据")

            target_format = (target_format or "csv").lower()

            if target_format == "csv":
                base, _ = os.path.splitext(output_path)
                saved_files = []
                for name, df in sheets_to_export.items():
                    file_path = f"{base}_{name}.csv"
                    self._normalize_df(df).to_csv(file_path, index=False, encoding="utf-8-sig")
                    saved_files.append(file_path)

                return self._format_response(
                    True,
                    data={"files": saved_files},
                    execution_time=time.time() - start_time,
                )

            return self._format_response(False, error="暂不支持该目标格式")
        except Exception as exc:
            return self._format_response(False, error=str(exc), execution_time=time.time() - start_time)


class ExcelToolManager:
    """Excel 工具管理器，供 Xenon 动态工具系统自动加载。

    说明：本工具以 pandas 数据表模式处理工作簿。读写单元格数据很方便，
    但保存时会重写工作簿，不保留原样式、公式、图表、宏和合并单元格。
    """

    def __init__(self):
        self.handlers: Dict[str, ExcelHandler] = {}

    def _normalize_file_path(self, file_path: str) -> str:
        if not file_path:
            raise ValueError("缺少 file 参数")
        return os.path.abspath(os.fspath(file_path))

    def _get_handler(self, file_path: str) -> ExcelHandler:
        path = self._normalize_file_path(file_path)
        handler = self.handlers.get(path)
        if handler is None:
            handler = ExcelHandler(path)
            self.handlers[path] = handler
        return handler

    def _remember_handler(self, handler: ExcelHandler) -> None:
        if not handler.file_path:
            return

        path = self._normalize_file_path(handler.file_path)
        for key, cached_handler in list(self.handlers.items()):
            if cached_handler is handler and key != path:
                del self.handlers[key]
        self.handlers[path] = handler

    def info(self, file: str) -> Dict[str, Any]:
        """获取 Excel 文件元信息。

        :param file: Excel 文件路径。
        """
        try:
            return self._get_handler(file).load_excel_info()
        except Exception as exc:
            return {"success": False, "data": None, "message": "", "error": str(exc), "execution_time": 0.0}

    def get_data(
        self,
        file: str,
        sheet: str,
        start: int = 0,
        limit: int = 1000,
        filters: Optional[Dict[str, Any]] = None,
        columns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """读取工作表数据，支持分页、列选择和简单过滤。

        :param file: Excel 文件路径。
        :param sheet: 工作表名称。
        :param start: 起始行，0 表示第一行数据。
        :param limit: 最大返回行数。
        :param filters: 过滤条件，例如 {"Age": {"gt": 18}, "Name": {"contains": "张"}}。
        :param columns: 只返回指定列。
        """
        try:
            return self._get_handler(file).get_sheet_data(sheet, start, limit, filters, columns)
        except Exception as exc:
            return {"success": False, "data": None, "message": "", "error": str(exc), "execution_time": 0.0}

    def update(
        self,
        file: str,
        sheet: str,
        updates: List[Dict[str, Any]],
        auto_save: bool = True,
        output_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按行索引批量更新单元格。

        :param file: Excel 文件路径。
        :param sheet: 工作表名称。
        :param updates: 更新列表，例如 [{"index": 0, "data": {"Name": "Alice"}}]。
        :param auto_save: 是否在更新后立即保存。
        :param output_path: 可选另存为路径。
        """
        try:
            handler = self._get_handler(file)
            result = handler.update_records(sheet, updates)
            if result["success"] and auto_save:
                save_result = handler.save_excel(output_path)
                result = handler._merge_save_result(result, save_result)
                self._remember_handler(handler)
            return result
        except Exception as exc:
            return {"success": False, "data": None, "message": "", "error": str(exc), "execution_time": 0.0}

    def add_row(
        self,
        file: str,
        sheet: str,
        data: Dict[str, Any],
        auto_save: bool = True,
        output_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """向工作表追加一行数据。

        :param file: Excel 文件路径。
        :param sheet: 工作表名称。
        :param data: 新行数据，键为列名。
        :param auto_save: 是否在追加后立即保存。
        :param output_path: 可选另存为路径。
        """
        try:
            handler = self._get_handler(file)
            result = handler.add_row(sheet, data)
            if result["success"] and auto_save:
                save_result = handler.save_excel(output_path)
                result = handler._merge_save_result(result, save_result)
                self._remember_handler(handler)
            return result
        except Exception as exc:
            return {"success": False, "data": None, "message": "", "error": str(exc), "execution_time": 0.0}

    def create(
        self,
        file: Optional[str] = None,
        output_path: Optional[str] = None,
        sheet: str = "Sheet1",
        columns: Optional[List[str]] = None,
        rows: Optional[Any] = None,
        data: Optional[Any] = None,
        sheets: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """创建新的 Excel 工作簿。

        :param file: 输出文件路径。
        :param output_path: 输出文件路径，优先级高于 file。
        :param sheet: 单表创建时的工作表名称。
        :param columns: 单表创建时的列名。
        :param rows: 单表创建时的数据行。
        :param data: rows 的别名。
        :param sheets: 多工作表数据，例如 [{"name": "Sheet1", "columns": [...], "rows": [...]}]。
        """
        handler = ExcelHandler(None)
        result = handler.create_workbook(
            output_path=output_path or file,
            sheet_name=sheet,
            columns=columns,
            rows=rows if rows is not None else data,
            sheets=sheets,
        )
        if result.get("success"):
            self._remember_handler(handler)
        return result

    def save(self, file: str, output_path: Optional[str] = None) -> Dict[str, Any]:
        """保存当前缓存的工作簿数据。

        :param file: Excel 文件路径。
        :param output_path: 可选另存为路径。
        """
        try:
            handler = self._get_handler(file)
            result = handler.save_excel(output_path)
            if result.get("success"):
                self._remember_handler(handler)
            return result
        except Exception as exc:
            return {"success": False, "data": None, "message": "", "error": str(exc), "execution_time": 0.0}

    def stats(self, file: str, sheet: str) -> Dict[str, Any]:
        """获取工作表统计信息。

        :param file: Excel 文件路径。
        :param sheet: 工作表名称。
        """
        try:
            return self._get_handler(file).get_stats(sheet)
        except Exception as exc:
            return {"success": False, "data": None, "message": "", "error": str(exc), "execution_time": 0.0}

    def convert(self, file: str, output_path: str, format: str = "csv") -> Dict[str, Any]:
        """转换 Excel 文件格式，目前支持导出为 CSV。

        :param file: Excel 文件路径。
        :param output_path: 输出路径，CSV 导出时会按工作表生成多个文件。
        :param format: 目标格式，目前支持 csv。
        """
        try:
            return self._get_handler(file).convert_format(output_path, format)
        except Exception as exc:
            return {"success": False, "data": None, "message": "", "error": str(exc), "execution_time": 0.0}


def main() -> None:
    """
    命令行入口。
    推荐使用 STDIN 传递 JSON 参数，避免命令行参数过长。

    示例:
    echo '{"action": "get_data", "file": "test.xlsx", "sheet": "Sheet1", "limit": 10}' | python excel_handler.py
    """
    input_json: Dict[str, Any] = {}

    if not sys.stdin.isatty():
        try:
            input_str = sys.stdin.read()
            if input_str.strip():
                input_json = json.loads(input_str)
        except Exception as exc:
            print(json.dumps({"success": False, "error": f"STDIN JSON 解析失败: {str(exc)}"}, ensure_ascii=False))
            sys.exit(1)

    if not input_json and len(sys.argv) > 1:
        try:
            input_json = json.loads(sys.argv[1])
        except Exception as exc:
            print(json.dumps({"success": False, "error": f"命令行 JSON 解析失败: {str(exc)}"}, ensure_ascii=False))
            sys.exit(1)

    if not input_json or "action" not in input_json:
        print(json.dumps({"success": False, "error": "未提供操作指令。请通过 STDIN 传递 JSON。"}, ensure_ascii=False))
        sys.exit(1)

    action = input_json.get("action")
    file_path = input_json.get("file") or input_json.get("output_path")

    if action != "create" and not file_path:
        print(json.dumps({"success": False, "error": "缺少 'file' 参数"}, ensure_ascii=False))
        sys.exit(1)

    handler = ExcelHandler(file_path)
    result: Dict[str, Any]

    try:
        if action == "info":
            result = handler.load_excel_info()

        elif action == "get_data":
            result = handler.get_sheet_data(
                sheet_name=input_json.get("sheet"),
                start_row=input_json.get("start", 0),
                limit=input_json.get("limit", 1000),
                filters=input_json.get("filters"),
                columns=input_json.get("columns"),
            )

        elif action == "update":
            result = handler.update_records(
                sheet_name=input_json.get("sheet"),
                updates=input_json.get("updates", []),
            )
            if result["success"] and input_json.get("auto_save", True):
                save_result = handler.save_excel(input_json.get("output_path"))
                result = handler._merge_save_result(result, save_result)

        elif action == "add_row":
            result = handler.add_row(input_json.get("sheet"), input_json.get("data", {}))
            if result["success"] and input_json.get("auto_save", True):
                save_result = handler.save_excel(input_json.get("output_path"))
                result = handler._merge_save_result(result, save_result)

        elif action == "stats":
            result = handler.get_stats(input_json.get("sheet"))

        elif action == "save":
            result = handler.save_excel(input_json.get("output_path"))

        elif action == "convert":
            result = handler.convert_format(
                input_json.get("output_path"),
                input_json.get("format", "csv"),
            )

        elif action == "create":
            result = handler.create_workbook(
                output_path=input_json.get("output_path") or input_json.get("file"),
                sheet_name=input_json.get("sheet", "Sheet1"),
                columns=input_json.get("columns"),
                rows=input_json.get("rows", input_json.get("data")),
                sheets=input_json.get("sheets"),
            )

        else:
            result = {"success": False, "error": f"未知操作: {action}"}

    except Exception as exc:
        result = {"success": False, "error": f"执行异常: {str(exc)}"}

    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
