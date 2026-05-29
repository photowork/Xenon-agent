import json
import os
import logging
import platform
import tempfile
from typing import Dict, Any, List, Optional
from pathlib import Path

# --- 库检测与导入 ---
try:
    from PyPDF2 import PdfReader, PdfWriter, PdfMerger
    PYPDF2_AVAILABLE = True
except ImportError:
    PYPDF2_AVAILABLE = False
    logging.warning("PyPDF2 库不可用，PDF 功能将受限。请尝试安装: pip install PyPDF2")

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter, A4
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    # logging.warning("reportlab 库不可用...") # 减少不必要的警告刷屏

try:
    import docx2pdf
    DOCX2PDF_AVAILABLE = True
except ImportError:
    DOCX2PDF_AVAILABLE = False

try:
    from pdf2image import convert_from_path, pdfinfo_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    logging.warning("pdf2image 库不可用。请尝试安装: pip install pdf2image")

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    logging.warning("PyMuPDF 库不可用。请尝试安装: pip install pymupdf")

try:
    import win32com.client
    WIN32COM_AVAILABLE = True
except ImportError:
    WIN32COM_AVAILABLE = False

# --- 工具类 ---

class PDFHandler:
    def __init__(self, file_path: str = None):
        """
        初始化 PDF 处理器
        :param file_path: PDF 文件路径（可选）
        """
        self.file_path = file_path
        self.reader = None
        if not PYPDF2_AVAILABLE:
            logging.error("PyPDF2 库不可用，核心功能无法使用。")

    @staticmethod
    def _parse_page_spec(page_spec: str) -> List[int]:
        pages = []
        for part in page_spec.split(","):
            token = part.strip()
            if not token:
                continue
            if "-" in token:
                start_text, end_text = token.split("-", 1)
                start = int(start_text.strip())
                end = int(end_text.strip())
                if start > end:
                    raise ValueError(f"页码范围无效: {token}")
                pages.extend(range(start, end + 1))
            else:
                pages.append(int(token))
        return pages

    def _normalize_page_numbers(
        self,
        pages: Any,
        total_pages: int,
        *,
        default_all: bool = False,
        label: str = "页码",
    ) -> List[int]:
        if pages is None:
            if default_all:
                if total_pages <= 0:
                    raise ValueError(f"没有可用的{label}")
                return list(range(1, total_pages + 1))
            raise ValueError(f"{label}不能为空")

        if isinstance(pages, str):
            page_numbers = self._parse_page_spec(pages)
        elif isinstance(pages, int):
            page_numbers = [pages]
        else:
            page_numbers = list(pages)

        normalized = []
        for page in page_numbers:
            page_number = int(page)
            if page_number == 0:
                logging.warning("检测到页码 0，已自动转换为 1。")
                page_number = 1
            if not 1 <= page_number <= total_pages:
                raise ValueError(f"{label} {page_number} 超出范围 (1-{total_pages})")
            normalized.append(page_number)

        if not normalized:
            raise ValueError(f"没有有效的{label}")
        return normalized

    @staticmethod
    def _default_output_path(input_path: str, suffix: str) -> str:
        input_path_obj = Path(input_path)
        return str(input_path_obj.with_name(f"{input_path_obj.stem}_{suffix}{input_path_obj.suffix}"))

    @staticmethod
    def _write_pdf_safely(writer: PdfWriter, output_path: str, input_path: str = None) -> None:
        output_path_obj = Path(output_path)
        output_path_obj.parent.mkdir(parents=True, exist_ok=True)

        temp_path = None
        write_path = output_path_obj
        if input_path and output_path_obj.resolve() == Path(input_path).resolve():
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{output_path_obj.stem}_",
                suffix=".pdf",
                dir=str(output_path_obj.parent),
            )
            os.close(fd)
            temp_path = Path(temp_name)
            write_path = temp_path

        try:
            with open(write_path, "wb") as out_file:
                writer.write(out_file)
            if temp_path:
                os.replace(temp_path, output_path_obj)
        except Exception:
            if temp_path and temp_path.exists():
                temp_path.unlink()
            raise

    @staticmethod
    def _blank_page_size(reader: PdfReader, insert_index: int, page_size: str, width: float = None, height: float = None):
        if width is not None or height is not None:
            if width is None or height is None:
                raise ValueError("自定义空白页尺寸必须同时提供 width 和 height")
            custom_width = float(width)
            custom_height = float(height)
            if custom_width <= 0 or custom_height <= 0:
                raise ValueError("自定义空白页尺寸必须大于0")
            return custom_width, custom_height

        normalized_size = (page_size or "same").strip().lower()
        size_map = {
            "a4": (595.2756, 841.8898),
            "a4_landscape": (841.8898, 595.2756),
            "letter": (612.0, 792.0),
            "letter_landscape": (792.0, 612.0),
        }

        if normalized_size in size_map:
            return size_map[normalized_size]

        if normalized_size != "same":
            raise ValueError("page_size 仅支持 same、A4、A4_landscape、letter、letter_landscape")

        total_pages = len(reader.pages)
        if total_pages == 0:
            return size_map["a4"]

        reference_index = min(insert_index, total_pages - 1)
        reference_page = reader.pages[reference_index]
        return float(reference_page.mediabox.width), float(reference_page.mediabox.height)
    
    def load_pdf(self, file_path: str = None) -> Dict[str, Any]:
        """
        加载 PDF 文件
        """
        if not PYPDF2_AVAILABLE:
            return {"success": False, "error": "PyPDF2 库不可用"}
        
        try:
            path_to_load = file_path or self.file_path
            if path_to_load and Path(path_to_load).exists():
                self.reader = PdfReader(path_to_load)
                self.file_path = path_to_load
            else:
                return {"success": False, "error": "文件不存在"}
            
            return {
                "success": True,
                "message": f"成功加载PDF: {path_to_load}",
                "pages_count": len(self.reader.pages),
                "metadata": self.reader.metadata if self.reader.metadata else {}
            }
        except Exception as e:
            return {"success": False, "error": f"加载PDF失败: {str(e)}"}
    
    def read_pdf(self, file_path: str = None, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        读取PDF内容。
        :param pages: 页码列表（从1开始）。例如 [1, 2, 3]。如果为 None，则读取所有页面。
                      注意：为了直观，此处页码从 1 开始，不再是 0。
        """
        if not PYPDF2_AVAILABLE:
            return {"success": False, "error": "PyPDF2 库不可用"}
        
        try:
            # 加载逻辑
            if file_path:
                load_result = self.load_pdf(file_path)
                if not load_result["success"]:
                    return load_result
            
            if not self.reader:
                return {"success": False, "error": "PDF未加载"}
            
            total_pages = len(self.reader.pages)
            
            # 处理页码逻辑 (从1开始 -> 转换为从0开始)
            if pages is None:
                # 默认读取所有页，生成 1 到 N 的列表
                pages = list(range(1, total_pages + 1))
            else:
                if isinstance(pages, int):
                    pages = [pages]
                
                # 自动纠正：如果用户/智能体传入了0，自动修正为1，或者过滤掉非正数
                if 0 in pages:
                    logging.warning("检测到页码 0 (通常表示第一页)。已自动转换为 1。建议以后直接使用 1 表示第一页。")
                    pages = [p if p != 0 else 1 for p in pages]

            content = []
            valid_page_numbers = []
            
            for p in pages:
                # 验证范围 (1 到 total_pages)
                if 1 <= p <= total_pages:
                    index = p - 1 # 转换为索引
                    page = self.reader.pages[index]
                    text = page.extract_text()
                    
                    content.append({
                        "page_number": p,  # 返回给用户的是 1-based 页码
                        "content": text
                    })
                    valid_page_numbers.append(p)
                else:
                    logging.warning(f"页码 {p} 超出范围 (1-{total_pages})，已跳过")
            
            return {
                "success": True,
                "pages_count": len(valid_page_numbers),
                "content": content,
                "total_pages": total_pages,
                "note": "页码已按从1开始的标准返回"
            }
        except Exception as e:
            return {"success": False, "error": f"读取PDF内容失败: {str(e)}"}
    
    def split_pdf(self, input_path: str, output_prefix: str, pages_per_split: int = 1) -> Dict[str, Any]:
        """
        拆分PDF文件
        """
        if not PYPDF2_AVAILABLE:
            return {"success": False, "error": "PyPDF2 库不可用"}
        
        try:
            reader = PdfReader(input_path)
            total_pages = len(reader.pages)
            
            if pages_per_split <= 0:
                return {"success": False, "error": "每份页数必须大于0"}
            
            output_files = []
            
            for start_page in range(0, total_pages, pages_per_split):
                end_page = min(start_page + pages_per_split, total_pages)
                writer = PdfWriter()
                
                for page_idx in range(start_page, end_page):
                    writer.add_page(reader.pages[page_idx])
                
                output_filename = f"{output_prefix}_part_{start_page // pages_per_split + 1}.pdf"
                with open(output_filename, "wb") as out_file:
                    writer.write(out_file)
                
                output_files.append(output_filename)
            
            return {
                "success": True,
                "message": f"成功拆分PDF为 {len(output_files)} 个文件",
                "output_files": output_files
            }
        except Exception as e:
            return {"success": False, "error": f"拆分PDF失败: {str(e)}"}
    
    def merge_pdfs(self, input_paths: List[str], output_path: str) -> Dict[str, Any]:
        """
        合并多个PDF文件
        """
        if not PYPDF2_AVAILABLE:
            return {"success": False, "error": "PyPDF2 库不可用"}
        
        try:
            merger = PdfMerger()
            
            for path in input_paths:
                if not Path(path).exists():
                    return {"success": False, "error": f"输入文件不存在: {path}"}
                merger.append(path)
            
            merger.write(output_path)
            merger.close()
            
            return {
                "success": True,
                "message": f"成功合并 {len(input_paths)} 个PDF文件",
                "output_file": output_path
            }
        except Exception as e:
            return {"success": False, "error": f"合并PDF失败: {str(e)}"}

    def insert_pages(
        self,
        input_path: str,
        position: int,
        output_path: str = None,
        source_path: str = None,
        source_pages: Optional[List] = None,
        blank_pages: int = 1,
        page_size: str = "same",
        width: float = None,
        height: float = None,
    ) -> Dict[str, Any]:
        """
        在PDF任意位置插入页面。
        :param input_path: 待编辑的PDF文件路径
        :param position: 插入位置，从1开始；1表示插到第一页前，total_pages+1表示追加到末尾
        :param output_path: 输出PDF路径；不传则生成 *_pages_inserted.pdf
        :param source_path: 可选，来源PDF路径；提供后会从该PDF插入页面
        :param source_pages: 可选，来源PDF页码列表或"1,3-5"范围字符串；不传则插入来源PDF所有页面
        :param blank_pages: 未提供source_path时插入的空白页数量
        :param page_size: 空白页尺寸，支持 same、A4、A4_landscape、letter、letter_landscape
        :param width: 可选，自定义空白页宽度（PDF point）
        :param height: 可选，自定义空白页高度（PDF point）
        """
        if not PYPDF2_AVAILABLE:
            return {"success": False, "error": "PyPDF2 库不可用"}

        try:
            input_path_obj = Path(input_path)
            if not input_path_obj.exists():
                return {"success": False, "error": f"文件不存在: {input_path}"}

            reader = PdfReader(input_path)
            total_pages = len(reader.pages)
            insert_position = int(position)
            if insert_position == 0:
                logging.warning("检测到插入位置 0，已自动转换为 1。")
                insert_position = 1
            if not 1 <= insert_position <= total_pages + 1:
                return {"success": False, "error": f"插入位置超出范围，应为 1-{total_pages + 1}"}

            insert_index = insert_position - 1
            writer = PdfWriter()
            inserted_count = 0
            source_file = None

            if source_path:
                source_path_obj = Path(source_path)
                if not source_path_obj.exists():
                    return {"success": False, "error": f"来源PDF不存在: {source_path}"}

                source_reader = PdfReader(source_path)
                source_total_pages = len(source_reader.pages)
                page_numbers = self._normalize_page_numbers(
                    source_pages,
                    source_total_pages,
                    default_all=True,
                    label="来源页码",
                )

                for page_idx in range(total_pages + 1):
                    if page_idx == insert_index:
                        for page_number in page_numbers:
                            writer.add_page(source_reader.pages[page_number - 1])
                            inserted_count += 1
                    if page_idx < total_pages:
                        writer.add_page(reader.pages[page_idx])

                source_file = str(source_path_obj)
            else:
                blank_count = int(blank_pages or 1)
                if blank_count <= 0:
                    return {"success": False, "error": "空白页数量必须大于0"}

                blank_width, blank_height = self._blank_page_size(reader, insert_index, page_size, width, height)
                for page_idx in range(total_pages + 1):
                    if page_idx == insert_index:
                        for _ in range(blank_count):
                            writer.add_blank_page(width=blank_width, height=blank_height)
                            inserted_count += 1
                    if page_idx < total_pages:
                        writer.add_page(reader.pages[page_idx])

            final_output_path = output_path or self._default_output_path(input_path, "pages_inserted")
            self._write_pdf_safely(writer, final_output_path, input_path)

            return {
                "success": True,
                "message": f"成功插入 {inserted_count} 页",
                "output_file": final_output_path,
                "insert_position": insert_position,
                "inserted_pages": inserted_count,
                "total_pages_before": total_pages,
                "total_pages_after": total_pages + inserted_count,
                "source_file": source_file,
            }
        except Exception as e:
            return {"success": False, "error": f"插入页面失败: {str(e)}"}

    def delete_pages(self, input_path: str, pages: List, output_path: str = None) -> Dict[str, Any]:
        """
        删除PDF中的指定页面。
        :param input_path: 待编辑的PDF文件路径
        :param pages: 要删除的页码列表（从1开始），也支持"1,3-5"范围字符串
        :param output_path: 输出PDF路径；不传则生成 *_pages_deleted.pdf
        """
        if not PYPDF2_AVAILABLE:
            return {"success": False, "error": "PyPDF2 库不可用"}

        try:
            input_path_obj = Path(input_path)
            if not input_path_obj.exists():
                return {"success": False, "error": f"文件不存在: {input_path}"}

            reader = PdfReader(input_path)
            total_pages = len(reader.pages)
            pages_to_delete = set(self._normalize_page_numbers(pages, total_pages, label="待删除页码"))

            if len(pages_to_delete) >= total_pages:
                return {"success": False, "error": "不能删除全部页面，PDF至少需要保留1页"}

            writer = PdfWriter()
            for page_number in range(1, total_pages + 1):
                if page_number not in pages_to_delete:
                    writer.add_page(reader.pages[page_number - 1])

            final_output_path = output_path or self._default_output_path(input_path, "pages_deleted")
            self._write_pdf_safely(writer, final_output_path, input_path)

            deleted_pages = sorted(pages_to_delete)
            return {
                "success": True,
                "message": f"成功删除 {len(deleted_pages)} 页",
                "output_file": final_output_path,
                "deleted_pages": deleted_pages,
                "total_pages_before": total_pages,
                "total_pages_after": total_pages - len(deleted_pages),
            }
        except Exception as e:
            return {"success": False, "error": f"删除页面失败: {str(e)}"}
    
    def convert_docx_to_pdf(self, input_path: str, output_path: str = None) -> Dict[str, Any]:
        """
        将DOCX文件转换为PDF
        """
        if not DOCX2PDF_AVAILABLE and not WIN32COM_AVAILABLE:
            return {
                "success": False,
                "error": "缺少转换库。请安装 docx2pdf，或在 Windows 上安装 pywin32 并确保已安装 Microsoft Word"
            }
        
        if output_path is None:
            output_path = str(Path(input_path).with_suffix('.pdf'))
        
        try:
            input_path_obj = Path(input_path)
            if not input_path_obj.exists():
                return {"success": False, "error": f"文件不存在: {input_path}"}

            file_ext = Path(input_path).suffix.lower()
            current_platform = platform.system()

            if file_ext != '.docx':
                return {"success": False, "error": f"仅支持 .docx 文件，当前扩展名为: {file_ext or '无'}"}
            
            if DOCX2PDF_AVAILABLE and file_ext == '.docx':
                if current_platform not in ("Windows", "Darwin"):
                    return {
                        "success": False,
                        "error": f"docx2pdf 仅支持 Windows/macOS，当前平台为: {current_platform}"
                    }
                import docx2pdf
                docx2pdf.convert(input_path, output_path)
                if os.path.exists(output_path):
                    return {"success": True, "message": "转换成功", "output_file": output_path}
                else:
                    return {"success": False, "error": "转换完成但文件未生成"}
                    
            elif WIN32COM_AVAILABLE and current_platform == "Windows":
                word_app = win32com.client.Dispatch('Word.Application')
                word_app.Visible = False
                try:
                    doc = word_app.Documents.Open(os.path.abspath(input_path))
                    doc.SaveAs(os.path.abspath(output_path), FileFormat=17)
                    doc.Close()
                finally:
                    word_app.Quit()
                return {"success": True, "message": "转换成功", "output_file": output_path}
            else:
                return {
                    "success": False,
                    "error": (
                        f"当前平台 {current_platform} 无可用的 DOCX 转 PDF 方法。"
                        "该功能通常需要 Windows/macOS 环境，且需安装 Microsoft Word。"
                    )
                }
        except Exception as e:
            return {"success": False, "error": f"转换失败: {str(e)}"}
    
    def pdf_to_images(self, input_path: str, output_folder: str = None, output_format: str = "png", dpi: int = 300, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        将PDF转换为图片
        :param pages: 要转换的页码列表（从1开始）。如果为 None，则转换所有页面。
        """
        if not PYMUPDF_AVAILABLE and not PDF2IMAGE_AVAILABLE:
            return {"success": False, "error": "需要安装 PyMuPDF 或 pdf2image"}
        
        try:
            input_path_obj = Path(input_path)
            if not input_path_obj.exists():
                return {"success": False, "error": f"文件不存在: {input_path}"}
            
            if output_folder is None:
                output_folder = str(input_path_obj.parent)
            
            output_folder_path = Path(output_folder)
            output_folder_path.mkdir(parents=True, exist_ok=True)
            
            output_format = output_format.lower()
            if output_format == "jpg": output_format = "jpeg"
            
            output_files = []
            
            # --- PyMuPDF 逻辑 ---
            if PYMUPDF_AVAILABLE:
                doc = fitz.open(input_path)
                total_pages = doc.page_count
                
                # 处理页码范围 (从1开始)
                target_pages = pages if pages is not None else list(range(1, total_pages + 1))
                if isinstance(target_pages, int): target_pages = [target_pages]
                
                # 自动纠正0
                if 0 in target_pages:
                    logging.warning("检测到页码 0，已修正为 1。")
                    target_pages = [p if p != 0 else 1 for p in target_pages]

                for p in target_pages:
                    if not (1 <= p <= total_pages):
                        logging.warning(f"页码 {p} 超出范围 (1-{total_pages})，跳过")
                        continue
                    
                    index = p - 1 # 内部转换
                    page = doc[index]
                    zoom = dpi / 72
                    mat = fitz.Matrix(zoom, zoom)
                    pix = page.get_pixmap(matrix=mat)
                    
                    # 文件名也使用 1-based，更直观
                    out_file = output_folder_path / f"{input_path_obj.stem}_page_{p}.{output_format}"
                    pix.save(str(out_file))
                    output_files.append(str(out_file))
                
                doc.close()
            
            # --- pdf2image 逻辑 (备用) ---
            elif PDF2IMAGE_AVAILABLE:
                info = pdfinfo_from_path(input_path)
                total_pages = int(info.get("Pages", 0))

                if total_pages <= 0:
                    return {"success": False, "error": "无法获取 PDF 页数，pdf2image 可能缺少运行依赖（如 Poppler）"}

                if pages is None:
                    target_pages = list(range(1, total_pages + 1))
                else:
                    target_pages = [pages] if isinstance(pages, int) else list(pages)

                if 0 in target_pages:
                    logging.warning("检测到页码 0，已修正为 1。")
                    target_pages = [p if p != 0 else 1 for p in target_pages]

                valid_pages = sorted({p for p in target_pages if 1 <= p <= total_pages})
                if not valid_pages:
                    return {"success": False, "error": f"没有可转换的有效页码，范围应为 1-{total_pages}"}

                for p in valid_pages:
                    try:
                        # pdf2image 的 first_page 参数是 1-based
                        images = convert_from_path(input_path, dpi=dpi, first_page=p, last_page=p)
                        if not images:
                            logging.error(f"Page {p} failed: no image returned")
                            continue

                        out_file = output_folder_path / f"{input_path_obj.stem}_page_{p}.{output_format}"
                        images[0].save(str(out_file), output_format.upper())
                        output_files.append(str(out_file))
                    except Exception as inner_e:
                        logging.error(f"Page {p} failed: {inner_e}")

                if not output_files:
                    return {"success": False, "error": "pdf2image 未生成任何图片，请检查 PDF 内容和运行依赖"}

            return {
                "success": True,
                "message": f"成功转换 {len(output_files)} 张图片",
                "output_files": output_files,
                "output_folder": str(output_folder_path)
            }
        except Exception as e:
            return {"success": False, "error": f"PDF转图片失败: {str(e)}"}


class PDFToolManager:
    def __init__(self):
        pass

    def read_pdf(self, file_path: str, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        读取PDF内容。
        :param pages: 页码列表，**从1开始计数**。例如 [1, 2, 3]。
        """
        return PDFHandler().read_pdf(file_path, pages)

    def split_pdf(self, input_path: str, output_prefix: str, pages_per_split: int = 1) -> Dict[str, Any]:
        return PDFHandler().split_pdf(input_path, output_prefix, pages_per_split)

    def merge_pdfs(self, input_paths: List[str], output_path: str) -> Dict[str, Any]:
        return PDFHandler().merge_pdfs(input_paths, output_path)

    def insert_pages(
        self,
        input_path: str,
        position: int,
        output_path: str = None,
        source_path: str = None,
        source_pages: Optional[List] = None,
        blank_pages: int = 1,
        page_size: str = "same",
        width: float = None,
        height: float = None,
    ) -> Dict[str, Any]:
        """
        在PDF任意位置插入页面。
        :param input_path: 待编辑的PDF文件路径
        :param position: 插入位置，从1开始；1表示插到第一页前，total_pages+1表示追加到末尾
        :param output_path: 输出PDF路径；不传则生成 *_pages_inserted.pdf
        :param source_path: 可选，来源PDF路径；提供后会从该PDF插入页面
        :param source_pages: 可选，来源PDF页码列表或"1,3-5"范围字符串；不传则插入来源PDF所有页面
        :param blank_pages: 未提供source_path时插入的空白页数量
        :param page_size: 空白页尺寸，支持 same、A4、A4_landscape、letter、letter_landscape
        :param width: 可选，自定义空白页宽度（PDF point）
        :param height: 可选，自定义空白页高度（PDF point）
        """
        return PDFHandler().insert_pages(
            input_path,
            position,
            output_path,
            source_path,
            source_pages,
            blank_pages,
            page_size,
            width,
            height,
        )

    def delete_pages(self, input_path: str, pages: List, output_path: str = None) -> Dict[str, Any]:
        """
        删除PDF中的指定页面。
        :param input_path: 待编辑的PDF文件路径
        :param pages: 要删除的页码列表（从1开始），也支持"1,3-5"范围字符串
        :param output_path: 输出PDF路径；不传则生成 *_pages_deleted.pdf
        """
        return PDFHandler().delete_pages(input_path, pages, output_path)

    def convert_docx_to_pdf(self, input_path: str, output_path: str = None) -> Dict[str, Any]:
        return PDFHandler().convert_docx_to_pdf(input_path, output_path)
    
    def pdf_to_images(self, input_path: str, output_folder: str = None, output_format: str = "png", dpi: int = 300, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        将PDF转换为图片
        :param pages: 页码列表，**从1开始计数**。
        """
        return PDFHandler().pdf_to_images(input_path, output_folder, output_format, dpi, pages)
