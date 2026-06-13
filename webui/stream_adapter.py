import sys
import json
import io
import asyncio
import copy
import os
import threading
import time
import traceback
from queue import Queue, Empty
from typing import Dict, List, Any, Generator, Optional, Callable
from contextlib import redirect_stdout, redirect_stderr
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

        if worker_thread is None or not worker_thread.is_alive():
            return

        self.interrupt()
        worker_thread.join(timeout=STREAM_WORKER_JOIN_SECONDS)
        if worker_thread.is_alive():
            raise RuntimeError("Previous chat stream is still shutting down. Please retry shortly.")

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
                self._active_worker_thread = None
            if self._active_queue is event_queue:
                self._active_queue = None
            if self._active_stop_event is stop_event:
                self._active_stop_event = None

    def has_pending_worker(self) -> bool:
        with self._state_lock:
            worker_thread = self._active_worker_thread
        return worker_thread is not None and worker_thread.is_alive()

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
        self._ensure_no_active_worker()
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
                arguments=event_dict.get('arguments', '')
            )
            event_queue.put(event)
        
        self.agent.set_stream_callback(callback)
        
        self.last_message_count = len(self.agent.current_context)
        
        def run_chat():
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
            while True:
                if stop_event.is_set() or getattr(self.agent, "interrupted", False):
                    break

                try:
                    event = event_queue.get(timeout=STREAM_QUEUE_POLL_SECONDS)
                except Empty:
                    now = time.monotonic()
                    if (
                        STREAM_IDLE_TIMEOUT_SECONDS
                        and now - last_activity >= STREAM_IDLE_TIMEOUT_SECONDS
                    ):
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
                        yield StreamEvent(type='heartbeat')
                    continue

                if event is None:
                    break
                last_activity = time.monotonic()
                yield event
            
            self.last_message_count = len(self.agent.current_context)
            
            yield StreamEvent(type='done')
        except GeneratorExit:
            # 生成器被外部关闭
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
                print(
                    f"Warning: chat thread still alive after {STREAM_WORKER_JOIN_SECONDS}s join, "
                    "releasing active state anyway",
                    file=sys.stderr,
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
