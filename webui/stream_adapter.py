import sys
import json
import io
import asyncio
import copy
import logging
import os
import threading
import time
import traceback
from queue import Queue, Empty
from typing import Dict, List, Any, Generator, Optional, Callable
from contextlib import redirect_stdout, redirect_stderr

logger = logging.getLogger(__name__)
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


STREAM_QUEUE_POLL_SECONDS = max(_env_float("XENON_WEBUI_STREAM_POLL_SECONDS", 0.25), 0.05)
STREAM_HEARTBEAT_SECONDS = max(_env_float("XENON_WEBUI_STREAM_HEARTBEAT_SECONDS", 15.0), 0.0)
STREAM_IDLE_TIMEOUT_SECONDS = max(_env_float("XENON_WEBUI_STREAM_IDLE_TIMEOUT", 300.0), 0.0)
STREAM_WORKER_JOIN_SECONDS = max(_env_float("XENON_WEBUI_STREAM_WORKER_JOIN_SECONDS", 3.0), 0.0)
STREAM_INTERRUPT_JOIN_SECONDS = max(_env_float("XENON_WEBUI_STREAM_INTERRUPT_JOIN_SECONDS", 2.0), 0.0)


@dataclass
class StreamEvent:
    type: str
    content: str = ""
    tool_call: Optional[Dict] = None
    tool_call_id: Optional[str] = None
    reasoning_content: str = ""
    tool_result: str = ""
    tool_name: str = ""
    arguments: str = ""
    error: Optional[str] = None  # 新增错误字段，用于传递异常信息
    queue_position: int = 0      # 排队消息在队列中的位置
    queue_remaining: int = 0     # 队列中剩余消息数


class AIAgentStreamAdapter:
    def __init__(self, agent):
        self.agent = agent
        self.lock = threading.Lock()
        self.last_message_count = 0
        self._state_lock = threading.RLock()
        self._active_queue: Optional[Queue] = None
        self._active_stop_event: Optional[threading.Event] = None
        self._active_worker_thread: Optional[threading.Thread] = None

    def _ensure_no_active_worker(self):
        with self._state_lock:
            worker_thread = self._active_worker_thread

        if worker_thread is not None and worker_thread.is_alive():
            self.interrupt()
            worker_thread.join(timeout=STREAM_WORKER_JOIN_SECONDS)
            if worker_thread.is_alive():
                raise RuntimeError("Previous chat stream is still shutting down. Please retry shortly.")
            return

        # ★ 兜底：即使线程引用丢失，如果 agent 仍在处理中（_turn_running=True），
        # 也拒绝启动新 worker，防止两个 chat 线程同时操作同一个 agent
        if getattr(self.agent, '_turn_running', False):
            self.agent.interrupted = True
            # 走到这里说明 worker 线程引用已死（或不存在）。_turn_running 正常由
            # worker 线程的 finally 重置，线程死亡即标志复位；若标志仍为 True，
            # 属于异常路径的残留状态。短暂等待后强制复位实现自愈，
            # 避免用户被永久卡在 "still processing" 只能重启程序。
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and getattr(self.agent, '_turn_running', False):
                time.sleep(0.05)
            if getattr(self.agent, '_turn_running', False):
                logger.warning(
                    "[stream_adapter] _turn_running stale with no live worker thread; "
                    "force-resetting to allow a new stream"
                )
                self.agent._turn_running = False

    def _capture_context_snapshot(self):
        context = copy.deepcopy(getattr(self.agent, "current_context", []))
        if hasattr(self.agent, "get_full_context"):
            full_context = self.agent.get_full_context()
        else:
            full_context = getattr(self.agent, "full_conversation_history", context)
        return context, copy.deepcopy(full_context)

    def _restore_context_snapshot(self, context, full_context):
        if hasattr(self.agent, "current_context"):
            self.agent.current_context = copy.deepcopy(context)

        if hasattr(self.agent, "set_full_context"):
            self.agent.set_full_context(full_context)
        elif hasattr(self.agent, "full_conversation_history"):
            self.agent.full_conversation_history = copy.deepcopy(full_context)

        self.last_message_count = len(getattr(self.agent, "current_context", []))

    def _clear_active_state(self, event_queue, stop_event, worker_thread=None):
        with self._state_lock:
            if worker_thread is not None and self._active_worker_thread is worker_thread:
                # ★ 关键修复：如果工作线程还活着（被阻塞在工具调用中），
                # 不要清除引用。否则后续请求会认为没有活跃 worker，
                # 创建重叠的 chat 线程，导致状态混乱。
                if not worker_thread.is_alive():
                    self._active_worker_thread = None
            if self._active_queue is event_queue:
                self._active_queue = None
            if self._active_stop_event is stop_event:
                self._active_stop_event = None

    def has_pending_worker(self) -> bool:
        with self._state_lock:
            worker_thread = self._active_worker_thread
        if worker_thread is not None and worker_thread.is_alive():
            return True
        # ★ 兜底：即使线程引用丢失，如果 agent 内部仍在处理（_turn_running），
        # 也视为有活跃 worker，防止创建重叠的 chat 线程
        if getattr(self.agent, '_turn_running', False):
            return True
        return False

    def wait_for_worker(self, timeout: Optional[float] = None) -> bool:
        with self._state_lock:
            worker_thread = self._active_worker_thread

        if worker_thread is None:
            return True
        worker_thread.join(STREAM_INTERRUPT_JOIN_SECONDS if timeout is None else timeout)
        if worker_thread.is_alive():
            return False
        return True
    
    def stream_chat(self, user_input: str) -> Generator[StreamEvent, None, None]:
        logger.info(
            "[stream_adapter] stream_chat starting, user_input=%r, agent.interrupted=%s",
            user_input[:80], getattr(self.agent, "interrupted", None),
        )
        self._ensure_no_active_worker()
        # ★ 重置中断标志：确保上一轮残留的 interrupted=True 不会让新流的生成器循环立即 break
        self.agent.interrupted = False
        yield StreamEvent(type='user', content=user_input)

        event_queue: Queue = Queue()
        stop_event = threading.Event()
        context_snapshot, full_context_snapshot = self._capture_context_snapshot()
        with self._state_lock:
            self._active_queue = event_queue
            self._active_stop_event = stop_event
        
        def callback(event_dict):
            if stop_event.is_set():
                return
            event = StreamEvent(
                type=event_dict.get('type', ''),
                content=event_dict.get('content', ''),
                tool_name=event_dict.get('tool_name', ''),
                tool_call_id=event_dict.get('tool_call_id', ''),
                arguments=event_dict.get('arguments', ''),
                queue_position=event_dict.get('queue_position', 0),
                queue_remaining=event_dict.get('queue_remaining', 0),
            )
            event_queue.put(event)
        
        self.agent.set_stream_callback(callback)
        
        self.last_message_count = len(self.agent.current_context)
        
        def run_chat():
            logger.info("[stream_adapter] chat thread starting for user_input=%r", user_input[:80])
            chat_start = time.monotonic()
            try:
                self.agent.chat(user_input)
            except Exception as e:
                # 捕获完整的异常信息
                error_traceback = traceback.format_exc()
                if not stop_event.is_set():
                    # 将错误事件放入队列
                    event_queue.put(StreamEvent(type='error', content=str(e), error=str(e)))
                    print(f"Chat error: {e}\n{error_traceback}", file=sys.stderr)
            finally:
                chat_elapsed = time.monotonic() - chat_start
                logger.info(
                    "[stream_adapter] chat thread finished, elapsed=%.1fs, stop_event=%s, agent.interrupted=%s",
                    chat_elapsed, stop_event.is_set(), getattr(self.agent, "interrupted", None),
                )
                if stop_event.is_set():
                    self._restore_context_snapshot(context_snapshot, full_context_snapshot)
                event_queue.put(None)
                self._clear_active_state(event_queue, stop_event, threading.current_thread())
        
        chat_thread = threading.Thread(target=run_chat, daemon=True)
        with self._state_lock:
            self._active_worker_thread = chat_thread
        chat_thread.start()
        
        try:
            last_activity = time.monotonic()
            last_heartbeat = last_activity
            event_count = 0
            heartbeat_count = 0
            while True:
                if stop_event.is_set() or getattr(self.agent, "interrupted", False):
                    logger.warning(
                        "[stream_adapter] generator loop breaking: stop_event=%s, interrupted=%s, events=%d, heartbeats=%d",
                        stop_event.is_set(), getattr(self.agent, "interrupted", None),
                        event_count, heartbeat_count,
                    )
                    break

                try:
                    event = event_queue.get(timeout=STREAM_QUEUE_POLL_SECONDS)
                except Empty:
                    now = time.monotonic()
                    if (
                        STREAM_IDLE_TIMEOUT_SECONDS
                        and now - last_activity >= STREAM_IDLE_TIMEOUT_SECONDS
                    ):
                        logger.error(
                            "[stream_adapter] IDLE TIMEOUT after %.0fs (limit=%.0fs), events=%d, heartbeats=%d",
                            now - last_activity, STREAM_IDLE_TIMEOUT_SECONDS,
                            event_count, heartbeat_count,
                        )
                        self.agent.interrupted = True
                        yield StreamEvent(
                            type='error',
                            content=(
                                "Web UI stream idle timeout. "
                                f"No events were produced for {STREAM_IDLE_TIMEOUT_SECONDS:.0f} seconds."
                            ),
                        )
                        break
                    if (
                        STREAM_HEARTBEAT_SECONDS
                        and now - last_heartbeat >= STREAM_HEARTBEAT_SECONDS
                    ):
                        last_heartbeat = now
                        last_activity = now  # ★ 心跳也是一种活动，防止误触发空闲超时
                        heartbeat_count += 1
                        yield StreamEvent(type='heartbeat')
                    continue

                if event is None:
                    logger.info(
                        "[stream_adapter] received None sentinel, events=%d, heartbeats=%d",
                        event_count, heartbeat_count,
                    )
                    break
                last_activity = time.monotonic()
                event_count += 1
                if event.type == 'heartbeat':
                    heartbeat_count += 1
                yield event
            
            self.last_message_count = len(self.agent.current_context)
            
            logger.info(
                "[stream_adapter] generator yielding done, events=%d, heartbeats=%d",
                event_count, heartbeat_count,
            )
            yield StreamEvent(type='done')
        except GeneratorExit:
            # 生成器被外部关闭
            logger.warning(
                "[stream_adapter] GeneratorExit (SSE connection likely dropped), events=%d, heartbeats=%d",
                event_count, heartbeat_count,
            )
            self.agent.interrupted = True
            stop_event.set()
            event_queue.put(None)
            raise
        finally:
            if chat_thread.is_alive():
                chat_thread.join(timeout=STREAM_WORKER_JOIN_SECONDS)
            if chat_thread.is_alive():
                # 线程在超时后仍存活 — 仍清除状态以避免阻塞后续请求
                # agent.interrupted 标志已设置，旧 chat 应自行中止
                logger.warning(
                    "[stream_adapter] chat thread STILL ALIVE after %.0fs join, releasing state anyway",
                    STREAM_WORKER_JOIN_SECONDS,
                )
            self._clear_active_state(event_queue, stop_event, chat_thread)
    
    def get_messages_from_context(self) -> List[Dict[str, Any]]:
        return self.agent.current_context.copy()

    def get_messages_from_full_context(self) -> List[Dict[str, Any]]:
        if hasattr(self.agent, "get_full_context"):
            return self.agent.get_full_context()
        return self.agent.current_context.copy()
    
    def restore_context(self, messages: List[Dict[str, Any]]):
        with self.lock:
            self.agent.current_context.clear()
            self.agent.current_context.extend(messages)
            self.last_message_count = len(messages)

    def restore_full_context(self, messages: List[Dict[str, Any]]):
        if hasattr(self.agent, "set_full_context"):
            self.agent.set_full_context(messages)

    def set_model(self, model: str):
        if hasattr(self.agent, "set_model"):
            return self.agent.set_model(model)
        return model

    def get_model(self) -> str:
        if hasattr(self.agent, "get_model"):
            return self.agent.get_model()
        return ""
    
    def interrupt(self):
        with self._state_lock:
            stop_event = self._active_stop_event
            event_queue = self._active_queue
        if stop_event is not None:
            stop_event.set()
        self.agent.interrupted = True
        if event_queue is not None:
            event_queue.put(None)


class AsyncAIAgentWrapper:
    def __init__(self, agent):
        self.agent = agent
        self.adapter = AIAgentStreamAdapter(agent)
        self._stream_lock = asyncio.Lock()
    
    async def stream_chat_async(self, user_input: str):
        async with self._stream_lock:
            loop = asyncio.get_running_loop()
            
            def generator():
                return self.adapter.stream_chat(user_input)

            gen = generator()
            pending_next = None
            completed_normally = False

            def next_event():
                try:
                    return next(gen)
                except StopIteration:
                    return None

            def close_generator():
                if hasattr(gen, 'close'):
                    try:
                        gen.close()
                    except ValueError:
                        # Generator still executing in thread pool — 
                        # adapter.interrupt() already sent stop signals,
                        # the generator will complete naturally. Safe to ignore.
                        pass
                    except Exception:
                        # Other unexpected errors during close — don't crash the event loop
                        pass
            
            try:
                while True:
                    try:
                        pending_next = loop.run_in_executor(None, next_event)
                        event = await pending_next
                        pending_next = None
                        if event is None:
                            completed_normally = True
                            break
                        yield event
                        if event.type == 'done':
                            completed_normally = True
                            break
                    except asyncio.CancelledError:
                        self.adapter.interrupt()
                        if pending_next is not None and not pending_next.done():
                            try:
                                await asyncio.wait_for(asyncio.shield(pending_next), timeout=1.0)
                            except Exception:
                                pass
                        raise
            finally:
                if not completed_normally:
                    self.adapter.interrupt()
                if pending_next is not None and not pending_next.done():
                    pending_next.add_done_callback(lambda _future: close_generator())
                else:
                    close_generator()
    
    def queue_message(self, message: str):
        """将消息排入 agent 内部队列。在活跃流期间由 WebUI 调用。

        直接调用 AIAgent.chat()，agent 会检测 _turn_running 并自动入队 +
        通过当前活跃的 stream_callback 发送 user_queued 事件到 SSE 流。
        """
        if hasattr(self.agent, 'chat'):
            return self.agent.chat(message)
        return {"queued": False, "reason": "agent_does_not_support_queueing"}

    def interrupt(self):
        self.adapter.interrupt()

    def has_pending_worker(self) -> bool:
        return self.adapter.has_pending_worker()

    def wait_for_worker(self, timeout: Optional[float] = None) -> bool:
        return self.adapter.wait_for_worker(timeout)
    
    def get_context(self) -> List[Dict[str, Any]]:
        return self.adapter.get_messages_from_context()

    def get_full_context(self) -> List[Dict[str, Any]]:
        return self.adapter.get_messages_from_full_context()
    
    def set_context(self, messages: List[Dict[str, Any]]):
        self.adapter.restore_context(messages)

    def set_full_context(self, messages: List[Dict[str, Any]]):
        self.adapter.restore_full_context(messages)

    def set_model(self, model: str):
        return self.adapter.set_model(model)

    def get_model(self) -> str:
        return self.adapter.get_model()
    # ── 属性穿透：暴露底层 AIAgent 的关键属性给 WebUI 使用 ──
    @property
    def context_manager(self):
        return getattr(self.agent, "context_manager", None)

    @property
    def system_prompt(self):
        return getattr(self.agent, "system_prompt", None)

    @property
    def current_context(self):
        return getattr(self.agent, "current_context", [])

    def _get_current_tools(self):
        return getattr(self.agent, "_get_current_tools", lambda: [])()

    def _get_available_tools_message(self, *args, **kwargs):
        fn = getattr(self.agent, "_get_available_tools_message", None)
        if fn:
            return fn(*args, **kwargs)
        return ""


def create_stream_adapter(agent) -> AsyncAIAgentWrapper:
    return AsyncAIAgentWrapper(agent)
