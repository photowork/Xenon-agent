#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
智能记忆处理器
增强版记忆系统，包含标签分类、关联网络、统计分析等功能
"""

import os
import re
import json
import pickle
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Set, Tuple
from collections import defaultdict
import hashlib

# 记忆系统基类
HAS_EXISTING_MEMORY = True

class MemoryQueryHandler:
    """记忆查询处理器基类"""
    DEFAULT_MEMORY_DIR = "Memory/memory_Write"
    DEFAULT_ENCODING = 'utf-8'
    
    def __init__(self, *_, **_compat_kwargs):
        self.memory_dir = Path(self.DEFAULT_MEMORY_DIR)
    
    @staticmethod
    def _make_memory_filename(timestamp: str, summary: str = None) -> str:
        """根据 summary 生成描述性文件名；无 summary 时回退到通用后缀。"""
        if summary:
            safe = re.sub(r'[\\/:*?"<>|\s]+', '_', summary).strip('_')[:60]
            if safe:
                return f"{timestamp}_{safe}.txt"
        return f"{timestamp}_记忆.txt"

    def write_memory(self, content: str, summary: str = None,
                    encoding: str = 'utf-8') -> Dict[str, Any]:
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
            filename = self._make_memory_filename(timestamp, summary)
            file_path = self.memory_dir / filename

            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding=encoding) as f:
                f.write(content)
            
            return {
                "success": True,
                "filename": filename,
                "message": f"成功写入记忆文件: {filename}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"写入记忆失败: {str(e)}"
            }
    
    def search_memories(self, keyword: str, limit: int = 10, 
                       case_sensitive: bool = False) -> Dict[str, Any]:
        try:
            matches = []
            for file_path in self.memory_dir.glob("*.txt"):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    search_text = content if case_sensitive else content.lower()
                    search_keyword = keyword if case_sensitive else keyword.lower()
                    
                    if search_keyword in search_text:
                        keyword_count = search_text.count(search_keyword)
                        matches.append({
                            "filename": file_path.name,
                            "keyword_count": keyword_count,
                            "score": keyword_count,
                            "modified_time": file_path.stat().st_mtime,
                            "size": file_path.stat().st_size
                        })
                except Exception as e:
                    continue
            
            matches.sort(key=lambda x: x["keyword_count"], reverse=True)
            
            return {
                "success": True,
                "search_type": "keyword_search",
                "keyword": keyword,
                "total_files_matched": len(matches),
                "matches": matches[:limit],
                "message": f"使用关键词搜索找到 {len(matches)} 个相关记忆"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"搜索记忆失败: {str(e)}"
            }
    
    def list_memories(self, limit: int = 20, sort_by: str = 'newest') -> Dict[str, Any]:
        try:
            memory_files = []
            for file_path in self.memory_dir.glob("*.txt"):
                stat = file_path.stat()
                memory_files.append({
                    "filename": file_path.name,
                    "modified_time": stat.st_mtime,
                    "size": stat.st_size
                })
            
            if sort_by == 'newest':
                memory_files.sort(key=lambda x: x["modified_time"], reverse=True)
            elif sort_by == 'oldest':
                memory_files.sort(key=lambda x: x["modified_time"], reverse=False)
            elif sort_by == 'largest':
                memory_files.sort(key=lambda x: x["size"], reverse=True)
            elif sort_by == 'smallest':
                memory_files.sort(key=lambda x: x["size"], reverse=False)
            
            return {
                "success": True,
                "count": len(memory_files),
                "memories": memory_files[:limit]
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"列出记忆失败: {str(e)}"
            }

class MemoryNode:
    """记忆节点类，表示单个记忆及其元数据"""
    
    def __init__(self, node_id: str, content: str, filename: str, 
                 timestamp: str, summary: str = "", tags: List[str] = None,
                 cognitive_type: str = "memory", cognitive_state: str = "active",
                 confidence: float = 0.7, priority: float = 0.5,
                 scope: str = "general", source_kind: str = "memory_write",
                 activation_keywords: List[str] = None, supersedes: List[str] = None,
                 invalidates: List[str] = None):
        self.id = node_id
        self.content = content
        self.filename = filename
        self.timestamp = timestamp
        self.summary = summary
        self.tags = tags or []
        self.importance = 1.0
        self.relations = []
        self.access_count = 0
        self.last_accessed = datetime.now().isoformat()
        self.created_at = datetime.now().isoformat()
        self.cognitive_type = cognitive_type or "memory"
        self.cognitive_state = cognitive_state or "active"
        self.confidence = confidence
        self.priority = priority
        self.scope = scope or "general"
        self.source_kind = source_kind or "memory_write"
        self.activation_keywords = activation_keywords or []
        self.supersedes = supersedes or []
        self.invalidates = invalidates or []
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "timestamp": self.timestamp,
            "summary": self.summary,
            "tags": self.tags,
            "importance": self.importance,
            "content_length": len(self.content),
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "created_at": self.created_at,
            "cognitive_type": self.cognitive_type,
            "cognitive_state": self.cognitive_state,
            "confidence": self.confidence,
            "priority": self.priority,
            "scope": self.scope,
            "source_kind": self.source_kind,
            "activation_keywords": self.activation_keywords,
            "supersedes": self.supersedes,
            "invalidates": self.invalidates
        }
    
    def add_relation(self, target_id: str, relation_type: str, weight: float = 1.0):
        self.relations.append({
            "target_id": target_id,
            "type": relation_type,
            "weight": weight,
            "created_at": datetime.now().isoformat()
        })
    
    def increment_access(self):
        self.access_count += 1
        self.last_accessed = datetime.now().isoformat()

class MemoryGraph:
    """记忆图类，管理记忆节点之间的关系网络"""
    
    def __init__(self):
        self.nodes = {}
        self.edges = defaultdict(list)
        self.next_node_id = 1
    
    def add_node(self, content: str, filename: str, timestamp: str, 
                 summary: str = "", tags: List[str] = None, **metadata) -> str:
        node_id = f"node_{self.next_node_id}"
        self.next_node_id += 1
        
        node = MemoryNode(node_id, content, filename, timestamp, summary, tags, **metadata)
        self.nodes[node_id] = node
        return node_id
    
    def add_existing_node(self, node: MemoryNode):
        self.nodes[node.id] = node
    
    def add_relation(self, source_id: str, target_id: str, 
                    relation_type: str, weight: float = 1.0):
        if source_id in self.nodes and target_id in self.nodes:
            self.nodes[source_id].add_relation(target_id, relation_type, weight)
            self.nodes[target_id].add_relation(source_id, relation_type, weight)
            
            self.edges[source_id].append(target_id)
            self.edges[target_id].append(source_id)
    
    def find_related_nodes(self, node_id: str, max_depth: int = 2) -> List[Dict]:
        if node_id not in self.nodes:
            return []
        
        visited = set()
        results = []
        
        def dfs(current_id: str, depth: int, path: List[str]):
            if depth > max_depth:
                return
            
            if current_id in visited:
                return
            
            visited.add(current_id)
            
            if current_id != node_id:
                node = self.nodes[current_id]
                results.append({
                    "node_id": current_id,
                    "filename": node.filename,
                    "summary": node.summary,
                    "depth": depth,
                    "path": path.copy()
                })
            
            for target_id in self.edges.get(current_id, []):
                dfs(target_id, depth + 1, path + [current_id])
        
        dfs(node_id, 0, [])
        return results
    
    def get_network_stats(self) -> Dict[str, Any]:
        total_nodes = len(self.nodes)
        total_edges = sum(len(targets) for targets in self.edges.values()) // 2
        
        degrees = [len(self.edges.get(node_id, [])) for node_id in self.nodes]
        
        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "density": total_edges / (total_nodes * (total_nodes - 1) / 2) if total_nodes > 1 else 0,
            "avg_degree": sum(degrees) / total_nodes if total_nodes > 0 else 0,
            "max_degree": max(degrees) if degrees else 0,
            "min_degree": min(degrees) if degrees else 0
        }
    
class SmartMemoryHandler(MemoryQueryHandler):
    """智能记忆处理器，增强现有记忆系统"""
    DEFAULT_EXECUTION_LOG_DIR = "Memory/execution_logs"
    EXECUTION_LOG_RETENTION_DAYS = 1
    EXECUTION_LOG_CLEANUP_INTERVAL_SECONDS = 3600
    
    def __init__(self, *_, enable_network: bool = False,
                 execution_log_dir: str = None, **_compat_kwargs):
        super().__init__()
        
        self.enable_network = False
        self.memory_graph = MemoryGraph()
        self.tag_index = defaultdict(set)
        self.importance_threshold = 0.5
        self._last_execution_log_cleanup: Optional[datetime] = None
        
        # 执行日志目录：与记忆目录分离，避免污染记忆查询
        if execution_log_dir:
            self.execution_log_dir = Path(execution_log_dir)
        else:
            self.execution_log_dir = Path(self.DEFAULT_EXECUTION_LOG_DIR)
        
        # 首次创建 handler 时自动清理过期执行日志，防止无限堆积
        try:
            self.cleanup_execution_logs(retention_days=self.EXECUTION_LOG_RETENTION_DAYS)
            self._last_execution_log_cleanup = datetime.now()
        except Exception:
            pass

    def write_memory(self, content: str, summary: str = None,
                    encoding: str = 'utf-8', tags: List[str] = None) -> Dict[str, Any]:
        result = super().write_memory(content, summary, encoding)
        
        if result["success"] and self.enable_network:
            try:
                if tags is None:
                    tags = self._extract_tags(content, summary)
                profile = self._infer_cognitive_profile(content, summary, tags)
                
                filename = result.get("filename", "")
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                node_id = self.memory_graph.add_node(
                    content,
                    filename,
                    timestamp,
                    summary,
                    tags,
                    cognitive_type=profile["cognitive_type"],
                    cognitive_state=profile["cognitive_state"],
                    confidence=profile["confidence"],
                    priority=profile["priority"],
                    scope=profile["scope"],
                    source_kind=profile["source_kind"],
                    activation_keywords=profile["activation_keywords"],
                )
                
                for tag in tags:
                    self.tag_index[tag].add(node_id)
                
                self._auto_connect_node(node_id, content)
                result["node_id"] = node_id
                result["tags"] = tags
                result["cognitive_type"] = profile["cognitive_type"]
                result["cognitive_state"] = profile["cognitive_state"]
                result["priority"] = profile["priority"]
                result["network_saved"] = True
                
            except Exception as e:
                result["network_saved"] = False
                result["network_error"] = str(e)
        
        return result
    
    def _extract_tags(self, content: str, summary: str = None) -> List[str]:
        tags = []
        max_tag_length = 6
        
        text_source = summary if summary else content[:200]
        
        common_tags = {
            '任务': ['任务', 'task', '完成'],
            '用户': ['用户', 'user'],
            '信息': ['信息', 'info', '记录'],
            '代码': ['代码', 'code', 'python', 'js'],
            '测试': ['测试', 'test', '验证'],
            '对话': ['对话', 'dialogue', 'chat'],
            '配置': ['配置', 'config', '设置'],
            '系统': ['系统', 'system'],
            '开发': ['开发', 'development', 'dev']
        }
        
        for common_tag, keywords in common_tags.items():
            if any(keyword in text_source.lower() for keyword in keywords):
                if len(common_tag) <= max_tag_length and common_tag not in tags:
                    tags.append(common_tag)
        
        words = re.findall(r'[\u4e00-\u9fffA-Za-z]{2,}', text_source)
        for word in words[:5]:
            if len(word) > 1:
                tag = word.lower()[:max_tag_length]
                if tag not in tags:
                    tags.append(tag)
        
        if not tags:
            words = re.findall(r'[\u4e00-\u9fffA-Za-z]{2,}', text_source)
            for word in words[:3]:
                if len(word) > 1:
                    tag = word.lower()[:max_tag_length]
                    if tag not in tags:
                        tags.append(tag)
        
        if not tags:
            tags = ["未分类"]
        
        return list(set(tags))
    
    def _infer_cognitive_profile(self, content: str, summary: str = None, tags: List[str] = None) -> Dict[str, Any]:
        text = " ".join(
            part for part in [
                summary or "",
                content[:1200] if content else "",
                " ".join(tags or []),
            ] if part
        ).lower()

        profile = {
            "cognitive_type": "memory",
            "cognitive_state": "active",
            "confidence": 0.7,
            "priority": 0.55,
            "scope": "general",
            "source_kind": "memory_write",
            "activation_keywords": self._build_activation_keywords(summary, content, tags),
        }

        rules = [
            ("goal", 0.95, 0.92, ["目标", "计划", "打算", "希望", "准备", "priority", "roadmap", "next step"]),
            ("constraint", 0.95, 0.95, ["不能", "不要", "禁止", "约束", "必须", "限制", "must not", "constraint"]),
            ("decision", 0.95, 0.9, ["决定", "确定", "改成", "选择", "方案", "采用", "switch", "decide", "chosen"]),
            ("preference", 0.85, 0.8, ["喜欢", "偏好", "习惯", "更想", "prefer", "usually", "tend to"]),
            ("project_state", 0.9, 0.82, ["完成", "进展", "状态", "当前", "阻塞", "待办", "progress", "status", "blocked"]),
            ("risk", 0.88, 0.86, ["风险", "问题", "失败", "报错", "注意", "crash", "issue", "bug", "warning"]),
            ("lesson", 0.84, 0.82, ["经验", "教训", "总结", "复盘", "建议", "learned", "lesson"]),
            ("relationship", 0.82, 0.75, ["用户", "创造者", "伙伴", "关系", "family", "creator", "relationship"]),
            ("identity", 0.9, 0.8, ["xenon", "我是", "身份", "创造者", "自我", "identity"]),
            ("workflow", 0.75, 0.72, ["流程", "步骤", "协议", "工作流", "workflow", "protocol"]),
        ]

        best_hits = 0
        for cognitive_type, priority, confidence, keywords in rules:
            hits = sum(1 for keyword in keywords if keyword in text)
            if hits > best_hits:
                best_hits = hits
                profile["cognitive_type"] = cognitive_type
                profile["priority"] = priority
                profile["confidence"] = confidence

        if any(token in text for token in ["过时", "失效", "废弃", "删除", "停用", "deprecated", "obsolete"]):
            profile["cognitive_state"] = "outdated"
            profile["priority"] = min(profile["priority"], 0.45)

        if any(token in text for token in ["当前", "正在", "latest", "现在", "目前"]):
            profile["scope"] = "current"
        elif any(token in text for token in ["长期", "稳定", "规则", "偏好", "always", "long-term"]):
            profile["scope"] = "long_term"

        return profile

    def _build_activation_keywords(self, summary: str = None, content: str = None, tags: List[str] = None) -> List[str]:
        seed_text = " ".join(part for part in [summary or "", (content or "")[:240], " ".join(tags or [])] if part)
        keywords = []
        for match in re.findall(r'[\u4e00-\u9fffA-Za-z0-9_]{2,16}', seed_text):
            token = match.strip().lower()
            if token and token not in keywords:
                keywords.append(token)
            if len(keywords) >= 10:
                break
        return keywords

    def _auto_connect_node(self, node_id: str, content: str):
        if node_id not in self.memory_graph.nodes:
            return
        
        recent_nodes = list(self.memory_graph.nodes.keys())[-10:-1]
        current_node = self.memory_graph.nodes[node_id]
        current_tags = set(current_node.tags)
        
        for other_id in recent_nodes:
            if other_id == node_id:
                continue

            other_node = self.memory_graph.nodes.get(other_id)
            if not other_node:
                continue

            relation_type = "time_related"
            weight = 0.2
            if other_node.cognitive_type == current_node.cognitive_type:
                relation_type = "same_cognitive_type"
                weight = 0.7
            elif current_tags & set(other_node.tags):
                relation_type = "shared_tags"
                weight = 0.55
            if relation_type:
                self.memory_graph.add_relation(node_id, other_id, relation_type, weight)
            else:
                self.memory_graph.add_relation(node_id, other_id, "时间相关", 0.3)
    
    def search_memories(self, keyword: str, limit: int = 10, 
                       case_sensitive: bool = False,
                       tags: List[str] = None) -> Dict[str, Any]:
        
        if tags and self.enable_network:
            if keyword:
                matches = []
                for file_path in self.memory_dir.glob("*.txt"):
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                    except Exception:
                        continue

                    search_text = content if case_sensitive else content.lower()
                    search_keyword = keyword if case_sensitive else keyword.lower()
                    if search_keyword not in search_text:
                        continue

                    node_id = self._find_node_by_filename(file_path.name)
                    if not node_id:
                        continue

                    node = self.memory_graph.nodes.get(node_id)
                    if not node or not any(tag in node.tags for tag in tags):
                        continue

                    keyword_count = search_text.count(search_keyword)
                    stat = file_path.stat()
                    matches.append({
                        "filename": file_path.name,
                        "keyword_count": keyword_count,
                        "score": keyword_count,
                        "modified_time": stat.st_mtime,
                        "size": stat.st_size,
                        "tags": node.tags,
                        "importance": node.importance,
                        "access_count": node.access_count
                    })

                matches.sort(key=lambda x: x["keyword_count"], reverse=True)
                result = {
                    "success": True,
                    "search_type": "keyword_search",
                    "keyword": keyword,
                    "total_files_matched": len(matches),
                    "matches": matches[:limit],
                    "filtered_by_tags": tags,
                    "message": f"使用关键词和标签搜索找到 {len(matches)} 个相关记忆"
                }
            else:
                matches = []
                for node_id, node in self.memory_graph.nodes.items():
                    if any(tag in node.tags for tag in tags):
                        file_path = self.memory_dir / node.filename
                        if file_path.exists():
                            stat = file_path.stat()
                            matches.append({
                                "filename": node.filename,
                                "keyword_count": 0,
                                "score": 0,
                                "modified_time": stat.st_mtime,
                                "size": stat.st_size,
                                "tags": node.tags,
                                "importance": node.importance,
                                "access_count": node.access_count
                            })
                
                matches.sort(key=lambda x: x["modified_time"], reverse=True)
                
                result = {
                    "success": True,
                    "search_type": "tag_search",
                    "keyword": "",
                    "total_files_matched": len(matches),
                    "matches": matches[:limit],
                    "filtered_by_tags": tags,
                    "message": f"使用标签搜索找到 {len(matches)} 个相关记忆"
                }
        else:
            result = super().search_memories(keyword, limit, case_sensitive)
        
        return result
    
    def _find_node_by_filename(self, filename: str) -> Optional[str]:
        for node_id, node in self.memory_graph.nodes.items():
            if node.filename == filename:
                return node_id
        return None
    
    def read_memory(self, filename: str, encoding: str = 'utf-8') -> Dict[str, Any]:
        try:
            file_path = self.memory_dir / filename
            if not file_path.exists():
                return {
                    "success": False,
                    "error": f"记忆文件不存在: {filename}"
                }
            
            with open(file_path, 'r', encoding=encoding) as f:
                content = f.read()
             
            node_id = self._find_node_by_filename(filename)
            if node_id and node_id in self.memory_graph.nodes:
                node = self.memory_graph.nodes[node_id]
                node.increment_access()
            return {
                "success": True,
                "filename": filename,
                "content": content,
                "encoding": encoding,
                "size": len(content)
            }
        except UnicodeDecodeError:
            try:
                with open(file_path, 'r', encoding='gbk') as f:
                    content = f.read()

                node_id = self._find_node_by_filename(filename)
                if node_id and node_id in self.memory_graph.nodes:
                    node = self.memory_graph.nodes[node_id]
                    node.increment_access()
                return {
                    "success": True,
                    "filename": filename,
                    "content": content,
                    "encoding": 'gbk',
                    "size": len(content)
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"读取记忆文件失败(编码问题): {str(e)}"
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"读取记忆文件失败: {str(e)}"
            }
    
    def get_latest_memory(self, encoding: str = 'utf-8') -> Dict[str, Any]:
        try:
            list_result = self.list_memories(limit=1, sort_by='newest')
            if not list_result["success"] or list_result["count"] == 0:
                return {
                    "success": False,
                    "error": "没有找到记忆文件"
                }
            
            filename = list_result["memories"][0]["filename"]
            return self.read_memory(filename, encoding)
        except Exception as e:
            return {
                "success": False,
                "error": f"获取最新记忆失败: {str(e)}"
            }
    
    def get_memory_by_date(self, date_str: str) -> Dict[str, Any]:
        try:
            date_formats = ["%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"]
            target_date = None
            
            for fmt in date_formats:
                try:
                    target_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue
            
            if target_date is None:
                return {
                    "success": False,
                    "error": f"无效的日期格式: {date_str}，请使用 YYYY-MM-DD 格式"
                }
            
            memories_on_date = []
            for file_path in self.memory_dir.glob("*.txt"):
                file_date = datetime.fromtimestamp(file_path.stat().st_mtime).date()
                if file_date == target_date:
                    memories_on_date.append({
                        "filename": file_path.name,
                        "modified_time": file_path.stat().st_mtime,
                        "size": file_path.stat().st_size
                    })
            
            return {
                "success": True,
                "date": date_str,
                "count": len(memories_on_date),
                "memories": memories_on_date
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"按日期查找记忆失败: {str(e)}"
            }
    
    def get_memory_summary(self, limit: int = 5) -> Dict[str, Any]:
        try:
            list_result = self.list_memories(limit=limit, sort_by='newest')
            if not list_result["success"]:
                return list_result
            
            summaries = []
            for mem in list_result["memories"]:
                filename = mem["filename"]
                read_result = self.read_memory(filename)
                
                if read_result["success"]:
                    content = read_result["content"]
                    preview = content[:100] + "..." if len(content) > 100 else content
                    
                    summaries.append({
                        "filename": filename,
                        "preview": preview,
                        "size": len(content),
                        "modified_time": mem["modified_time"]
                    })
            
            return {
                "success": True,
                "count": len(summaries),
                "summaries": summaries
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"获取记忆摘要失败: {str(e)}"
            }
    
    def append_to_memory(self, filename: str, content: str, 
                        encoding: str = 'utf-8') -> Dict[str, Any]:
        try:
            file_path = self.memory_dir / filename
            if not file_path.exists():
                return {
                    "success": False,
                    "error": f"记忆文件不存在: {filename}"
                }
            
            with open(file_path, 'a', encoding=encoding) as f:
                f.write("\n" + content)
            
            new_size = file_path.stat().st_size
            
            return {
                "success": True,
                "filename": filename,
                "appended_size": len(content),
                "new_total_size": new_size
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"追加记忆内容失败: {str(e)}"
            }
    
    def delete_memory(self, filename: str) -> Dict[str, Any]:
        try:
            file_path = self.memory_dir / filename
            if not file_path.exists():
                return {
                    "success": False,
                    "error": f"记忆文件不存在: {filename}"
                }
            
            node_id = self._find_node_by_filename(filename)
            if node_id and node_id in self.memory_graph.nodes:
                node = self.memory_graph.nodes[node_id]
                for tag in node.tags:
                    if tag in self.tag_index and node_id in self.tag_index[tag]:
                        self.tag_index[tag].remove(node_id)
                
                del self.memory_graph.nodes[node_id]
                if node_id in self.memory_graph.edges:
                    del self.memory_graph.edges[node_id]
                
                for source_id, targets in list(self.memory_graph.edges.items()):
                    if node_id in targets:
                        targets.remove(node_id)
                
            file_path.unlink()
            
            return {
                "success": True,
                "filename": filename,
                "message": f"成功删除记忆文件: {filename}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"删除记忆文件失败: {str(e)}"
            }
    
    def batch_delete_memories(self, filenames: List[str]) -> Dict[str, Any]:
        try:
            results = []
            for filename in filenames:
                result = self.delete_memory(filename)
                results.append(result)
            
            success_count = sum(1 for r in results if r["success"])
            
            return {
                "success": True,
                "total": len(filenames),
                "success_count": success_count,
                "failed_count": len(filenames) - success_count,
                "results": results
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"批量删除记忆文件失败: {str(e)}"
            }
    
    def get_memory_with_tags(self, limit: int = 20, sort_by: str = 'newest') -> Dict[str, Any]:
        try:
            if sort_by in ['importance', 'access_count']:
                memory_files = []
                for node_id, node in self.memory_graph.nodes.items():
                    file_path = self.memory_dir / node.filename
                    if file_path.exists():
                        stat = file_path.stat()
                        memory_files.append({
                            "filename": node.filename,
                            "modified_time": stat.st_mtime,
                            "size": stat.st_size,
                            "tags": node.tags,
                            "importance": node.importance,
                            "access_count": node.access_count
                        })
                
                if sort_by == 'importance':
                    memory_files.sort(key=lambda x: x["importance"], reverse=True)
                elif sort_by == 'access_count':
                    memory_files.sort(key=lambda x: x["access_count"], reverse=True)
                
                return {
                    "success": True,
                    "count": len(memory_files),
                    "memories": memory_files[:limit],
                    "message": f"成功获取{len(memory_files)}条记忆，按{sort_by}排序"
                }
            else:
                list_result = self.list_memories(limit=limit, sort_by=sort_by)
                
                if not list_result["success"]:
                    return list_result
                
                enhanced_memories = []
                for mem in list_result["memories"]:
                    enhanced_mem = mem.copy()
                    filename = mem["filename"]
                    
                    node_id = self._find_node_by_filename(filename)
                    if node_id and node_id in self.memory_graph.nodes:
                        node = self.memory_graph.nodes[node_id]
                        enhanced_mem["tags"] = node.tags
                        enhanced_mem["importance"] = node.importance
                        enhanced_mem["access_count"] = node.access_count
                    else:
                        enhanced_mem["tags"] = []
                        enhanced_mem["importance"] = 1.0
                        enhanced_mem["access_count"] = 0
                    
                    enhanced_memories.append(enhanced_mem)
                
                return {
                    "success": True,
                    "count": len(enhanced_memories),
                    "memories": enhanced_memories,
                    "message": f"成功获取{len(enhanced_memories)}条带标签的记忆"
                }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"获取带标签记忆失败: {str(e)}"
            }
    
    def get_tag_stats(self) -> Dict[str, Any]:
        try:
            tag_counts = {}
            for tag, node_ids in self.tag_index.items():
                tag_counts[tag] = len(node_ids)
            
            sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
            
            return {
                "success": True,
                "total_tags": len(tag_counts),
                "tag_counts": dict(sorted_tags[:20]),
                "message": f"共 {len(tag_counts)} 个标签"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"获取标签统计失败: {str(e)}"
            }
    
    def list_cognitive_memories(self, cognitive_type: str = None, state: str = "active",
                                limit: int = 20) -> Dict[str, Any]:
        try:
            memories = []
            for node_id, node in self.memory_graph.nodes.items():
                if cognitive_type and node.cognitive_type != cognitive_type:
                    continue
                if state and node.cognitive_state != state:
                    continue
                memories.append({
                    "node_id": node_id,
                    "filename": node.filename,
                    "summary": node.summary,
                    "cognitive_type": node.cognitive_type,
                    "cognitive_state": node.cognitive_state,
                    "priority": node.priority,
                    "confidence": node.confidence,
                    "scope": node.scope,
                    "tags": node.tags,
                })

            memories.sort(key=lambda item: (item["priority"], item["confidence"]), reverse=True)
            return {
                "success": True,
                "count": len(memories),
                "memories": memories[:limit],
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"鍒楀嚭璁ょ煡鑺傜偣澶辫触: {str(e)}"
            }

    def get_cognitive_state(self, current_query: str = "", limit: int = 10) -> Dict[str, Any]:
        try:
            from xenon_core.cognitive_network import CognitiveNetworkState

            builder = CognitiveNetworkState(memory_dir=str(self.memory_dir))
            summary = builder.build_summary(current_query=current_query, max_nodes=limit)
            return {
                "success": True,
                "summary": summary,
                "query": current_query,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"鐢熸垚璁ょ煡鐘舵€佸け璐? {str(e)}"
            }

    def build_phase_summary(
        self,
        current_query: str = "",
        current_phase: str = "",
        current_intent: str = "",
        limit: int = 8,
        recent_failures: List[str] = None,
    ) -> Dict[str, Any]:
        try:
            from xenon_core.cognitive_network import CognitiveNetworkState

            builder = CognitiveNetworkState(memory_dir=str(self.memory_dir))
            summary = builder.build_phase_summary(
                current_query=current_query,
                current_phase=current_phase,
                current_intent=current_intent,
                max_nodes=limit,
                recent_failures=recent_failures or [],
            )
            return {
                "success": True,
                "summary": summary,
                "query": current_query,
                "phase": current_phase,
                "intent": current_intent,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"build_phase_summary failed: {str(e)}"
            }

    def get_activation_set(
        self,
        current_query: str = "",
        current_phase: str = "",
        current_intent: str = "",
        limit: int = 5,
        recent_failures: List[str] = None,
    ) -> Dict[str, Any]:
        try:
            from xenon_core.cognitive_network import CognitiveNetworkState

            builder = CognitiveNetworkState(memory_dir=str(self.memory_dir))
            activation_set = builder.get_activation_set(
                current_query=current_query,
                current_phase=current_phase,
                current_intent=current_intent,
                limit=limit,
                recent_failures=recent_failures or [],
            )
            return {
                "success": True,
                "count": len(activation_set),
                "activation_set": activation_set,
                "phase": current_phase,
                "intent": current_intent,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"get_activation_set failed: {str(e)}"
            }

    def write_execution_memory(
        self,
        goal: str,
        phase: str,
        tool_name: str,
        success: bool,
        blockage_reason: str = None,
        lesson: str = "",
        summary: str = "",
        next_actions: List[str] = None,
    ) -> Dict[str, Any]:
        try:
            timestamp = datetime.now()
            status_label = "success" if success else "failure"
            base_summary = (summary or lesson or goal or phase or tool_name or "execution").strip()
            compact_summary = re.sub(r"\s+", " ", base_summary)[:180]
            raw_filename = f"{timestamp.strftime('%Y-%m-%d_%H-%M-%S_%f')}_execution_{status_label}.txt"
            file_path = self.execution_log_dir / raw_filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                filename = os.path.relpath(file_path, self.memory_dir)
            except ValueError:
                filename = str(file_path)

            content_lines = [
                "[execution_memory]",
                f"goal: {goal or 'unknown'}",
                f"phase: {phase or 'unknown'}",
                f"tool_name: {tool_name or 'none'}",
                f"success: {success}",
            ]
            if blockage_reason:
                content_lines.append(f"blockage_reason: {blockage_reason}")
            if compact_summary:
                content_lines.append(f"summary: {compact_summary}")
            if lesson:
                content_lines.append(f"lesson: {lesson}")
            if next_actions:
                joined_actions = ", ".join(str(action) for action in next_actions if str(action).strip())
                if joined_actions:
                    content_lines.append(f"next_actions: {joined_actions}")
            content = "\n".join(content_lines)

            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(content)

            if not self.enable_network:
                self._maybe_cleanup_execution_logs()
                return {
                    "success": True,
                    "filename": filename,
                    "network_saved": False,
                    "message": "execution memory persisted without network node",
                }

            tags = self._execution_memory_tags(goal, phase, tool_name, success, blockage_reason, next_actions)
            activation_keywords = self._build_activation_keywords(compact_summary, content, tags)
            cognitive_type = "workflow" if success else ("lesson" if lesson else "risk")

            node_id = self.memory_graph.add_node(
                content,
                filename,
                timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                compact_summary,
                tags,
                cognitive_type=cognitive_type,
                cognitive_state="active",
                confidence=0.86 if success else 0.82,
                priority=0.78 if success else 0.88,
                scope="current",
                source_kind="execution_episode",
                activation_keywords=activation_keywords,
            )

            for tag in tags:
                self.tag_index[tag].add(node_id)

            self._auto_connect_node(node_id, content)
            self._connect_execution_outcome(node_id, phase, tool_name, blockage_reason, next_actions)
            self._maybe_cleanup_execution_logs()

            return {
                "success": True,
                "filename": filename,
                "node_id": node_id,
                "tags": tags,
                "summary": compact_summary,
                "cognitive_type": cognitive_type,
                "network_saved": True,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"write_execution_memory failed: {str(e)}"
            }

    def _maybe_cleanup_execution_logs(self) -> None:
        now = datetime.now()
        if (
            self._last_execution_log_cleanup is not None
            and (now - self._last_execution_log_cleanup).total_seconds()
            < self.EXECUTION_LOG_CLEANUP_INTERVAL_SECONDS
        ):
            return
        self.cleanup_execution_logs(retention_days=self.EXECUTION_LOG_RETENTION_DAYS)
        self._last_execution_log_cleanup = now

    def _execution_memory_tags(
        self,
        goal: str,
        phase: str,
        tool_name: str,
        success: bool,
        blockage_reason: str = None,
        next_actions: List[str] = None,
    ) -> List[str]:
        tags = ["execution_memory", phase or "unknown_phase", tool_name or "unknown_tool"]
        tags.append("execution_success" if success else "execution_failure")
        if blockage_reason:
            tags.append(str(blockage_reason))
        if goal:
            for token in re.findall(r'[\u4e00-\u9fffA-Za-z0-9_]{2,16}', goal.lower()):
                if token not in tags:
                    tags.append(token)
                if len(tags) >= 10:
                    break
        for action in next_actions or []:
            normalized = str(action).strip().lower()
            if normalized and normalized not in tags:
                tags.append(normalized)
            if len(tags) >= 12:
                break
        return tags

    def _connect_execution_outcome(
        self,
        node_id: str,
        phase: str,
        tool_name: str,
        blockage_reason: str = None,
        next_actions: List[str] = None,
    ):
        current_node = self.memory_graph.nodes.get(node_id)
        if not current_node:
            return

        current_tags = set(current_node.tags)
        recent_items = list(self.memory_graph.nodes.items())[-25:]
        for other_id, other_node in recent_items:
            if other_id == node_id:
                continue

            relation_type = None
            weight = 0.0
            searchable = " ".join(other_node.tags + other_node.activation_keywords + [other_node.summary]).lower()

            if blockage_reason and other_node.cognitive_type == "lesson" and str(blockage_reason).lower() in searchable:
                relation_type = "failure_pattern"
                weight = 0.78
            elif phase and other_node.cognitive_type == "workflow" and (phase.lower() in searchable or str(tool_name).lower() in searchable):
                relation_type = "phase_tool_pattern"
                weight = 0.72
            elif current_tags & set(other_node.tags):
                relation_type = "shared_context"
                weight = 0.55
            elif next_actions and any(str(action).lower() in searchable for action in next_actions):
                relation_type = "recommended_recovery"
                weight = 0.63

            if relation_type:
                self.memory_graph.add_relation(node_id, other_id, relation_type, weight)

    def cleanup_memories(self, days_old: int = 30, min_importance: float = 0.3) -> Dict[str, Any]:
        try:
            cutoff_time = datetime.now() - timedelta(days=days_old)
            to_delete = []
            
            for node_id, node in self.memory_graph.nodes.items():
                created_at = datetime.fromisoformat(node.created_at)
                if created_at < cutoff_time and node.importance < min_importance:
                    to_delete.append(node.filename)
            
            if to_delete:
                result = self.batch_delete_memories(to_delete)
                return {
                    "success": True,
                    "days_old": days_old,
                    "min_importance": min_importance,
                    "deleted_count": len(to_delete),
                    "deleted_files": to_delete,
                    "message": f"清理了 {len(to_delete)} 个旧记忆"
                }
            else:
                return {
                    "success": True,
                    "days_old": days_old,
                    "min_importance": min_importance,
                    "deleted_count": 0,
                    "message": "没有需要清理的记忆"
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"清理记忆失败: {str(e)}"
            }

    def cleanup_execution_logs(self, retention_days: int = EXECUTION_LOG_RETENTION_DAYS) -> Dict[str, Any]:
        """删除执行日志目录中超过 retention_days 的旧文件。

        执行日志在工具每次运行时产生，积累速度极快（单次会话可产生上千份）。
        纯本地文件级清理：扫描 mtime，过期的直接删除。
        同时清理图网络中对应节点（如有）。
        """
        import os as _os

        try:
            cutoff_time = datetime.now() - timedelta(days=retention_days)
            log_dir = self.execution_log_dir
            if not log_dir.exists():
                return {
                    "success": True,
                    "deleted_file_count": 0,
                    "message": "执行日志目录不存在，无需清理",
                }

            to_delete = []
            for file_path in log_dir.iterdir():
                if not file_path.is_file() or not file_path.suffix == ".txt":
                    continue
                try:
                    mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if mtime < cutoff_time:
                        to_delete.append(file_path)
                except OSError:
                    continue

            for file_path in to_delete:
                try:
                    file_path.unlink()
                except OSError:
                    continue

            # 图网络清理：用文件名（不含路径）匹配
            deleted_names = {p.name for p in to_delete}
            removed_nodes = 0
            for node_id, node in list(self.memory_graph.nodes.items()):
                node_filename = _os.path.basename(str(node.filename))
                if node_filename in deleted_names:
                    for tag in node.tags:
                        if tag in self.tag_index and node_id in self.tag_index[tag]:
                            self.tag_index[tag].discard(node_id)
                    del self.memory_graph.nodes[node_id]
                    if node_id in self.memory_graph.edges:
                        del self.memory_graph.edges[node_id]
                    removed_nodes += 1

            if removed_nodes:
                for source_id, targets in list(self.memory_graph.edges.items()):
                    targets[:] = [t for t in targets if t in self.memory_graph.nodes]

            return {
                "success": True,
                "retention_days": retention_days,
                "deleted_file_count": len(to_delete),
                "deleted_node_count": removed_nodes,
                "message": f"清理了 {len(to_delete)} 个执行日志文件（保留 {retention_days} 天内）",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"清理执行日志失败: {str(e)}",
            }

    def export_memories(self, format: str = "json", include_content: bool = True) -> Dict[str, Any]:
        try:
            format = format.lower().strip()
            if format not in {"json", "txt"}:
                return {
                    "success": False,
                    "error": f"不支持的导出格式: {format}，仅支持 json 或 txt"
                }

            export_data = {
                "export_time": datetime.now().isoformat(),
                "total_memories": 0,
                "memories": []
            }
            
            list_result = self.list_memories(limit=1000, sort_by='oldest')
            if not list_result["success"]:
                return list_result
            
            for mem in list_result["memories"]:
                modified_time_str = datetime.fromtimestamp(mem["modified_time"]).strftime("%Y-%m-%d %H:%M:%S")
                memory_data = {
                    "filename": mem["filename"],
                    "modified_time": modified_time_str,
                    "modified_timestamp": mem["modified_time"],
                    "size": mem["size"]
                }
                
                if include_content:
                    read_result = self.read_memory(mem["filename"])
                    if read_result["success"]:
                        memory_data["content"] = read_result.get("content", 
                                                               read_result.get("content_preview", ""))
                
                node_id = self._find_node_by_filename(mem["filename"])
                if node_id and node_id in self.memory_graph.nodes:
                    node = self.memory_graph.nodes[node_id]
                    memory_data["tags"] = node.tags
                    memory_data["importance"] = node.importance
                    memory_data["access_count"] = node.access_count
                else:
                    memory_data["tags"] = []
                    memory_data["importance"] = 1.0
                    memory_data["access_count"] = 0
                
                export_data["memories"].append(memory_data)
            
            export_data["total_memories"] = len(export_data["memories"])
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"memory_export_{timestamp}.{format}"
            export_path = self.memory_dir.parent / export_filename
            
            if format == "json":
                with open(export_path, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, ensure_ascii=False, indent=2)
            elif format == "txt":
                with open(export_path, 'w', encoding='utf-8') as f:
                    for mem in export_data["memories"]:
                        f.write(f"=== {mem['filename']} ===\n")
                        f.write(f"时间: {mem['modified_time']}\n")
                        f.write(f"大小: {mem.get('size', 0)} 字节\n")
                        f.write(f"标签: {', '.join(mem.get('tags', []))}\n")
                        f.write(f"重要性: {mem.get('importance', 1.0)}\n")
                        f.write(f"访问次数: {mem.get('access_count', 0)}\n")
                        if 'content' in mem and mem['content']:
                            f.write(f"内容:\n{mem.get('content', '')}\n")
                        f.write("\n")
            
            return {
                "success": True,
                "export_path": str(export_path),
                "format": format,
                "total_memories": export_data["total_memories"],
                "message": f"成功导出 {export_data['total_memories']} 条记忆到 {export_filename}"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"导出记忆失败: {str(e)}"
            }

class SmartMemoryToolManager:
    """智能记忆工具管理器"""
    
    def __init__(self, *_, enable_network: bool = False,
                 execution_log_dir: str = None, **_compat_kwargs):
        self.enable_network = False
        self.execution_log_dir = execution_log_dir
        self.handlers = {}
    
    def _get_handler(self) -> SmartMemoryHandler:
        key = "fixed_memory_write"
        
        if key not in self.handlers:
            self.handlers[key] = SmartMemoryHandler(
                enable_network=self.enable_network,
                execution_log_dir=self.execution_log_dir,
            )
        
        return self.handlers[key]
    
    def list_memories(self, limit: int = 20, sort_by: str = 'newest') -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.list_memories(limit, sort_by)
    
    def read_memory(self, filename: str, encoding: str = 'utf-8') -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.read_memory(filename, encoding)
    
    def search_memories(self, keyword: str, limit: int = 10, 
                       case_sensitive: bool = False,
                       tags: List[str] = None) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.search_memories(keyword, limit, case_sensitive, tags)
    
    def write_memory(self, content: str, summary: str = None,
                    encoding: str = 'utf-8', tags: List[str] = None) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.write_memory(content, summary, encoding, tags)
    
    def get_memory_with_tags(self, limit: int = 20, sort_by: str = 'newest') -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.get_memory_with_tags(limit, sort_by)
    
    def get_tag_stats(self) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.get_tag_stats()
    
    def list_cognitive_memories(self, cognitive_type: str = None, state: str = "active",
                                limit: int = 20) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.list_cognitive_memories(cognitive_type, state, limit)

    def get_cognitive_state(self, current_query: str = "", limit: int = 10) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.get_cognitive_state(current_query, limit)

    def build_phase_summary(self, current_query: str = "", current_phase: str = "",
                            current_intent: str = "", limit: int = 8,
                            recent_failures: List[str] = None) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.build_phase_summary(current_query, current_phase, current_intent, limit, recent_failures)

    def get_activation_set(self, current_query: str = "", current_phase: str = "",
                           current_intent: str = "", limit: int = 5,
                           recent_failures: List[str] = None) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.get_activation_set(current_query, current_phase, current_intent, limit, recent_failures)

    def write_execution_memory(self, goal: str, phase: str, tool_name: str, success: bool,
                               blockage_reason: str = None, lesson: str = "", summary: str = "",
                               next_actions: List[str] = None) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.write_execution_memory(goal, phase, tool_name, success, blockage_reason, lesson, summary, next_actions)
    
    def cleanup_memories(self, days_old: int = 30, min_importance: float = 0.3) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.cleanup_memories(days_old, min_importance)

    def cleanup_execution_logs(self, retention_days: int = SmartMemoryHandler.EXECUTION_LOG_RETENTION_DAYS) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.cleanup_execution_logs(retention_days)

    def export_memories(self, format: str = "json", include_content: bool = True) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.export_memories(format, include_content)
    
    def get_latest_memory(self, encoding: str = 'utf-8') -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.get_latest_memory(encoding)
    
    def get_memory_by_date(self, date_str: str) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.get_memory_by_date(date_str)
    
    def get_memory_summary(self, limit: int = 5) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.get_memory_summary(limit)
    
    def append_to_memory(self, filename: str, content: str, 
                         encoding: str = 'utf-8') -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.append_to_memory(filename, content, encoding)
    
    def batch_delete_memories(self, filenames: List[str]) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.batch_delete_memories(filenames)
    
    def delete_memory(self, filename: str) -> Dict[str, Any]:
        handler = self._get_handler()
        return handler.delete_memory(filename)

default_manager = SmartMemoryToolManager()

def list_memories(limit: int = 20, sort_by: str = 'newest'):
    return default_manager.list_memories(limit, sort_by)

def read_memory(filename: str, encoding: str = 'utf-8'):
    return default_manager.read_memory(filename, encoding)

def search_memories(keyword: str, limit: int = 10, case_sensitive: bool = False, 
                   tags: List[str] = None):
    return default_manager.search_memories(keyword, limit, case_sensitive, tags)

def write_memory(content: str, summary: str = None, encoding: str = 'utf-8',
                tags: List[str] = None):
    return default_manager.write_memory(content, summary, encoding, tags)

def get_memory_with_tags(limit: int = 20, sort_by: str = 'newest'):
    return default_manager.get_memory_with_tags(limit, sort_by)

def get_tag_stats():
    return default_manager.get_tag_stats()

def list_cognitive_memories(cognitive_type: str = None, state: str = "active",
                           limit: int = 20):
    return default_manager.list_cognitive_memories(cognitive_type, state, limit)

def get_cognitive_state(current_query: str = "", limit: int = 10):
    return default_manager.get_cognitive_state(current_query, limit)

def build_phase_summary(current_query: str = "", current_phase: str = "", current_intent: str = "",
                       limit: int = 8, recent_failures: List[str] = None):
    return default_manager.build_phase_summary(
        current_query,
        current_phase,
        current_intent,
        limit,
        recent_failures,
    )

def get_activation_set(current_query: str = "", current_phase: str = "", current_intent: str = "",
                      limit: int = 5, recent_failures: List[str] = None):
    return default_manager.get_activation_set(
        current_query,
        current_phase,
        current_intent,
        limit,
        recent_failures,
    )

def write_execution_memory(goal: str, phase: str, tool_name: str, success: bool,
                           blockage_reason: str = None, lesson: str = "", summary: str = "",
                          next_actions: List[str] = None):
    return default_manager.write_execution_memory(
        goal,
        phase,
        tool_name,
        success,
        blockage_reason,
        lesson,
        summary,
        next_actions,
    )

def cleanup_memories(days_old: int = 30, min_importance: float = 0.3):
    return default_manager.cleanup_memories(days_old, min_importance)

def cleanup_execution_logs(
    retention_days: int = SmartMemoryHandler.EXECUTION_LOG_RETENTION_DAYS,
):
    return default_manager.cleanup_execution_logs(retention_days)

def export_memories(format: str = "json", include_content: bool = True):
    return default_manager.export_memories(format, include_content)

def get_latest_memory(encoding: str = 'utf-8'):
    return default_manager.get_latest_memory(encoding)

def get_memory_by_date(date_str: str):
    return default_manager.get_memory_by_date(date_str)

def get_memory_summary(limit: int = 5):
    return default_manager.get_memory_summary(limit)

def append_to_memory(filename: str, content: str, encoding: str = 'utf-8'):
    return default_manager.append_to_memory(filename, content, encoding)

def batch_delete_memories(filenames: List[str]):
    return default_manager.batch_delete_memories(filenames)

def delete_memory(filename: str):
    return default_manager.delete_memory(filename)
