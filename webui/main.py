import sys
import os
import json
import asyncio
import logging
import platform
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Any, Optional
from contextlib import asynccontextmanager
from datetime import datetime

WEBUI_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = WEBUI_DIR.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from openai import OpenAI
import uvicorn

from Xenon import AIAgent, AVAILABLE_MODELS, BASE_URL, API_KEY, MODEL, APP_VERSION, MAX_CONTEXT_TOKENS_DEFAULT
from webui.database import Database
from webui.stream_adapter import create_stream_adapter
from xenon_core.runtime_health import (
    collect_runtime_health,
    format_runtime_health_report,
    format_tool_load_report,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 确保 stderr 使用 UTF-8 编码，避免 Windows 下中文日志乱码
try:
    sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in FALSE_VALUES


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using %s", name, value, default)
        return default


def is_termux_runtime() -> bool:
    prefix = os.getenv("PREFIX", "")
    return bool(os.getenv("TERMUX_VERSION") or "com.termux" in prefix or "/termux" in prefix.lower())


def resolve_webui_prewarm_mode() -> str:
    mode = os.getenv("XENON_WEBUI_PREWARM", "auto").strip().lower()
    if mode in FALSE_VALUES or mode in {"skip", "none"}:
        return "off"
    if mode in {"sync", "blocking"}:
        return "blocking"
    if mode in {"async", "background"}:
        return "background"
    if mode != "auto":
        logger.warning("Unknown XENON_WEBUI_PREWARM=%r, using auto", mode)

    if is_termux_runtime():
        return "off"
    return "background"


def should_start_file_watcher() -> bool:
    mode = os.getenv("XENON_WEBUI_FILE_WATCHER", "auto").strip().lower()
    if mode in TRUE_VALUES:
        return True
    if mode in FALSE_VALUES:
        return False
    if mode != "auto":
        logger.warning("Unknown XENON_WEBUI_FILE_WATCHER=%r, using auto", mode)
    return platform.system().lower() == "windows" and not is_termux_runtime()


def env_list(name: str, default: List[str]) -> List[str]:
    value = os.getenv(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


WEBUI_HOST = os.getenv("XENON_WEBUI_HOST", "127.0.0.1")
WEBUI_PORT = env_int("XENON_WEBUI_PORT", 8000)
ENABLE_TOOL_FILE_WATCHER = should_start_file_watcher()
BACKGROUND_SESSION_THEME = env_flag("XENON_WEBUI_BACKGROUND_THEME", True)
WEBUI_CORS_ORIGINS = env_list(
    "XENON_WEBUI_CORS_ORIGINS",
    [
        f"http://127.0.0.1:{WEBUI_PORT}",
        f"http://localhost:{WEBUI_PORT}",
    ],
)
WEBUI_CORS_ALLOW_CREDENTIALS = (
    env_flag("XENON_WEBUI_CORS_ALLOW_CREDENTIALS", False)
    and "*" not in WEBUI_CORS_ORIGINS
)

# 配置常量
MAX_MESSAGE_LENGTH = 10000  # 最大消息长度
MAX_SESSIONS = 100  # 最大会话数量，达到上限后自动淘汰最旧的会话（滑动窗口）
SESSION_THEME_MODEL = MODEL
SESSION_THEME_MAX_LENGTH = 18
SESSION_THEME_TIMEOUT = 12

# 使用线程安全的容器
class ThreadSafeDict:
    """线程安全的字典包装器"""
    def __init__(self):
        self._dict = {}
        self._lock = threading.RLock()
    
    def get(self, key, default=None):
        with self._lock:
            return self._dict.get(key, default)
    
    def set(self, key, value):
        with self._lock:
            self._dict[key] = value
    
    def delete(self, key):
        with self._lock:
            if key in self._dict:
                del self._dict[key]
    
    def contains(self, key):
        with self._lock:
            return key in self._dict
    
    def get_all_keys(self):
        with self._lock:
            return list(self._dict.keys())

    def get_ref(self, key):
        """返回 key 对应值的原始引用（非副本），修改返回值会影响共享数据。

        如需安全副本请使用 get_copy(key) 或调用方自行 copy.deepcopy。
        """
        with self._lock:
            return self._dict.get(key)

    def get_copy(self, key):
        """返回 key 对应值的深拷贝，修改返回值不影响共享数据。"""
        import copy
        with self._lock:
            value = self._dict.get(key)
            return copy.deepcopy(value) if value is not None else None

    def get_or_setdefault(self, key, factory):
        with self._lock:
            if key not in self._dict:
                self._dict[key] = factory()
            return self._dict[key]

# 全局实例
db = Database()
agent_instances = ThreadSafeDict()
agent_locks = ThreadSafeDict()
active_streams = ThreadSafeDict()
running_streams = ThreadSafeDict()
stream_lifecycle_locks = ThreadSafeDict()


def resolve_model(model: Optional[str] = None) -> str:
    selected = (model or MODEL).strip()
    if selected not in AVAILABLE_MODELS:
        raise HTTPException(status_code=400, detail=f"Unsupported model: {selected}")
    return selected


def apply_agent_model(agent: Any, model: str):
    if hasattr(agent, "set_model"):
        agent.set_model(model)


def get_stream_lifecycle_lock(session_id: str) -> threading.RLock:
    return stream_lifecycle_locks.get_or_setdefault(session_id, threading.RLock)


def has_pending_session_worker(session_id: str, agent: Any = None) -> bool:
    agent = agent if agent is not None else agent_instances.get(session_id)
    return bool(
        agent is not None
        and hasattr(agent, "has_pending_worker")
        and agent.has_pending_worker()
    )


def has_active_session_stream(session_id: str) -> bool:
    return (
        running_streams.contains(session_id)
        or active_streams.contains(session_id)
        or has_pending_session_worker(session_id)
    )


def wait_for_session_worker(session_id: str, timeout: Optional[float] = None, agent: Any = None) -> bool:
    agent = agent if agent is not None else agent_instances.get(session_id)
    if agent is None or not hasattr(agent, "wait_for_worker"):
        return not has_pending_session_worker(session_id, agent)
    try:
        return bool(agent.wait_for_worker(timeout))
    except Exception as error:
        logger.warning("Error waiting for agent worker for %s: %s", session_id, error)
        return False


def interrupt_session_stream(session_id: str, *, discard_agent: bool = False) -> bool:
    active_streams.delete(session_id)
    running_streams.delete(session_id)

    agent = agent_instances.get(session_id)
    if agent:
        try:
            agent.interrupt()
        except Exception as error:
            logger.warning("Error interrupting agent for %s: %s", session_id, error)

    stopped = wait_for_session_worker(session_id, agent=agent)
    if discard_agent:
        if stopped:
            agent_instances.delete(session_id)
            agent_locks.delete(session_id)
        else:
            logger.warning(
                "Keeping agent for %s because its worker is still shutting down",
                session_id,
            )
    return stopped


def reject_if_session_stream_active(session_id: str) -> None:
    if not has_active_session_stream(session_id):
        return
    raise HTTPException(
        status_code=409,
        detail="A response is already running for this session. Stop it before sending another message.",
    )


def _fallback_session_theme(seed_text: Optional[str]) -> str:
    text = " ".join((seed_text or "").split())
    if not text:
        return "新对话"
    return text[:SESSION_THEME_MAX_LENGTH]


def _clean_session_theme(theme: Optional[str], fallback_seed: Optional[str]) -> str:
    text = " ".join((theme or "").split())
    text = text.strip("`'\"“”‘’《》<>[]【】()（）{} ")
    for prefix in ("主题：", "主题:", "对话主题：", "对话主题:", "标题：", "标题:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if not text:
        text = _fallback_session_theme(fallback_seed)
    return text[:SESSION_THEME_MAX_LENGTH]


def generate_session_theme(seed_text: Optional[str]) -> str:
    if not seed_text or not seed_text.strip():
        return "新对话"

    fallback = _fallback_session_theme(seed_text)
    try:
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=SESSION_THEME_TIMEOUT)
        response = client.chat.completions.create(
            model=SESSION_THEME_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 Xenon 潜意识里的对话主题提炼器。"
                        "只输出一个中文短主题，不超过12个汉字。"
                        "不要输出时间、引号、标点、解释或换行。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"请为这段用户输入提炼会话主题：\n{seed_text[:1000]}",
                },
            ],
            temperature=0.2,
            max_tokens=24,
            timeout=SESSION_THEME_TIMEOUT,
        )
        choices = getattr(response, "choices", None)
        if not choices:
            return fallback
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", "") if message is not None else ""
        return _clean_session_theme(content, seed_text)
    except Exception as error:
        logger.warning("会话主题生成失败，使用本地回退: %s", error)
        return fallback


def session_needs_generated_theme(session: Dict[str, Any]) -> bool:
    theme = (session.get("theme") or "").strip()
    title = (session.get("title") or "").strip()
    messages = session.get("full_context") or session.get("context") or []
    if messages:
        return False
    if not theme:
        return True
    return title.startswith("新对话 ") or theme.startswith("新对话 ")


async def ensure_generated_session_theme(session_id: str, session: Dict[str, Any], seed_text: str) -> None:
    if not session_needs_generated_theme(session):
        return
    theme = await asyncio.to_thread(generate_session_theme, seed_text)
    db.update_session_theme(session_id, theme)


def refresh_session_theme(session_id: str, seed_text: str) -> None:
    try:
        theme = generate_session_theme(seed_text)
        db.update_session_theme(session_id, theme)
    except Exception as error:
        logger.warning("Failed to refresh session theme for %s: %s", session_id, error)


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=MAX_MESSAGE_LENGTH, description="用户消息内容")
    model: Optional[str] = Field(None, description="Model name")
    
    @field_validator('message')
    def validate_message(cls, v):
        if not v or not v.strip():
            raise ValueError('消息不能为空')
        return v.strip()


class CreateSessionRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=100, description="会话标题")
    seed_message: Optional[str] = Field(None, max_length=1000, description="用于生成会话主题的首条消息")
    model: Optional[str] = Field(None, description="Model name")


class ModelRequest(BaseModel):
    model: str = Field(..., description="Model name")


def get_or_create_agent(session_id: str) -> Any:
    """获取或创建 agent 实例（线程安全）"""
    session = db.get_session(session_id) or {}
    model = resolve_model(session.get("model"))

    # 先检查是否存在
    agent = agent_instances.get(session_id)
    if agent is not None:
        apply_agent_model(agent, model)
        return agent
    
    # 获取或创建该 session 的锁（原子操作，消除 TOCTOU 竞态）
    lock = agent_locks.get_or_setdefault(session_id, threading.RLock)
    
    # 双重检查锁定
    with lock:
        agent = agent_instances.get(session_id)
        if agent is None:
            agent = AIAgent()
            if ENABLE_TOOL_FILE_WATCHER:
                agent.tool_manager.start_file_watcher()
            wrapped_agent = create_stream_adapter(agent)
            apply_agent_model(wrapped_agent, model)
            
            context = db.get_context(session_id)
            full_context = db.get_full_context(session_id)
            if context:
                wrapped_agent.set_context(context)
            if full_context:
                wrapped_agent.set_full_context(full_context)
            
            agent_instances.set(session_id, wrapped_agent)
            return wrapped_agent
        apply_agent_model(agent, model)
        return agent


async def get_or_create_agent_async(session_id: str) -> Any:
    return await asyncio.to_thread(get_or_create_agent, session_id)


async def event_generator(session_id: str, user_input: str, stream_id: Optional[str] = None):
    if stream_id is None:
        stream_id = f"{session_id}_{uuid.uuid4().hex}"
        with get_stream_lifecycle_lock(session_id):
            active_streams.set(session_id, stream_id)
            running_streams.set(session_id, stream_id)
    
    agent = None
    try:
        if active_streams.get(session_id) != stream_id:
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        agent = await get_or_create_agent_async(session_id)

        if active_streams.get(session_id) != stream_id:
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        async for event in agent.stream_chat_async(user_input):
            if active_streams.get(session_id) != stream_id:
                logger.info(f"Stream {stream_id} cancelled")
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                break
            
            if event.type == 'user':
                yield f"data: {json.dumps({'type': 'user', 'content': event.content}, ensure_ascii=False)}\n\n"
            
            elif event.type == 'thinking':
                yield f"data: {json.dumps({'type': 'thinking', 'content': event.content}, ensure_ascii=False)}\n\n"
            
            elif event.type == 'content':
                yield f"data: {json.dumps({'type': 'content', 'content': event.content}, ensure_ascii=False)}\n\n"

            elif event.type == 'final_content':
                yield f"data: {json.dumps({'type': 'final_content', 'content': event.content}, ensure_ascii=False)}\n\n"

            elif event.type == 'user_queued':
                yield f"data: {json.dumps({'type': 'user_queued', 'content': event.content, 'queue_position': event.queue_position}, ensure_ascii=False)}\n\n"

            elif event.type == 'queue_processing':
                yield f"data: {json.dumps({'type': 'queue_processing', 'content': event.content, 'queue_remaining': event.queue_remaining}, ensure_ascii=False)}\n\n"

            elif event.type == 'tool_call':
                yield f"data: {json.dumps({'type': 'tool_call', 'tool_name': event.tool_name, 'arguments': event.arguments, 'tool_call_id': event.tool_call_id}, ensure_ascii=False)}\n\n"

            elif event.type == 'tool_progress':
                yield f"data: {json.dumps({'type': 'tool_progress', 'content': event.content, 'tool_name': event.tool_name, 'tool_call_id': event.tool_call_id}, ensure_ascii=False)}\n\n"
            
            elif event.type == 'tool_result':
                yield f"data: {json.dumps({'type': 'tool_result', 'content': event.content}, ensure_ascii=False)}\n\n"

            elif event.type == 'heartbeat':
                yield ": keep-alive\n\n"
            
            elif event.type == 'error':
                yield f"data: {json.dumps({'type': 'error', 'content': event.content}, ensure_ascii=False)}\n\n"
            
            elif event.type == 'done':
                context = agent.get_context()
                full_context = agent.get_full_context()
                db.save_session_state(session_id, context, full_context)
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                break

            else:
                logger.warning(
                    "Unknown stream event type=%r from session=%s — silently ignored, check stream_adapter",
                    event.type, session_id,
                )
    
    except Exception as e:
        logger.error(f"Stream error for session {session_id}: {e}", exc_info=True)
        if active_streams.get(session_id) != stream_id:
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return
        try:
            if agent is not None:
                context = agent.get_context()
                full_context = agent.get_full_context()
                db.save_session_state(session_id, context, full_context)
        except Exception as save_error:
            logger.warning(f"Failed to save context after stream error for session {session_id}: {save_error}")
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
    finally:
        # 清理资源
        if active_streams.get(session_id) == stream_id:
            active_streams.delete(session_id)
        if running_streams.get(session_id) == stream_id:
            running_streams.delete(session_id)


async def prewarm_agent_once() -> None:
    logger.info("Pre-warming agent (loading tools and initializing tiktoken)...")
    try:
        warmup_agent = await asyncio.to_thread(AIAgent)
        logger.info("\n%s", format_tool_load_report(warmup_agent.tool_manager.get_load_report()))
        warmup_wrapper = create_stream_adapter(warmup_agent)
        del warmup_agent
        del warmup_wrapper
        logger.info("Agent pre-warming completed successfully")
    except Exception as e:
        logger.error(f"Agent pre-warming failed: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Xenon Web UI...")
    logger.info("\n%s", format_runtime_health_report(collect_runtime_health(project_root=PROJECT_ROOT)))
    
    prewarm_mode = resolve_webui_prewarm_mode()
    prewarm_task = None
    logger.info(
        "Web UI runtime mode: prewarm=%s, file_watcher=%s, termux=%s",
        prewarm_mode,
        "on" if ENABLE_TOOL_FILE_WATCHER else "off",
        is_termux_runtime(),
    )
    if prewarm_mode == "blocking":
        await prewarm_agent_once()
    elif prewarm_mode == "background":
        prewarm_task = asyncio.create_task(prewarm_agent_once())
    else:
        logger.info("Agent pre-warming skipped")

    try:
        yield
    finally:
        if prewarm_task and not prewarm_task.done():
            prewarm_task.cancel()
    
    # 关闭时清理所有资源
    logger.info("Shutting down Xenon Web UI...")
    for session_id in agent_instances.get_all_keys():
        try:
            agent = agent_instances.get(session_id)
            if agent and hasattr(agent, 'interrupt'):
                agent.interrupt()
        except Exception as e:
            logger.error(f"Error interrupting agent {session_id}: {e}")
    
    # 清理所有实例
    for session_id in agent_instances.get_all_keys():
        agent_instances.delete(session_id)
    for session_id in agent_locks.get_all_keys():
        agent_locks.delete(session_id)
    for session_id in active_streams.get_all_keys():
        active_streams.delete(session_id)
    for session_id in running_streams.get_all_keys():
        running_streams.delete(session_id)
    for session_id in stream_lifecycle_locks.get_all_keys():
        stream_lifecycle_locks.delete(session_id)
    
    logger.info("Cleanup completed")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=WEBUI_CORS_ORIGINS,
    allow_credentials=WEBUI_CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/assets", StaticFiles(directory=WEBUI_DIR), name="webui-assets")


@app.get("/")
async def root():
    index_path = WEBUI_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding='utf-8'))
    return {"message": "Xenon Web UI API"}


# 输入验证辅助函数
def validate_session_id(session_id: str) -> bool:
    """验证 session_id 格式"""
    try:
        # UUID 格式验证
        uuid.UUID(session_id)
        return True
    except ValueError:
        return False


def require_session_id(session_id: str) -> str:
    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")
    return session_id


@app.get("/sessions")
async def get_sessions():
    try:
        sessions = db.get_sessions()
        for session in sessions:
            session["model"] = session.get("model") or MODEL
        # 限制返回数量，防止过大响应
        return {"sessions": sessions[:MAX_SESSIONS]}
    except Exception as e:
        logger.error(f"Error getting sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models")
async def get_models():
    return {
        "models": AVAILABLE_MODELS,
        "default_model": MODEL,
        "app_version": APP_VERSION,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "host": WEBUI_HOST,
        "port": WEBUI_PORT,
        "prewarm": resolve_webui_prewarm_mode(),
        "file_watcher": ENABLE_TOOL_FILE_WATCHER,
        "termux": is_termux_runtime(),
        "cors_origins": WEBUI_CORS_ORIGINS,
        "cors_credentials": WEBUI_CORS_ALLOW_CREDENTIALS,
    }


@app.get("/balance")
async def get_balance():
    """查询 DeepSeek 账户余额"""
    try:
        import aiohttp
        headers = {"Authorization": f"Bearer {API_KEY}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/user/balance", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise HTTPException(status_code=resp.status, detail=f"DeepSeek API error: {text}")
                data = await resp.json()
                return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting balance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sessions")
async def create_session(request: CreateSessionRequest, background_tasks: BackgroundTasks):
    try:
        # 达到上限时自动淘汰最旧的会话（滑动窗口）
        sessions = db.get_sessions()
        if len(sessions) >= MAX_SESSIONS:
            evicted = db.evict_oldest_sessions(keep=MAX_SESSIONS - 1)
            for evicted_id in evicted:
                with get_stream_lifecycle_lock(evicted_id):
                    interrupt_session_stream(evicted_id, discard_agent=True)
                agent_instances.delete(evicted_id)
                agent_locks.delete(evicted_id)
                active_streams.delete(evicted_id)
                running_streams.delete(evicted_id)
                stream_lifecycle_locks.delete(evicted_id)
                logger.info("会话数已达上限，自动淘汰最旧会话: %s", evicted_id)
        
        session_model = resolve_model(request.model)
        seed_message = request.seed_message or request.title
        if seed_message:
            theme = _fallback_session_theme(seed_message) if BACKGROUND_SESSION_THEME else await asyncio.to_thread(generate_session_theme, seed_message)
            title = theme
        else:
            theme = request.title
            title = request.title
        session_id = db.create_session(title, session_model, theme)
        if seed_message and BACKGROUND_SESSION_THEME:
            background_tasks.add_task(refresh_session_theme, session_id, seed_message)
        logger.info(f"Created new session: {session_id}")
        return {"session_id": session_id, "model": session_model, "theme": theme}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    try:
        require_session_id(session_id)
        # 先停止该会话的所有流
        with get_stream_lifecycle_lock(session_id):
            interrupt_session_stream(session_id, discard_agent=True)
        # 删除数据库中的会话
        success = db.delete_session(session_id)
        if not success:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # 清理内存中的实例
        agent_instances.delete(session_id)
        agent_locks.delete(session_id)
        active_streams.delete(session_id)
        running_streams.delete(session_id)
        stream_lifecycle_locks.delete(session_id)
        
        return {"message": "Session deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    try:
        # 验证 session_id 格式
        require_session_id(session_id)
        
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session["model"] = session.get("model") or MODEL
        return session
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/sessions/{session_id}/model")
async def update_session_model(session_id: str, request: ModelRequest):
    try:
        require_session_id(session_id)

        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        with get_stream_lifecycle_lock(session_id):
            if has_active_session_stream(session_id):
                raise HTTPException(status_code=409, detail="Cannot switch model while a response is running")

            model = resolve_model(request.model)
            db.update_session_model(session_id, model)

            agent = agent_instances.get(session_id)
            if agent:
                apply_agent_model(agent, model)

        return {"model": model}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating model for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}/usage")
async def get_session_usage(session_id: str):
    """返回当前会话的上下文 token 用量信息。"""
    try:
        require_session_id(session_id)
        agent = agent_instances.get(session_id)
        result = {
            "context_tokens": 0,
            "context_limit": MAX_CONTEXT_TOKENS_DEFAULT,
            "percent": 0.0,
        }
        if agent is None or not hasattr(agent, "context_manager"):
            return result

        cm = agent.context_manager
        tc = getattr(cm, "token_counter", None)
        if tc is None:
            return result

        try:
            # 优先使用运行时已经算好的 token 信息（与状态栏同步）
            inner = getattr(agent, "agent", None) or agent
            live_status = getattr(inner, "_last_live_context_status", None)
            if (
                getattr(inner, "_turn_running", False)
                and isinstance(live_status, dict)
                and live_status.get("success")
            ):
                live_limit = int(
                    live_status.get("configured_max_tokens")
                    or live_status.get("max_tokens")
                    or getattr(cm, "max_context_tokens", MAX_CONTEXT_TOKENS_DEFAULT)
                    or MAX_CONTEXT_TOKENS_DEFAULT
                )
                live_tokens = int(live_status.get("tokens", 0) or 0)
                live_ratio = getattr(cm, "cleanup_thresholds", {}).get("trigger", 0.8)
                return {
                    "context_tokens": live_tokens,
                    "context_limit": live_limit,
                    "percent": round(float(live_status.get("percentage", 0) or 0), 1),
                    "level": live_status.get("level") or tc.get_token_usage_warning_level(live_tokens, live_limit),
                    "msg_count": int(live_status.get("message_count", 0) or 0),
                    "tool_count": int(live_status.get("tool_count", 0) or 0),
                    "system_tokens": int(live_status.get("system_tokens", 0) or 0),
                    "message_tokens": int(live_status.get("message_tokens", 0) or 0),
                    "tool_tokens": int(live_status.get("tool_tokens", 0) or 0),
                    "cleanup_trigger_tokens": int(live_status.get("cleanup_trigger_tokens", int(live_limit * live_ratio)) or 0),
                    "cleanup_trigger_ratio": live_status.get("cleanup_trigger_ratio", live_ratio),
                    "last_request_estimated_tokens": getattr(inner, "_last_request_estimated_tokens", None),
                    "last_api_total_tokens": getattr(inner, "_last_api_total_tokens", None),
                    "source": "live_request",
                }

            messages = getattr(inner, "current_context", getattr(agent, "current_context", []))
            tools = []
            system_msg = ""
            token_info_str = ""
            try:
                # AsyncAIAgentWrapper 没有 _get_context_token_info，直接调用底层 agent
                inner = getattr(agent, "agent", None) or agent
                get_info = getattr(inner, "_get_context_token_info", None)
                if get_info:
                    token_info_str = get_info()
            except Exception:
                token_info_str = ""

            # 始终先获取 messages（后续两个分支都需要）
            messages = getattr(inner, "current_context", getattr(agent, "current_context", []))
            tools = []
            system_msg = ""

            # 从 token_info_str 中解析出 token 数值
            tokens = 0
            import re
            match = re.search(r"Token使用量:\s*([\d,]+)", token_info_str)
            if match:
                tokens = int(match.group(1).replace(",", ""))
            else:
                # 回退到手动计算（与运行时的 format_context_token_info 完全一致）
                try:
                    system_msg = inner._get_available_tools_message() or ""
                except Exception:
                    pass
                try:
                    tools = inner._get_current_tools() or []
                except Exception:
                    pass

                tokens = 0
                if system_msg:
                    tokens += tc.count_tokens(system_msg)
                tokens += tc.estimate_messages_tokens(messages)
                if tools:
                    tools_json = json.dumps(tools, ensure_ascii=False)
                    tokens += tc.count_tokens(tools_json)
                # 加上认知网络摘要（API 提交时会注入）
                try:
                    cognitive = getattr(inner, "cognitive_network_summary", "")
                    if cognitive:
                        tokens += tc.count_tokens(str(cognitive))
                except Exception:
                    pass

            limit = int(getattr(cm, "max_context_tokens", MAX_CONTEXT_TOKENS_DEFAULT) or MAX_CONTEXT_TOKENS_DEFAULT)
            if limit <= 0:
                limit = MAX_CONTEXT_TOKENS_DEFAULT

            percent = round(tc.get_token_usage_percentage(tokens, limit), 1)

            result = {
                "context_tokens": tokens,
                "context_limit": limit,
                "percent": percent,
                "level": tc.get_token_usage_warning_level(tokens, limit),
                "msg_count": len(messages),
                "cleanup_trigger_tokens": int(limit * getattr(cm, "cleanup_thresholds", {}).get("trigger", 0.8)),
                "cleanup_trigger_ratio": getattr(cm, "cleanup_thresholds", {}).get("trigger", 0.8),
                "last_request_estimated_tokens": getattr(inner, "_last_request_estimated_tokens", None),
                "last_api_total_tokens": getattr(inner, "_last_api_total_tokens", None),
                "debug_system": bool(system_msg),
                "debug_tools": len(tools),
            }
        except Exception as e:
            logger.error(f"Usage calc error: {e}", exc_info=True)
            result["error"] = str(e)

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting usage for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    try:
        require_session_id(session_id)
        context = db.get_full_context(session_id)
        messages = []
        
        for msg in context:
            role = msg.get('role', '')
            
            if role == 'user':
                messages.append({
                    'role': 'user',
                    'type': 'content',
                    'content': msg.get('content', '')
                })
            
            elif role == 'assistant':
                reasoning = msg.get('reasoning_content', '')
                content = msg.get('content', '')
                tool_calls = msg.get('tool_calls', [])
                
                if reasoning:
                    messages.append({
                        'role': 'assistant',
                        'type': 'thinking',
                        'content': reasoning
                    })
                
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = tc.get('function', {}).get('name', '')
                        args = tc.get('function', {}).get('arguments', '')
                        messages.append({
                            'role': 'assistant',
                            'type': 'tool_call',
                            'content': f"{name}({args})"
                        })
                
                if content:
                    messages.append({
                        'role': 'assistant',
                        'type': 'content',
                        'content': content
                    })
            
            elif role == 'tool':
                messages.append({
                    'role': 'tool',
                    'type': 'tool_result',
                    'content': msg.get('content', '')
                })
        
        return {"messages": messages}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/{session_id}")
async def chat(session_id: str, request: ChatRequest, background_tasks: BackgroundTasks):
    try:
        require_session_id(session_id)
        # 验证 session 是否存在
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        requested_model = resolve_model(request.model) if request.model else None

        if session_needs_generated_theme(session):
            if BACKGROUND_SESSION_THEME:
                background_tasks.add_task(refresh_session_theme, session_id, request.message)
            else:
                await ensure_generated_session_theme(session_id, session, request.message)

        stream_id = f"{session_id}_{uuid.uuid4().hex}"
        with get_stream_lifecycle_lock(session_id):
            if requested_model and session.get("model") != requested_model:
                db.update_session_model(session_id, requested_model)
                session["model"] = requested_model

            reject_if_session_stream_active(session_id)

            active_streams.set(session_id, stream_id)
            running_streams.set(session_id, stream_id)
        
        return StreamingResponse(
            event_generator(session_id, request.message, stream_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in chat for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/{session_id}/queue")
async def queue_chat_message(session_id: str, request: ChatRequest):
    """在活跃流期间将消息排入 agent 内部队列，当前轮次完成后自动处理。"""
    try:
        require_session_id(session_id)
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if not has_active_session_stream(session_id):
            raise HTTPException(
                status_code=409,
                detail="No active stream for this session. Use POST /chat/{session_id} instead.",
            )

        agent = agent_instances.get(session_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found for this session")

        if hasattr(agent, 'queue_message'):
            result = agent.queue_message(request.message)
        else:
            raise HTTPException(status_code=500, detail="Agent does not support message queueing")

        return {
            "status": "queued",
            "queue_position": result.get("pending_count", 0),
            "dropped": result.get("dropped_count", 0) > 0,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error queueing message for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/chat/{session_id}/status")
async def get_chat_status(session_id: str):
    try:
        require_session_id(session_id)
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return {
            "active": running_streams.contains(session_id),
            "stream_id": running_streams.get(session_id)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting chat status for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/{session_id}/stop")
async def stop_chat(session_id: str):
    try:
        require_session_id(session_id)
        with get_stream_lifecycle_lock(session_id):
            interrupt_session_stream(session_id, discard_agent=True)
        
        return {"message": "Chat stopped"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping chat for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}/context")
async def get_session_context(session_id: str):
    try:
        require_session_id(session_id)
        agent = agent_instances.get(session_id)
        if agent:
            context = agent.get_context()
            return {"context": context}
        return {"context": []}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting context for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def main():
    uvicorn.run(
        app,
        host=WEBUI_HOST,
        port=WEBUI_PORT,
        reload=False,
        log_level="info"
    )


if __name__ == "__main__":
    main()
