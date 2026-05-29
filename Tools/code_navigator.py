"""
🚀 代码导航器 v1.0
为开发者提供闪电般的代码理解速度

核心理念：不读不需要的代码，但知道去哪找需要的代码
"""

import os
import re
import ast
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set
from pathlib import Path
from collections import defaultdict


# ============================================================================
# 数据结构定义
# ============================================================================

@dataclass
class CodeLocation:
    """代码位置"""
    file_path: str
    start_line: int
    end_line: int
    
    def __str__(self):
        return f"{self.file_path}:{self.start_line}-{self.end_line}"


@dataclass
class ClassInfo:
    """类信息"""
    name: str
    start_line: int
    end_line: int
    docstring: Optional[str] = None
    methods: List[str] = field(default_factory=list)
    base_classes: List[str] = field(default_factory=list)
    
    def to_summary(self) -> str:
        """生成简要描述"""
        base = f"类 {self.name}"
        if self.base_classes:
            base += f"({', '.join(self.base_classes)})"
        base += f" (行{self.start_line}-{self.end_line})"
        if self.docstring:
            # 只取第一行文档字符串
            first_line = self.docstring.split('\n')[0][:50]
            base += f"\n  📝 {first_line}"
        if self.methods:
            base += f"\n  🔧 方法: {', '.join(self.methods[:5])}"
            if len(self.methods) > 5:
                base += f" ... (+{len(self.methods)-5})"
        return base


@dataclass
class FunctionInfo:
    """函数信息"""
    name: str
    start_line: int
    end_line: int
    parameters: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    is_async: bool = False
    is_method: bool = False
    class_name: Optional[str] = None
    
    def to_summary(self) -> str:
        """生成简要描述"""
        prefix = "async " if self.is_async else ""
        func_type = "方法" if self.is_method else "函数"
        params = ", ".join(self.parameters) if self.parameters else ""
        summary = f"{prefix}{func_type} {self.name}({params}) (行{self.start_line}-{self.end_line})"
        if self.docstring:
            first_line = self.docstring.split('\n')[0][:50]
            summary += f"\n  📝 {first_line}"
        return summary


@dataclass
class ImportInfo:
    """导入信息"""
    module: str
    names: List[str] = field(default_factory=list)
    alias: Optional[str] = None
    line: int = 0


@dataclass
class CodeStructureCard:
    """代码结构卡片"""
    file_path: str
    file_size: int
    total_lines: int
    language: str
    classes: List[ClassInfo] = field(default_factory=list)
    functions: List[FunctionInfo] = field(default_factory=list)
    imports: List[ImportInfo] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    constants: Dict[str, int] = field(default_factory=dict)
    scan_time: float = 0.0
    
    def to_map(self) -> str:
        """生成结构地图（< 1KB）"""
        lines = []
        lines.append(f"📁 文件: {Path(self.file_path).name}")
        lines.append(f"   大小: {self.file_size/1024:.1f}KB | 行数: {self.total_lines}")
        lines.append(f"   语言: {self.language}")
        lines.append(f"⏱️  扫描耗时: {self.scan_time*1000:.1f}ms")
        lines.append("")
        
        # 入口点
        if self.entry_points:
            lines.append("🎯 入口点:")
            for ep in self.entry_points:
                lines.append(f"   • {ep}")
            lines.append("")
        
        # 类结构
        if self.classes:
            lines.append("🏗️  类结构:")
            for cls in self.classes:
                lines.append(f"   ├── {cls.name} (行{cls.start_line})")
                for method in cls.methods[:3]:
                    lines.append(f"   │   • {method}")
                if len(cls.methods) > 3:
                    lines.append(f"   │   ... (+{len(cls.methods)-3})")
            lines.append("")
        
        # 独立函数
        standalone_funcs = [f for f in self.functions if not f.is_method]
        if standalone_funcs:
            lines.append("🔧 关键函数:")
            for func in standalone_funcs[:5]:
                async_mark = "async " if func.is_async else ""
                lines.append(f"   • {async_mark}{func.name}() (行{func.start_line})")
            if len(standalone_funcs) > 5:
                lines.append(f"   ... (+{len(standalone_funcs)-5})")
            lines.append("")
        
        # 导入
        if self.imports:
            lines.append(f"📦 导入模块: {len(self.imports)}个")
        
        # 预计阅读时间
        read_time = len(self.functions) * 0.5 + len(self.classes) * 1.0
        lines.append(f"\n📖 预计阅读: {read_time:.1f}分钟 → 使用导航器: 30秒")
        
        return '\n'.join(lines)
    
    def find_function(self, name: str) -> Optional[FunctionInfo]:
        """查找函数"""
        for func in self.functions:
            if func.name == name:
                return func
        return None
    
    def find_class(self, name: str) -> Optional[ClassInfo]:
        """查找类"""
        for cls in self.classes:
            if cls.name == name:
                return cls
        return None


# ============================================================================
# 核心导航器类
# ============================================================================

class CodeNavigator:
    """代码导航器"""
    
    def __init__(self):
        self.cache: Dict[str, CodeStructureCard] = {}
        self.file_contents: Dict[str, List[str]] = {}
        
    # ------------------------------------------------------------------------
    # 核心功能1：闪电扫描
    # ------------------------------------------------------------------------
    
    def scan(self, file_path: str, use_cache: bool = True) -> CodeStructureCard:
        """
        闪电扫描 - 5秒内理解文件结构
        
        Args:
            file_path: 文件路径
            use_cache: 是否使用缓存
            
        Returns:
            CodeStructureCard: 代码结构卡片
        """
        # 检查缓存
        if use_cache and file_path in self.cache:
            return self.cache[file_path]
        
        start_time = time.time()
        
        # 读取文件
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')
        
        self.file_contents[file_path] = lines
        
        # 识别语言
        language = self._detect_language(file_path)
        
        # 创建结构卡片
        card = CodeStructureCard(
            file_path=file_path,
            file_size=os.path.getsize(file_path),
            total_lines=len(lines),
            language=language
        )
        
        # 根据语言选择解析方式
        if language == 'python':
            self._scan_python(content, lines, card)
        else:
            self._scan_generic(content, lines, card)
        
        # 查找入口点
        card.entry_points = self._find_entry_points(lines)
        
        # 记录扫描时间
        card.scan_time = time.time() - start_time
        
        # 缓存结果
        self.cache[file_path] = card
        
        return card
    
    def _detect_language(self, file_path: str) -> str:
        """检测文件语言"""
        ext = Path(file_path).suffix.lower()
        lang_map = {
            '.py': 'python',
            '.js': 'javascript',
            '.ts': 'typescript',
            '.java': 'java',
            '.cpp': 'cpp',
            '.c': 'c',
            '.go': 'go',
            '.rs': 'rust',
        }
        return lang_map.get(ext, 'unknown')
    
    def _scan_python(self, content: str, lines: List[str], card: CodeStructureCard):
        """Python代码扫描（使用AST）"""
        try:
            tree = ast.parse(content)
            
            # 扫描导入
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        card.imports.append(ImportInfo(
                            module=alias.name,
                            alias=alias.asname,
                            line=node.lineno
                        ))
                elif isinstance(node, ast.ImportFrom):
                    names = [alias.name for alias in node.names]
                    card.imports.append(ImportInfo(
                        module=node.module or '',
                        names=names,
                        line=node.lineno
                    ))
            
            # 扫描类和函数
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    # 提取基类
                    bases = []
                    for base in node.bases:
                        if isinstance(base, ast.Name):
                            bases.append(base.id)
                    
                    # 提取方法
                    methods = []
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            methods.append(item.name)
                    
                    # 提取文档字符串
                    docstring = ast.get_docstring(node)
                    
                    card.classes.append(ClassInfo(
                        name=node.name,
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        docstring=docstring,
                        methods=methods,
                        base_classes=bases
                    ))
                    
                    # 将方法也添加到函数列表
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                            is_async = isinstance(item, ast.AsyncFunctionDef)
                            params = [arg.arg for arg in item.args.args]
                            method_docstring = ast.get_docstring(item)
                            
                            card.functions.append(FunctionInfo(
                                name=item.name,
                                start_line=item.lineno,
                                end_line=item.end_lineno or item.lineno,
                                parameters=params,
                                docstring=method_docstring,
                                is_async=is_async,
                                is_method=True,
                                class_name=node.name
                            ))
                
                elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                    is_async = isinstance(node, ast.AsyncFunctionDef)
                    
                    # 提取参数
                    params = []
                    for arg in node.args.args:
                        params.append(arg.arg)
                    
                    # 提取文档字符串
                    docstring = ast.get_docstring(node)
                    
                    card.functions.append(FunctionInfo(
                        name=node.name,
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        parameters=params,
                        docstring=docstring,
                        is_async=is_async,
                        is_method=False
                    ))
        
        except SyntaxError:
            # AST解析失败，使用正则回退
            self._scan_generic(content, lines, card)
    
    def _scan_generic(self, content: str, lines: List[str], card: CodeStructureCard):
        """通用代码扫描（使用正则）"""
        # 扫描类定义
        class_pattern = r'^\s*class\s+(\w+).*?:'
        for i, line in enumerate(lines, 1):
            match = re.match(class_pattern, line)
            if match:
                card.classes.append(ClassInfo(
                    name=match.group(1),
                    start_line=i,
                    end_line=self._find_block_end(lines, i)
                ))
        
        # 扫描函数定义
        func_pattern = r'^\s*(async\s+)?def\s+(\w+)\s*\((.*?)\):'
        for i, line in enumerate(lines, 1):
            match = re.match(func_pattern, line)
            if match:
                is_async = match.group(1) is not None
                name = match.group(2)
                params = [p.strip() for p in match.group(3).split(',') if p.strip()]
                
                card.functions.append(FunctionInfo(
                    name=name,
                    start_line=i,
                    end_line=self._find_block_end(lines, i),
                    parameters=params,
                    is_async=is_async
                ))
    
    def _find_block_end(self, lines: List[str], start_line: int) -> int:
        """查找代码块结束行"""
        if start_line >= len(lines):
            return start_line
        
        # 获取起始缩进
        start_indent = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
        
        # 向下查找缩进减少的行
        for i in range(start_line, len(lines)):
            line = lines[i]
            if line.strip():  # 非空行
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= start_indent:
                    return i
        
        return len(lines)
    
    def _find_entry_points(self, lines: List[str]) -> List[str]:
        """查找入口点"""
        entry_points = []
        
        for i, line in enumerate(lines, 1):
            # Python: if __name__ == "__main__":
            if '__name__' in line and '__main__' in line:
                entry_points.append(f"主入口 (行{i})")
            
            # main() 函数
            if re.match(r'^\s*def\s+main\s*\(', line):
                entry_points.append(f"main() (行{i})")
            
            # async main
            if re.match(r'^\s*async\s+def\s+main\s*\(', line):
                entry_points.append(f"async main() (行{i})")
        
        return entry_points
    
    # ------------------------------------------------------------------------
    # 核心功能2：精准导航
    # ------------------------------------------------------------------------
    
    def search(
        self,
        pattern: str,
        file_path: Optional[str] = None,
        search_type: str = "name"
    ) -> List[Tuple[str, int, str]]:
        """
        智能搜索
        
        Args:
            pattern: 搜索模式
            file_path: 文件路径（可选）
            search_type: 搜索类型 (name/functionality/regex)
            
        Returns:
            List[Tuple[file_path, line_number, context]]
        """
        results = []
        
        # 确定搜索范围
        files = [file_path] if file_path else list(self.cache.keys())
        
        for fp in files:
            if fp not in self.file_contents:
                continue
            
            lines = self.file_contents[fp]
            
            if search_type == "name":
                # 按名称搜索
                results.extend(self._search_by_name(fp, lines, pattern))
            elif search_type == "regex":
                # 正则搜索
                results.extend(self._search_by_regex(fp, lines, pattern))
            elif search_type == "functionality":
                # 功能搜索（注释+代码）
                results.extend(self._search_by_functionality(fp, lines, pattern))
            elif search_type == "text":
                # 全文本子串搜索（不区分大小写）
                results.extend(self._search_by_text(fp, lines, pattern))
            else:
                # 兜底：未知类型降级为 functionality 搜索
                results.extend(self._search_by_functionality(fp, lines, pattern))
        
        return results
    
    def _search_by_name(
        self,
        file_path: str,
        lines: List[str],
        name: str
    ) -> List[Tuple[str, int, str]]:
        """按名称搜索"""
        results = []
        
        # 搜索函数定义
        func_pattern = rf'^\s*(async\s+)?def\s+{re.escape(name)}\s*\('
        for i, line in enumerate(lines, 1):
            if re.match(func_pattern, line):
                results.append((file_path, i, f"函数定义: {line.strip()[:60]}"))
        
        # 搜索类定义
        class_pattern = rf'^\s*class\s+{re.escape(name)}\s*[:\(]'
        for i, line in enumerate(lines, 1):
            if re.match(class_pattern, line):
                results.append((file_path, i, f"类定义: {line.strip()[:60]}"))
        
        # 搜索调用
        for i, line in enumerate(lines, 1):
            if name in line and not re.match(func_pattern, line) and not re.match(class_pattern, line):
                if not line.strip().startswith('#'):  # 排除注释
                    results.append((file_path, i, f"调用: {line.strip()[:60]}"))
        
        return results
    
    def _search_by_regex(
        self,
        file_path: str,
        lines: List[str],
        pattern: str
    ) -> List[Tuple[str, int, str]]:
        """正则搜索"""
        results = []
        try:
            regex = re.compile(pattern)
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    results.append((file_path, i, line.strip()[:80]))
        except re.error:
            pass
        return results
    
    def _search_by_functionality(
        self,
        file_path: str,
        lines: List[str],
        keyword: str
    ) -> List[Tuple[str, int, str]]:
        """功能搜索"""
        results = []
        keyword_lower = keyword.lower()
        
        for i, line in enumerate(lines, 1):
            # 搜索注释中的关键词
            if line.strip().startswith('#') and keyword_lower in line.lower():
                results.append((file_path, i, f"注释: {line.strip()[:60]}"))
            
            # 搜索文档字符串
            if '"""' in line or "'''" in line:
                if keyword_lower in line.lower():
                    results.append((file_path, i, f"文档: {line.strip()[:60]}"))
            
            # 搜索函数名和变量名
            if keyword_lower in line.lower():
                stripped = line.strip()
                if not stripped.startswith('#'):
                    results.append((file_path, i, f"代码: {stripped[:60]}"))
        
        return results

    def _search_by_text(
        self,
        file_path: str,
        lines: List[str],
        pattern: str
    ) -> List[Tuple[str, int, str]]:
        """全文本子串搜索（不区分大小写，用于 'text' 类型）"""
        results = []
        pattern_lower = pattern.lower()
        for i, line in enumerate(lines, 1):
            if pattern_lower in line.lower():
                results.append((file_path, i, f"匹配: {line.strip()[:80]}"))
        return results

    
    def jump_to(self, file_path: str, location: str) -> Tuple[int, int]:
        """
        精准跳转
        
        Args:
            file_path: 文件路径
            location: 位置标识（函数名、类名、行号范围）
            
        Returns:
            Tuple[start_line, end_line]
        """
        # 检查是否是行号范围
        range_match = re.match(r'(\d+)-(\d+)', location)
        if range_match:
            return int(range_match.group(1)), int(range_match.group(2))
        
        # 检查是否是单个行号
        if location.isdigit():
            line_num = int(location)
            return line_num, line_num
        
        # 在缓存中查找
        card = self.cache.get(file_path)
        if not card:
            card = self.scan(file_path)
        
        # 查找函数
        func = card.find_function(location)
        if func:
            return func.start_line, func.end_line
        
        # 查找类
        cls = card.find_class(location)
        if cls:
            return cls.start_line, cls.end_line
        
        # 未找到，返回错误
        raise ValueError(f"未找到位置: {location}")
    
    def view(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        context_lines: int = 5
    ) -> str:
        """
        查看代码片段
        
        Args:
            file_path: 文件路径
            start_line: 起始行
            end_line: 结束行
            context_lines: 上下文行数
            
        Returns:
            代码片段
        """
        if file_path not in self.file_contents:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.file_contents[file_path] = f.read().split('\n')
        
        lines = self.file_contents[file_path]
        
        # 添加上下文
        actual_start = max(1, start_line - context_lines)
        actual_end = min(len(lines), end_line + context_lines)
        
        result = []
        result.append(f"📄 {Path(file_path).name}:{start_line}-{end_line}")
        result.append("")
        
        for i in range(actual_start - 1, actual_end):
            line_num = i + 1
            prefix = "→" if start_line <= line_num <= end_line else " "
            result.append(f"{prefix} {line_num:4d} | {lines[i]}")
        
        return '\n'.join(result)
    
    # ------------------------------------------------------------------------
    # 核心功能3：代码分析
    # ------------------------------------------------------------------------
    
    def analyze_dependencies(self, file_path: str) -> Dict[str, List[str]]:
        """
        分析依赖关系
        
        Returns:
            Dict: {"imports": [...], "imported_by": [...]}
        """
        card = self.cache.get(file_path)
        if not card:
            card = self.scan(file_path)
        
        imports = [imp.module for imp in card.imports]
        
        # 查找被哪些文件导入（需要扫描其他文件）
        imported_by = []
        for other_path in self.cache:
            if other_path == file_path:
                continue
            other_card = self.cache[other_path]
            module_name = Path(file_path).stem
            for imp in other_card.imports:
                if module_name in imp.module or imp.module.endswith(module_name):
                    imported_by.append(other_path)
        
        return {
            "imports": imports,
            "imported_by": list(set(imported_by))
        }
    
    def find_callers(self, file_path: str, function_name: str) -> List[Tuple[str, int]]:
        """查找函数调用者"""
        callers = []
        
        for other_path in self.cache:
            if other_path == file_path:
                continue
            
            if other_path not in self.file_contents:
                with open(other_path, 'r', encoding='utf-8') as f:
                    self.file_contents[other_path] = f.read().split('\n')
            
            lines = self.file_contents[other_path]
            for i, line in enumerate(lines, 1):
                if function_name in line:
                    # 排除定义
                    if not re.match(rf'^\s*(async\s+)?def\s+{function_name}\s*\(', line):
                        callers.append((other_path, i))
        
        return callers
    
    def find_callees(self, file_path: str, start_line: int, end_line: int) -> List[str]:
        """查找被调用的函数"""
        if file_path not in self.file_contents:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.file_contents[file_path] = f.read().split('\n')
        
        lines = self.file_contents[file_path]
        code_block = '\n'.join(lines[start_line-1:end_line])
        
        # 提取函数调用
        callees = set()
        call_pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        
        for match in re.finditer(call_pattern, code_block):
            func_name = match.group(1)
            # 排除内置函数和关键字
            if func_name not in ['if', 'for', 'while', 'with', 'print', 'len', 'str', 'int', 'list', 'dict']:
                callees.add(func_name)
        
        return sorted(list(callees))
    
    def estimate_complexity(self, file_path: str, start_line: int, end_line: int) -> Dict[str, int]:
        """估算代码复杂度"""
        if file_path not in self.file_contents:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.file_contents[file_path] = f.read().split('\n')
        
        lines = self.file_contents[file_path]
        code_block = '\n'.join(lines[start_line-1:end_line])
        
        return {
            "lines": end_line - start_line + 1,
            "if_statements": len(re.findall(r'\bif\b', code_block)),
            "for_loops": len(re.findall(r'\bfor\b', code_block)),
            "while_loops": len(re.findall(r'\bwhile\b', code_block)),
            "try_blocks": len(re.findall(r'\btry\b', code_block)),
            "nesting_depth": self._estimate_nesting(lines, start_line, end_line)
        }
    
    def _estimate_nesting(self, lines: List[str], start_line: int, end_line: int) -> int:
        """估算嵌套深度"""
        max_indent = 0
        for i in range(start_line-1, end_line):
            if lines[i].strip():
                indent = len(lines[i]) - len(lines[i].lstrip())
                max_indent = max(max_indent, indent)
        
        # 每4个空格算一级
        return max_indent // 4
    
    # ------------------------------------------------------------------------
    # 核心功能4：项目级导航
    # ------------------------------------------------------------------------
    
    def scan_project(self, project_path: str) -> Dict[str, CodeStructureCard]:
        """扫描整个项目"""
        project_cards = {}
        
        for root, dirs, files in os.walk(project_path):
            # 跳过常见的不需要的目录
            dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__', 'node_modules', 'venv', 'env']]
            
            for file in files:
                if file.endswith('.py'):  # 可以扩展其他语言
                    file_path = os.path.join(root, file)
                    try:
                        card = self.scan(file_path)
                        project_cards[file_path] = card
                    except Exception as e:
                        print(f"⚠️  扫描失败 {file_path}: {e}")
        
        return project_cards
    
    def generate_project_map(self, project_path: str) -> str:
        """生成项目地图"""
        cards = self.scan_project(project_path)
        
        lines = []
        lines.append(f"📁 项目: {Path(project_path).name}")
        lines.append(f"📊 文件数: {len(cards)}")
        lines.append("")
        
        # 按文件大小排序
        sorted_cards = sorted(cards.items(), key=lambda x: x[1].file_size, reverse=True)
        
        for file_path, card in sorted_cards[:10]:  # 只显示前10个
            rel_path = os.path.relpath(file_path, project_path)
            lines.append(f"📄 {rel_path}")
            lines.append(f"   大小: {card.file_size/1024:.1f}KB | 类: {len(card.classes)} | 函数: {len(card.functions)}")
            
            # 显示主要类
            if card.classes:
                lines.append(f"   类: {', '.join([c.name for c in card.classes[:3]])}")
            
            # 显示入口点
            if card.entry_points:
                lines.append(f"   🎯 {card.entry_points[0]}")
            
            lines.append("")
        
        if len(sorted_cards) > 10:
            lines.append(f"... 还有 {len(sorted_cards)-10} 个文件")
        
        return '\n'.join(lines)


# ============================================================================
# 工具集成接口
# ============================================================================

class CodeNavigatorToolManager:
    """为智能体提供的工具接口"""
    
    def __init__(self):
        self.navigator = CodeNavigator()
    
    def scan_file(self, file_path: str) -> str:
        """工具：扫描文件"""
        card = self.navigator.scan(file_path)
        return card.to_map()
    
    def search_code(
        self,
        pattern: str,
        file_path: Optional[str] = None,
        search_type: str = "name"
    ) -> str:
        """工具：搜索代码"""
        results = self.navigator.search(pattern, file_path, search_type)
        
        if not results:
            return f"未找到匹配: {pattern}"
        
        lines = [f"找到 {len(results)} 个匹配:\n"]
        for fp, line_num, context in results[:20]:  # 限制输出
            lines.append(f"  • {Path(fp).name}:{line_num} - {context}")
        
        if len(results) > 20:
            lines.append(f"\n... 还有 {len(results)-20} 个结果")
        
        return '\n'.join(lines)
    
    def view_code(
        self,
        file_path: str,
        location: str,
        context_lines: int = 5
    ) -> str:
        """工具：查看代码"""
        try:
            start, end = self.navigator.jump_to(file_path, location)
            return self.navigator.view(file_path, start, end, context_lines)
        except ValueError as e:
            return str(e)
    
    def analyze_function(self, file_path: str, function_name: str) -> str:
        """工具：分析函数"""
        card = self.navigator.scan(file_path)
        func = card.find_function(function_name)
        
        if not func:
            return f"未找到函数: {function_name}"
        
        lines = []
        lines.append(f"🔍 函数分析: {function_name}")
        lines.append("")
        lines.append(func.to_summary())
        lines.append("")
        
        # 复杂度分析
        complexity = self.navigator.estimate_complexity(
            file_path, func.start_line, func.end_line
        )
        lines.append("📊 复杂度:")
        lines.append(f"  代码行数: {complexity['lines']}")
        lines.append(f"  嵌套深度: {complexity['nesting_depth']}")
        lines.append(f"  分支数: {complexity['if_statements'] + complexity['for_loops'] + complexity['while_loops']}")
        lines.append("")
        
        # 调用关系
        callees = self.navigator.find_callees(file_path, func.start_line, func.end_line)
        if callees:
            lines.append(f"📞 调用的函数: {', '.join(callees[:10])}")
        
        return '\n'.join(lines)
