#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
上下文管理工具
让智能体能够主动管理对话上下文，包括查询状态和清理上下文
"""

import os
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from xenon_core.context_runtime import (
        ContextManager as XenonContextManager,
        MAX_CONTEXT_TOKENS_DEFAULT,
        TokenCounter,
        TIKTOKEN_AVAILABLE,
    )
except ImportError:
    XenonContextManager = None
    MAX_CONTEXT_TOKENS_DEFAULT = 50000
    TokenCounter = None
    TIKTOKEN_AVAILABLE = False


class ContextManager:
    """上下文管理工具类，供智能体主动调用"""
    
    def __init__(self, max_context_tokens: Optional[int] = None, context_manager: Any = None):
        self.memory_dir = Path("Memory/memory_Write")
        self.max_context_tokens = int(
            max_context_tokens
            or getattr(context_manager, "max_context_tokens", 0)
            or MAX_CONTEXT_TOKENS_DEFAULT
        )
        
        self.context_manager = context_manager
        if self.context_manager is None and TIKTOKEN_AVAILABLE and XenonContextManager:
            try:
                self.context_manager = XenonContextManager(
                    memory_dir=str(self.memory_dir),
                    max_context_tokens=self.max_context_tokens
                )
            except Exception as e:
                print(f"上下文管理器初始化失败: {e}")
    
    def get_context_status(self) -> Dict[str, Any]:
        """
        获取当前上下文状态信息
        
        Returns:
            包含上下文状态信息的字典，包括：
            - token_counter_available: token计数器是否可用
            - max_context_tokens: 最大token限制
            - cleanup_count: 清理次数
            - last_cleanup_time: 上次清理时间
            - current_summary_length: 当前摘要长度
            - cleanup_thresholds: 清理阈值配置
        """
        if not self.context_manager:
            return {
                "success": False,
                "error": "上下文管理器不可用（tiktoken未安装或初始化失败）",
                "token_counter_available": False
            }
        
        try:
            status = self.context_manager.get_context_status()
            status["success"] = True
            status["message"] = "成功获取上下文状态"
            return status
        except Exception as e:
            return {
                "success": False,
                "error": f"获取上下文状态失败: {str(e)}",
                "token_counter_available": self.context_manager is not None
            }
    
    def estimate_current_tokens(self, system_prompt: str, memories: List[str], current_query: str) -> Dict[str, Any]:
        """
        估计当前上下文的token使用量
        
        Args:
            system_prompt: 系统提示词内容
            memories: 历史对话记忆列表
            current_query: 当前用户查询
        
        Returns:
            包含token估计信息的字典
        """
        if not self.context_manager or not self.context_manager.token_counter:
            return {
                "success": False,
                "error": "Token计数器不可用",
                "tokens": None,
                "percentage": None
            }
        
        try:
            tokens = self.context_manager.estimate_current_tokens(
                system_prompt, memories, current_query
            )
            
            if tokens is None:
                return {
                    "success": False,
                    "error": "无法估计token使用量",
                    "tokens": None,
                    "percentage": None
                }
            
            max_tokens = self._configured_max_tokens()
            trigger_ratio = getattr(self.context_manager, "cleanup_thresholds", {}).get("trigger", 0.8)
            trigger_tokens = int(max_tokens * trigger_ratio)
            output_reserve = int(getattr(self.context_manager, "output_token_reserve", 0) or 0)

            percentage = self.context_manager.token_counter.get_token_usage_percentage(tokens, max_tokens)
            level = self.context_manager.token_counter.get_token_usage_warning_level(tokens, max_tokens)
            recommendation = self.context_manager.token_counter.get_recommendation(tokens, max_tokens)
            
            return {
                "success": True,
                "tokens": tokens,
                "max_tokens": max_tokens,
                "configured_max_tokens": max_tokens,
                "output_token_reserve": output_reserve,
                "cleanup_trigger_tokens": trigger_tokens,
                "cleanup_trigger_ratio": trigger_ratio,
                "percentage": percentage,
                "level": level,
                "recommendation": recommendation,
                "message": (
                    f"当前使用 {tokens:,}/{max_tokens:,} tokens ({percentage}%)，"
                    f"自动压缩阈值 {trigger_tokens:,} ({trigger_ratio:.0%})，状态: {level}"
                )
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"估计token失败: {str(e)}",
                "tokens": None,
                "percentage": None
            }
    
    def should_cleanup_context(self, current_tokens: int) -> Dict[str, Any]:
        """
        判断是否应该清理上下文
        
        Args:
            current_tokens: 当前token使用量
        
        Returns:
            包含清理建议的字典
        """
        if not self.context_manager:
            return {
                "success": False,
                "error": "上下文管理器不可用",
                "should_cleanup": False,
                "level": "unknown",
                "reason": "无法判断"
            }
        
        try:
            should_cleanup, level, reason = self.context_manager.should_cleanup_context(current_tokens)
            
            return {
                "success": True,
                "should_cleanup": should_cleanup,
                "level": level,
                "reason": reason,
                "max_tokens": self._configured_max_tokens(),
                "message": f"清理级别: {level}，原因: {reason}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"判断清理需求失败: {str(e)}",
                "should_cleanup": False,
                "level": "unknown",
                "reason": str(e)
            }
    
    def load_memory_summaries(self, limit: int = 5) -> Dict[str, Any]:
        """
        加载记忆摘要
        
        Args:
            limit: 最多加载的记忆数量
        
        Returns:
            包含记忆摘要列表的字典
        """
        if not self.context_manager:
            return {
                "success": False,
                "error": "上下文管理器不可用",
                "summaries": []
            }
        
        try:
            summaries = self.context_manager.load_memory_summaries(limit)
            
            return {
                "success": True,
                "summaries": summaries,
                "count": len(summaries),
                "message": f"成功加载 {len(summaries)} 条记忆摘要"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"加载记忆摘要失败: {str(e)}",
                "summaries": []
            }
    
    def generate_network_summary(self, topic: Optional[str] = None) -> Dict[str, Any]:
        """
        生成网络图谱摘要
        
        Args:
            topic: 当前话题，用于聚焦摘要
        
        Returns:
            包含网络摘要的字典
        """
        if not self.context_manager:
            return {
                "success": False,
                "error": "上下文管理器不可用",
                "summary": ""
            }
        
        try:
            summary = self.context_manager.generate_network_summary(topic)
            
            return {
                "success": True,
                "summary": summary,
                "topic": topic,
                "message": "成功生成网络摘要"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"生成网络摘要失败: {str(e)}",
                "summary": ""
            }
    
    def cleanup_and_reload_context(self, current_query: str, topic: Optional[str] = None) -> Dict[str, Any]:
        """
        清理并重新加载上下文（返回新的上下文消息列表）
        
        Args:
            current_query: 当前用户查询
            topic: 当前话题
        
        Returns:
            包含新上下文和清理报告的字典
        """
        if not self.context_manager:
            return {
                "success": False,
                "error": "上下文管理器不可用",
                "new_context": [],
                "cleanup_report": ""
            }
        
        # 检查清理阻塞标记，防止递归清理
        if getattr(self.context_manager, "is_cleanup_blocked", None) and self.context_manager.is_cleanup_blocked():
            block_until = getattr(self.context_manager, "cleanup_block_until", None)
            msg = "上下文清理已被临时锁定"
            if block_until:
                remaining = int((block_until - datetime.now()).total_seconds() / 60)
                msg += f"（剩余约 {remaining} 分钟），上次清理完成不久，请勿重复清理"
            return {
                "success": False,
                "error": msg,
                "new_context": [],
                "cleanup_report": "",
                "blocked": True
            }
        
        try:
            new_context, cleanup_report = self._build_simple_cleanup_context(current_query)
            
            return {
                "success": True,
                "new_context": new_context,
                "cleanup_report": cleanup_report,
                "message": "上下文清理并重新加载成功"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"清理上下文失败: {str(e)}",
                "new_context": [],
                "cleanup_report": ""
            }
    
    def _build_simple_cleanup_context(self, current_query: str) -> Tuple[List[Dict[str, Any]], str]:
        now = datetime.now()
        if hasattr(self.context_manager, "last_cleanup_time"):
            self.context_manager.last_cleanup_time = now
        if hasattr(self.context_manager, "cleanup_block_until"):
            duration = getattr(self.context_manager, "cleanup_block_duration_minutes", 0) or 0
            self.context_manager.cleanup_block_until = now + timedelta(minutes=float(duration))
        if hasattr(self.context_manager, "cleanup_count"):
            self.context_manager.cleanup_count += 1
        if hasattr(self.context_manager, "current_context_summary"):
            self.context_manager.current_context_summary = ""

        new_context = [{"role": "user", "content": current_query}]
        cleanup_report = (
            "Simple context cleanup completed: old compact turns should be dropped, "
            "the latest 2-3 turns and current user request should be kept. "
            "No raw tool logs or generated network summary were added."
        )
        return new_context, cleanup_report

    def adjust_cleanup_thresholds(self, warning: Optional[int] = None, recommend: Optional[int] = None, critical: Optional[int] = None) -> Dict[str, Any]:
        """
        调整清理阈值
        
        Args:
            warning: 警告阈值（百分比，0-100）
            recommend: 建议清理阈值
            critical: 强制清理阈值
        
        Returns:
            操作结果
        """
        if not self.context_manager:
            return {
                "success": False,
                "error": "上下文管理器不可用"
            }
        
        try:
            self.context_manager.adjust_cleanup_thresholds(warning, recommend, critical)
            
            return {
                "success": True,
                "message": "清理阈值已更新",
                "new_thresholds": self.context_manager.cleanup_thresholds
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"调整阈值失败: {str(e)}"
            }

    def _configured_max_tokens(self) -> int:
        return int(getattr(self.context_manager, "max_context_tokens", None) or self.max_context_tokens)
