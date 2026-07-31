"""递归检测器 — 旁路监控模块（v3）

检测 API 请求载荷大小的变化特征，发现递归循环时自动注入跳出指令。
不侵入核心对话逻辑，不限制工具调用次数，不会"变笨"。

工作原理（v3）：
  每次 check_and_inject() 被调用时，计算当前 messages 的
  序列化大小（JSON UTF-8 字节数）。如果连续 threshold 次
  大小完全相同，说明每次递归进入时消息状态没有实质变化——
  典型的递归死循环。此时向消息队列注入一条 system 消息：
  "你正在递归，跳出循环"。

为什么检测"载荷大小"比检测"工具调用内容"更可靠：
  v2 提取 (函数名 + 参数MD5) 指纹 → 只要参数稍微变化（不同
  文件名、不同搜索词），指纹就不一样，检测器认为"正常"。
  但载荷大小反映的是整体状态——无论参数怎么变，只要递归
  循环的"净效果"是状态不再前进，大小就不会变。更简单、更鲁棒。

示例用法::

    detector = RecursionDetector(threshold=3)
    if detector.check_and_inject(
        messages=messages,
        append_message_fn=messages.append,
    ):
        # 已注入跳出消息，继续正常流程即可
        pass
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List


class RecursionDetector:
    """检测递归循环的旁路监控器（v3 — 基于 API 载荷大小）。

    Args:
        threshold: 连续多少次相同载荷大小触发注入。
    """

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = threshold
        self._size_history: List[int] = []
        self._triggered = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def check_and_inject(
        self,
        messages: List[Dict[str, Any]],
        append_message_fn: Callable[[Dict[str, Any]], None],
    ) -> bool:
        """检查是否递归，是则注入跳出消息。

        Args:
            messages: 当前完整的消息列表（即将作为 API 请求载荷发送）。
            append_message_fn: 向消息列表追加单条消息的回调。

        Returns:
            True 表示触发了注入；False 表示一切正常。
        """
        if self._triggered:
            return False  # 已触发过，不再重复

        # 计算当前载荷大小（JSON 序列化后的 UTF-8 字节数）
        current_size = self._compute_payload_size(messages)

        self._size_history.append(current_size)
        if len(self._size_history) > self._threshold:
            self._size_history.pop(0)

        # 还没收集够 threshold 次，不判断
        if len(self._size_history) < self._threshold:
            return False

        # 最近 threshold 次载荷大小全部相同？
        if len(set(self._size_history[-self._threshold:])) == 1:
            self._triggered = True
            append_message_fn({
                "role": "system",
                "content": "你正在递归，跳出循环",
            })
            return True

        return False

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_payload_size(messages: List[Dict[str, Any]]) -> int:
        """计算 messages 序列化后的载荷字节数。

        这是 API 请求中变化最大的部分（tool definitions 和 model
        参数是静态的），因此 messages 大小不变 ≈ 总载荷大小不变。
        """
        try:
            serialized = json.dumps(
                messages,
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )
            return len(serialized.encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            # 序列化失败，返回 -1（不会触发递归检测）
            return -1

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    @property
    def triggered(self) -> bool:
        """是否已经触发过注入。"""
        return self._triggered

    @property
    def size_history(self) -> List[int]:
        """当前记录的载荷大小历史（调试用）。"""
        return list(self._size_history)

    def reset(self) -> None:
        """重置检测器状态，允许重新检测。"""
        self._size_history.clear()
        self._triggered = False
