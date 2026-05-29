from __future__ import annotations

import signal
import time
from typing import Any, Callable, Type

INTERRUPTED_MESSAGE = "用户中断"


def handle_interrupt(
    agent: Any,
    *,
    interrupted_exception_cls: Type[BaseException],
    print_fn: Callable[..., Any] = print,
) -> None:
    agent.interrupted = True
    print_fn("\n\033[93m[收到中断信号，正在停止...]\033[0m")

    if getattr(agent, "_in_api_call", False):
        raise interrupted_exception_cls(INTERRUPTED_MESSAGE)


def setup_signal_handler(
    agent: Any,
    *,
    signal_handler: Callable[[int, Any], None],
    signal_module: Any = signal,
) -> None:
    agent._original_sigint_handler = signal_module.signal(signal_module.SIGINT, signal_handler)


def restore_signal_handler(
    agent: Any,
    *,
    signal_module: Any = signal,
) -> None:
    original_handler = getattr(agent, "_original_sigint_handler", None)
    if original_handler is not None:
        signal_module.signal(signal_module.SIGINT, original_handler)


def interruptible_sleep(
    *,
    is_interrupted: Callable[[], bool],
    seconds: float,
    interrupted_exception_cls: Type[BaseException],
    sleep_fn: Callable[[float], None] = time.sleep,
    interval: float = 0.1,
) -> None:
    steps = int(seconds / interval)
    for _ in range(steps):
        if is_interrupted():
            raise interrupted_exception_cls(INTERRUPTED_MESSAGE)
        sleep_fn(interval)

    remaining = seconds - (steps * interval)
    if remaining > 0:
        if is_interrupted():
            raise interrupted_exception_cls(INTERRUPTED_MESSAGE)
        sleep_fn(remaining)


def retry_request(
    func: Callable[..., Any],
    *args: Any,
    max_attempts: int,
    retry_delay: float,
    interrupted_exception_cls: Type[BaseException],
    logger: Any,
    is_interrupted: Callable[[], bool],
    sleep_fn: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    for attempt in range(max_attempts):
        if is_interrupted():
            raise interrupted_exception_cls(INTERRUPTED_MESSAGE)
        try:
            return func(*args, **kwargs)
        except interrupted_exception_cls:
            raise
        except Exception as error:
            if attempt == max_attempts - 1:
                raise
            logger.warning("请求失败，第%s次重试: %s", attempt + 1, error)
            interruptible_sleep(
                is_interrupted=is_interrupted,
                seconds=retry_delay,
                interrupted_exception_cls=interrupted_exception_cls,
                sleep_fn=sleep_fn,
            )
