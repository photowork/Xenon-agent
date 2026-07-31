"""
Xenon 消息轮询池 — 异步调用结果与待处理事件的存储中枢

设计原则:
  1. 生产者-消费者分离: 子代理、异步工具等往池中放结果，Xenon 每回合查看
  2. 优先级驱动: 高优先级的消息先被消费
  3. 自动过期: 消息可设置 TTL，超时自动清理（由心跳 tick 驱动）
  4. 合并去重: 同类消息自动合并，防止堆积

与心跳的关系:
  心跳每 5 秒 tick → 清理过期消息 →
  Xenon 每回合 peek 查看 pending 消息，决定处理顺序

用法:
  # 生产者 — 放结果
  from xenon_core.polling_pool import get_pool
  pool = get_pool()
  pool.push(PoolMessage(
      source="sub_agent",
      scenario="code_search",
      msg_type="result",
      payload={"files": [...], "status": "ok"},
      priority=1,
  ))

  # 消费者 — 我每回合查看
  pool = get_pool()
  msgs = pool.peek()    # 看有哪些消息在等
  msg  = pool.pull()    # 拿最高优先级的

与设计文档 v0.3 的关系:
  - 实现了 MessagePollingPool 核心 (push/pull/peek/merge/expire)
  - 调度器和执行引擎暂不在此文件中（后续阶段实现）
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 默认配置 ──
POOL_FILE_NAME = "polling_pool.json"
MAX_CONSUMED_HISTORY = 100   # 已消费记录保留上限
MAX_POOL_SIZE = 500          # 池中消息总数上限（防无限增长）


# ═══════════════════════════════════════════════════════════════════ #
#  PoolMessage — 池内消息数据单元
# ═══════════════════════════════════════════════════════════════════ #

class PoolMessage:
    """池内消息——一个异步调用返回的结果，或一个需要关注的事件。

    属性:
        msg_id:       唯一 ID（自动生成 UUID）
        source:       来源标识，如 "sub_agent" / "async_tool" / "timer" / "system"
        scenario:     场景名，如 "code_search" / "file_analysis" / "health_check"
        msg_type:     消息类型，如 "result" / "observation" / "intervention" / "info"
        payload:      具体数据（由来源方定义）
        priority:     优先级: 0=常规, 1=重要, 2=紧急
        created_at:   创建时间戳 (time.time)
        ttl:          存活秒数（从创建起算），None 表示永不过期
        status:       "pending" / "consumed" / "expired"
        merged_count: 被合并的次数
    """

    def __init__(
        self,
        source: str,
        scenario: str,
        msg_type: str,
        payload: Dict[str, Any],
        priority: int = 0,
        ttl: Optional[float] = None,
        msg_id: Optional[str] = None,
    ) -> None:
        self.msg_id = msg_id or str(uuid.uuid4())
        self.source = source
        self.scenario = scenario
        self.msg_type = msg_type
        self.payload = dict(payload)
        self.priority = priority
        self.created_at = time.time()
        self.ttl = ttl
        self.status = "pending"
        self.merged_count: int = 0

    # ── 便捷属性 ──

    @property
    def is_expired(self) -> bool:
        """判断消息是否已过期"""
        if self.ttl is None:
            return False
        return (time.time() - self.created_at) > self.ttl

    @property
    def age(self) -> float:
        """消息存在时长（秒）"""
        return time.time() - self.created_at

    @property
    def age_human(self) -> str:
        """人类可读的存在时长"""
        s = int(self.age)
        if s < 60:
            return f"{s}s"
        m = s // 60
        s = s % 60
        if m < 60:
            return f"{m}m{s}s"
        h = m // 60
        m = m % 60
        return f"{h}h{m}m"

    # ── 序列化 ──

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "source": self.source,
            "scenario": self.scenario,
            "msg_type": self.msg_type,
            "payload": self.payload,
            "priority": self.priority,
            "created_at": self.created_at,
            "created_at_human": datetime.fromtimestamp(self.created_at).isoformat(),
            "ttl": self.ttl,
            "status": self.status,
            "merged_count": self.merged_count,
            "age_seconds": round(self.age, 1),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PoolMessage":
        """从字典恢复消息（用于持久化加载）"""
        msg = cls(
            source=d["source"],
            scenario=d["scenario"],
            msg_type=d["msg_type"],
            payload=d.get("payload", {}),
            priority=d.get("priority", 0),
            ttl=d.get("ttl"),
            msg_id=d.get("msg_id"),
        )
        msg.created_at = d.get("created_at", time.time())
        msg.status = d.get("status", "pending")
        msg.merged_count = d.get("merged_count", 0)
        return msg

    def __repr__(self) -> str:
        return (
            f"PoolMessage({self.msg_id[:8]}… | "
            f"{self.source}/{self.scenario} | "
            f"pri={self.priority} | "
            f"{self.status})"
        )


# ═══════════════════════════════════════════════════════════════════ #
#  MessagePollingPool — 消息轮询池
# ═══════════════════════════════════════════════════════════════════ #

class MessagePollingPool:
    """消息轮询池——异步结果和待处理事件的核心缓冲区。

    不自持线程，由心跳循环驱动 tick()。
    生产者可随时 push，消费者（Xenon）每回合 peek/pull。
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self._messages: Dict[str, PoolMessage] = {}   # msg_id -> PoolMessage
        self._consumed: List[Dict[str, Any]] = []      # 已消费记录
        self._lock = threading.Lock()
        self._project_root: Optional[Path] = None
        self._pool_file: Optional[Path] = None

        if project_root is not None:
            self.set_project_root(project_root)

    # ═══════════════════════════════════════════════════════════════ #
    #  初始化
    # ═══════════════════════════════════════════════════════════════ #

    def set_project_root(self, project_root: Path) -> None:
        """设置项目根目录（持久化文件写入位置）"""
        self._project_root = Path(project_root)
        self._pool_file = self._project_root / "logs" / POOL_FILE_NAME
        self._pool_file.parent.mkdir(parents=True, exist_ok=True)
        self._load()  # 从文件恢复状态

    # ═══════════════════════════════════════════════════════════════ #
    #  核心操作 — 生产者接口
    # ═══════════════════════════════════════════════════════════════ #

    def push(self, message: PoolMessage) -> str:
        """推送一条消息。同类消息自动合并（同 scenario + 同 msg_type，非紧急时）。

        Returns:
            消息的 msg_id（合并时返回被合并目标的 msg_id）
        """
        with self._lock:
            # 尝试合并（priority < 2 且同场景同类型）
            if message.priority < 2:
                for existing in self._messages.values():
                    if (existing.status == "pending"
                            and existing.scenario == message.scenario
                            and existing.msg_type == message.msg_type):
                        # 合并：保留优先级高的，累加计数
                        existing.merged_count += 1
                        if message.priority > existing.priority:
                            existing.priority = message.priority
                        # 更新 payload（浅合并，新值覆盖旧值）
                        existing.payload.update(message.payload)
                        self._save()
                        return existing.msg_id

            # 不合并，直接放入
            self._messages[message.msg_id] = message

            # 控制总量上限
            if len(self._messages) > MAX_POOL_SIZE:
                self._trim()

            self._save()
            return message.msg_id

    def push_result(
        self,
        source: str,
        scenario: str,
        result: Dict[str, Any],
        priority: int = 0,
        ttl: Optional[float] = None,
    ) -> str:
        """便捷方法：推送一个异步结果消息（msg_type="result"）。"""
        return self.push(PoolMessage(
            source=source,
            scenario=scenario,
            msg_type="result",
            payload=result,
            priority=priority,
            ttl=ttl,
        ))

    def push_event(
        self,
        source: str,
        scenario: str,
        event_type: str,
        payload: Dict[str, Any],
        priority: int = 0,
        ttl: Optional[float] = None,
    ) -> str:
        """便捷方法：推送一个事件消息（msg_type="event"）。"""
        return self.push(PoolMessage(
            source=source,
            scenario=scenario,
            msg_type=event_type,
            payload=payload,
            priority=priority,
            ttl=ttl,
        ))

    # ═══════════════════════════════════════════════════════════════ #
    #  核心操作 — 消费者接口
    # ═══════════════════════════════════════════════════════════════ #

    def pull(self, max_priority: Optional[int] = None) -> Optional[PoolMessage]:
        """取出优先级最高的 pending 消息（消费）。

        Args:
            max_priority: 可选上限，只取出不超过此优先级的消息

        Returns:
            PoolMessage 或 None（无可用消息）
        """
        with self._lock:
            candidates = [
                m for m in self._messages.values()
                if m.status == "pending"
                and (max_priority is None or m.priority <= max_priority)
            ]
            if not candidates:
                return None

            # 按优先级降序 → 按创建时间升序（先来先服务）
            candidates.sort(key=lambda m: (-m.priority, m.created_at))
            msg = candidates[0]
            msg.status = "consumed"

            # 记录消费历史
            record = msg.to_dict()
            record["consumed_at"] = time.time()
            record["consumed_at_human"] = datetime.now().isoformat()
            self._consumed.append(record)

            # 裁剪上限
            if len(self._consumed) > MAX_CONSUMED_HISTORY:
                self._consumed = self._consumed[-MAX_CONSUMED_HISTORY:]

            self._save()
            return msg

    def peek(self, max_count: int = 30) -> List[PoolMessage]:
        """查看当前所有 pending 消息（不消费）。

        Args:
            max_count: 最大返回条数（默认 30，避免撑爆上下文）

        Returns:
            按优先级降序排列的 pending 消息列表
        """
        with self._lock:
            candidates = [
                m for m in self._messages.values()
                if m.status == "pending"
            ]
            candidates.sort(key=lambda m: (-m.priority, m.created_at))
            return candidates[:max_count]

    def peek_summary(self, max_count: int = 15) -> List[Dict[str, Any]]:
        """轻量摘要版 peek——返回关键字段，适合上下文注入。"""
        msgs = self.peek(max_count)
        return [
            {
                "msg_id": m.msg_id[:8],
                "source": m.source,
                "scenario": m.scenario,
                "msg_type": m.msg_type,
                "priority": m.priority,
                "age": m.age_human,
                "summary": str(m.payload)[:120] if m.payload else "",
            }
            for m in msgs
        ]

    # ═══════════════════════════════════════════════════════════════ #
    #  心跳驱动
    # ═══════════════════════════════════════════════════════════════ #

    def tick(self) -> Dict[str, Any]:
        """心跳驱动：清理过期消息。

        由 heartbeat._loop() 每 5 秒调用一次。

        Returns:
            {"status": ..., "expired": int, "pending": int}
        """
        if not self._project_root:
            return {"status": "uninitialized"}

        expired_count = self._expire()

        return {
            "status": "ok",
            "expired": expired_count,
            "pending": self.count_pending(),
        }

    def _expire(self) -> int:
        """清理所有过期消息。返回清理数。"""
        with self._lock:
            expired_ids = [
                mid for mid, msg in self._messages.items()
                if msg.status == "pending" and msg.is_expired
            ]
            for mid in expired_ids:
                self._messages[mid].status = "expired"
            if expired_ids:
                self._save()
            return len(expired_ids)

    # ═══════════════════════════════════════════════════════════════ #
    #  内部维护
    # ═══════════════════════════════════════════════════════════════ #

    def _trim(self) -> None:
        """消息超上限时，丢弃最旧的 pending 消息"""
        pending = [
            m for m in self._messages.values()
            if m.status == "pending"
        ]
        if len(pending) <= MAX_POOL_SIZE:
            return
        pending.sort(key=lambda m: m.created_at)
        to_remove = pending[:len(pending) - MAX_POOL_SIZE]
        for msg in to_remove:
            del self._messages[msg.msg_id]

    # ═══════════════════════════════════════════════════════════════ #
    #  查询
    # ═══════════════════════════════════════════════════════════════ #

    def count_pending(self) -> int:
        """统计 pending 消息数"""
        with self._lock:
            return sum(1 for m in self._messages.values() if m.status == "pending")

    def get_by_id(self, msg_id: str) -> Optional[PoolMessage]:
        """通过 ID 获取消息"""
        with self._lock:
            return self._messages.get(msg_id)

    def get_stats(self) -> Dict[str, Any]:
        """获取池的详细统计"""
        with self._lock:
            pending_list = [m for m in self._messages.values() if m.status == "pending"]

            # 按场景分布
            by_scenario: Dict[str, int] = {}
            for m in pending_list:
                by_scenario[m.scenario] = by_scenario.get(m.scenario, 0) + 1

            # 按优先级分布
            by_priority: Dict[int, int] = {}
            for m in pending_list:
                by_priority[m.priority] = by_priority.get(m.priority, 0) + 1

            return {
                "total_messages": len(self._messages),
                "pending": len(pending_list),
                "consumed_history": len(self._consumed),
                "by_scenario": by_scenario,
                "by_priority": by_priority,
            }

    def get_consumed_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取最近的消费记录"""
        with self._lock:
            return list(self._consumed[-limit:])

    # ═══════════════════════════════════════════════════════════════ #
    #  持久化
    # ═══════════════════════════════════════════════════════════════ #

    def _save(self) -> None:
        """将当前状态持久化到文件"""
        if not self._pool_file:
            return
        try:
            data = {
                "messages": {
                    mid: msg.to_dict()
                    for mid, msg in self._messages.items()
                },
                "consumed": self._consumed[-MAX_CONSUMED_HISTORY:],
                "saved_at": time.time(),
            }
            temp = self._pool_file.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp.replace(self._pool_file)  # 原子替换
        except Exception:
            pass  # 持久化失败非致命

    def _load(self) -> None:
        """从文件恢复状态"""
        if not self._pool_file or not self._pool_file.exists():
            return
        try:
            raw = self._pool_file.read_text(encoding="utf-8")
            if not raw.strip():
                return
            data = json.loads(raw)

            # 恢复消息
            for mid, d in data.get("messages", {}).items():
                try:
                    msg = PoolMessage.from_dict(d)
                    self._messages[mid] = msg
                except Exception:
                    continue  # 单条恢复失败不影响其他

            # 恢复消费记录
            self._consumed = data.get("consumed", [])
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # 恢复失败，从空状态开始


# ═══════════════════════════════════════════════════════════════════ #
#  全局单例管理
# ═══════════════════════════════════════════════════════════════════ #

_pool: Optional[MessagePollingPool] = None
_pool_lock = threading.Lock()


def get_pool(project_root: Optional[Path] = None) -> MessagePollingPool:
    """获取全局消息轮询池单例。

    首次调用时需传入 project_root；后续调用可省略。
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = MessagePollingPool(project_root)
    elif project_root is not None:
        if _pool._project_root is None:
            _pool.set_project_root(project_root)
    return _pool


def reset_pool() -> None:
    """重置全局轮询池（测试/重启用）"""
    global _pool
    with _pool_lock:
        _pool = None
