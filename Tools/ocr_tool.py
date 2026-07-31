"""
OCR 工具 - 基于 PaddleOCR 3.x / PP-OCRv6 的离线文字识别
=========================================================
支持单张图片、批量图片、目录扫描的 OCR 识别。

依赖: pip install paddlepaddle==3.2.2 paddleocr
首次运行会自动从 ModelScope 下载模型到本地缓存，之后完全离线。

用法（代码中）:
    from Tools.ocr_tool import OCRToolManager
    ocr = OCRToolManager()
    result = ocr.ocr_image("图片路径")

用法（命令行）:
    python ocr_tool.py ocr_image <图片路径>
    python ocr_tool.py ocr_images '[路径1, 路径2]'
    python ocr_tool.py ocr_directory <目录路径> [--recursive]
    python ocr_tool.py ocr_text <图片路径>
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------
try:
    from paddleocr import PaddleOCR
    PADDLEOCR_AVAILABLE = True
except ImportError:
    PADDLEOCR_AVAILABLE = False


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SUPPORTED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif",
    ".webp",
}
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
DEFAULT_OCR_OUTPUT_DIR = DEFAULT_OUTPUT_DIR / "ocr"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_OCR_PYTHON = PROJECT_ROOT / "venv_ocr" / "Scripts" / "python.exe"
GLOBAL_PYTHON_313 = Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python313" / "python.exe"
_PYTHON_BACKEND_CACHE: Dict[str, Dict[str, Any]] = {}
PADDLEOCR_LANG_MAP = {
    "ch": "ch",                     # 中文简体
    "en": "en",                     # 英文
    "chinese_cht": "chinese_cht",   # 中文繁体
    "japan": "japan",               # 日文
    "korean": "korean",             # 韩文
    "fr": "fr",                     # 法文
    "de": "de",                     # 德文
}

# ── 异步 OCR 默认配置 ──
OCR_STATE_FILE = Path("Memory/ocr_tasks.json")
MAX_OCR_TASK_HISTORY = 50
MAX_OCR_THREADS = 4                 # OCR 是 CPU/GPU 密集型，不宜过多


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def is_image_file(file_path: str) -> bool:
    """判断是否为支持的图片文件"""
    return Path(file_path).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def _safe_output_stem(file_path: str) -> str:
    """生成适合保存 OCR 结果的文件名前缀"""
    stem = Path(file_path).stem or "image"
    for ch in '<>:"/\\|?*':
        stem = stem.replace(ch, "_")
    return stem.strip() or "image"


def _extract_result_text(results: Dict[str, Any]) -> str:
    if isinstance(results.get("result"), dict):
        return str(results["result"].get("text", ""))
    if isinstance(results.get("text"), str):
        return results["text"]
    if isinstance(results.get("results"), list):
        text_parts = []
        for item in results["results"]:
            if isinstance(item, dict):
                text_parts.append(str(item.get("text", "")))
        return "\n\n".join(part for part in text_parts if part)
    return ""


def _save_ocr_auto_output(results: Dict[str, Any], image_path: str) -> Dict[str, Any]:
    """自动将 OCR 结果写入 output，避免大段返回内容被上层拦截后丢失"""
    DEFAULT_OCR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = _safe_output_stem(image_path)
    json_path = DEFAULT_OCR_OUTPUT_DIR / f"{stem}_{timestamp}.json"
    txt_path = DEFAULT_OCR_OUTPUT_DIR / f"{stem}_{timestamp}.txt"
    latest_json_path = DEFAULT_OUTPUT_DIR / "latest_ocr_result.json"
    latest_txt_path = DEFAULT_OUTPUT_DIR / "latest_ocr_result.txt"

    text = _extract_result_text(results)
    json_text = json.dumps(results, ensure_ascii=False, indent=2, default=str)
    json_path.write_text(json_text, encoding="utf-8")
    txt_path.write_text(text, encoding="utf-8")
    latest_json_path.write_text(json_text, encoding="utf-8")
    latest_txt_path.write_text(text, encoding="utf-8")

    return {
        "json": str(json_path),
        "txt": str(txt_path),
        "latest_json": str(latest_json_path),
        "latest_txt": str(latest_txt_path),
    }


def _attach_auto_output(results: Dict[str, Any], image_path: str) -> Dict[str, Any]:
    if not results.get("success"):
        return results
    try:
        results["output_files"] = _save_ocr_auto_output(results, image_path)
    except Exception as e:
        results["output_save_error"] = f"保存 OCR 结果失败: {str(e)}"
    return results


def _compact_cli_output(results: Dict[str, Any]) -> Dict[str, Any]:
    """命令行默认只返回摘要，完整文本请读取 output 中的落盘文件"""
    if not isinstance(results, dict) or not results.get("success"):
        return results
    if "gpu_info" in results or "available_languages" in results:
        return results

    compact = {
        "success": True,
        "message": results.get("message", "OCR 完成，完整结果已保存到 output"),
    }
    for key in (
        "output_files", "output_save_error", "subprocess_python",
        "subprocess_backend", "total", "success_count", "error_count", "errors",
    ):
        if key in results:
            compact[key] = results[key]

    if isinstance(results.get("result"), dict):
        result = results["result"]
        compact["file"] = result.get("file")
        compact["confidence"] = result.get("confidence")
        compact["line_count"] = result.get("line_count")
        text = str(result.get("text", ""))
        compact["text_preview"] = text[:300]
    elif "text" in results:
        text = str(results.get("text", ""))
        compact["confidence"] = results.get("confidence")
        compact["line_count"] = results.get("line_count")
        compact["text_preview"] = text[:300]
    elif isinstance(results.get("results"), list):
        compact["result_count"] = len(results["results"])

    return compact


def _print_cli_result(results: Dict[str, Any]) -> None:
    if "--full" not in sys.argv:
        results = _compact_cli_output(results)
    print(json.dumps(results, ensure_ascii=False, default=str))


def _patch_paddlex_paddle_dependency_check() -> None:
    """兼容 paddlepaddle-gpu/元数据异常导致 PaddleX 误判未安装 paddlepaddle"""
    try:
        import paddle  # noqa: F401
    except Exception:
        return

    try:
        import paddlex.utils.deps as deps
    except Exception:
        return

    original = getattr(deps, "is_dep_available", None)
    if not callable(original) or getattr(original, "_xenon_ocr_patch", False):
        patched = original
    else:
        def patched(dep, /, check_version=False):
            if dep == "paddlepaddle":
                try:
                    import paddle  # noqa: F401
                    return True
                except Exception:
                    return False
            return original(dep, check_version=check_version)

        patched._xenon_ocr_patch = True
        deps.is_dep_available = patched

    # 部分 PaddleX 模块用 `from paddlex.utils.deps import is_dep_available`
    # 复制了函数引用，需要同步替换。
    for module_name in (
        "paddlex.inference.models.engines.paddle",
        "paddlex.utils.env",
        "paddlex.utils.device",
    ):
        try:
            module = __import__(module_name, fromlist=["is_dep_available"])
            if patched:
                module.is_dep_available = patched
        except Exception:
            pass


def _candidate_ocr_pythons() -> List[Path]:
    candidates = [GLOBAL_PYTHON_313, VENV_OCR_PYTHON]
    discovered = shutil.which("python")
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(Path(sys.executable))

    unique = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved).lower()
        if key not in seen and resolved.exists():
            unique.append(resolved)
            seen.add(key)
    unique.sort(key=_python_backend_sort_key, reverse=True)
    return unique


def _probe_python_backend(python_exe: Path) -> Dict[str, Any]:
    try:
        resolved = python_exe.resolve()
    except Exception:
        resolved = python_exe
    cache_key = str(resolved).lower()
    if cache_key in _PYTHON_BACKEND_CACHE:
        return _PYTHON_BACKEND_CACHE[cache_key]

    code = r'''
import json
import sys
out = {
    "executable": sys.executable,
    "python_version": sys.version.split()[0],
    "paddle_import": False,
    "compiled_cuda": False,
    "cuda_device_count": 0,
}
try:
    import paddle
    out["paddle_import"] = True
    out["paddle_version"] = getattr(paddle, "__version__", None)
    out["compiled_cuda"] = bool(getattr(paddle, "is_compiled_with_cuda", lambda: False)())
    if out["compiled_cuda"]:
        try:
            out["cuda_device_count"] = int(paddle.device.cuda.device_count())
            out["cuda_device_name"] = paddle.device.cuda.get_device_name()
        except Exception as e:
            out["cuda_error"] = str(e)
except Exception as e:
    out["error"] = repr(e)
print("__OCR_PROBE__" + json.dumps(out, ensure_ascii=False))
'''
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [str(resolved), "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30,
        )
        info = {
            "executable": str(resolved),
            "returncode": result.returncode,
            "compiled_cuda": False,
            "cuda_device_count": 0,
        }
        for line in reversed((result.stdout or "").splitlines()):
            if line.startswith("__OCR_PROBE__"):
                info.update(json.loads(line.removeprefix("__OCR_PROBE__")))
                break
        else:
            info["error"] = (result.stderr or result.stdout or "probe produced no marker")[-1000:]
    except Exception as e:
        info = {
            "executable": str(resolved),
            "compiled_cuda": False,
            "cuda_device_count": 0,
            "error": str(e),
        }

    _PYTHON_BACKEND_CACHE[cache_key] = info
    return info


def _python_backend_sort_key(python_exe: Path) -> tuple:
    info = _probe_python_backend(python_exe)
    return (
        1 if info.get("compiled_cuda") else 0,
        int(info.get("cuda_device_count") or 0),
        1 if info.get("paddle_import") else 0,
    )


def _has_gpu_capable_subprocess() -> bool:
    current = Path(sys.executable).resolve()
    for python_exe in _candidate_ocr_pythons():
        if python_exe == current:
            continue
        if _probe_python_backend(python_exe).get("compiled_cuda"):
            return True
    return False


def _parse_worker_result(stdout: str) -> Dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("__OCR_RESULT__"):
            return json.loads(line.removeprefix("__OCR_RESULT__"))
    raise json.JSONDecodeError("OCR worker result marker not found", stdout, 0)


# ---------------------------------------------------------------------------
# GPU 加速检测 — 自动识别可用后端，无需手动配置
# ---------------------------------------------------------------------------
def _detect_gpu_backend() -> str:
    """检测当前环境可用的 GPU 加速后端

    检测优先级: NVIDIA CUDA → AMD ROCm → 自定义设备 → CPU

    Returns:
        "nvidia_cuda" : NVIDIA GPU (CUDA) 可用
        "amd_rocm"    : AMD GPU (ROCm) 可用
        "custom_xxx"  : 其他自定义设备（如 DirectML）
        "cpu"         : 无可用的 GPU 加速
    """
    try:
        import paddle
        if hasattr(paddle, 'is_compiled_with_cuda') and paddle.is_compiled_with_cuda():
            return "nvidia_cuda"
        if hasattr(paddle, 'is_compiled_with_rocm') and paddle.is_compiled_with_rocm():
            return "amd_rocm"
        # 检查自定义设备（Intel Arc、昇腾、昆仑芯等）
        if hasattr(paddle, 'device') and hasattr(paddle.device, 'get_all_custom_device_type'):
            custom_devices = paddle.device.get_all_custom_device_type()
            if custom_devices:
                return f"custom_{custom_devices[0]}"
    except ImportError:
        pass
    except Exception:
        pass
    return "cpu"


def get_gpu_info() -> Dict[str, Any]:
    """获取当前环境的 GPU 加速能力详情"""
    backend = _detect_gpu_backend()
    info = {
        "backend": backend,
        "gpu_available": backend != "cpu",
        "python_executable": sys.executable,
        "description": {
            "nvidia_cuda": "NVIDIA GPU（CUDA）加速 ✓",
            "amd_rocm": "AMD GPU（ROCm）加速 ✓",
            "cpu": "CPU 模式（未检测到 GPU 加速）",
        }.get(backend, f"自定义设备加速: {backend}"),
    }
    try:
        info["ocr_python_candidates"] = [
            _probe_python_backend(candidate)
            for candidate in _candidate_ocr_pythons()
        ]
    except Exception as e:
        info["ocr_python_candidates_error"] = str(e)
    if backend == "nvidia_cuda":
        try:
            import paddle
            info["gpu_count"] = paddle.device.cuda.device_count()
            info["current_device"] = paddle.device.cuda.get_device_name()
        except Exception:
            pass
    elif backend == "amd_rocm":
        try:
            import paddle
            info["gpu_count"] = paddle.device.cuda.device_count()  # ROCm 也走 cuda 接口
        except Exception:
            pass
    return info


def _normalize_v3_result(paddle_result: List[dict]) -> Dict[str, Any]:
    """将 PaddleOCR 3.x 的 predict() 结果标准化

    PaddleOCR 3.x 返回格式（每个元素是一个 dict）:
        {
            'input_path': str,
            'page_index': None,
            'dt_polys': [array(4x2), ...],
            'rec_texts': ['文本1', '文本2'],
            'rec_scores': [0.99, 0.98],
            'rec_polys': [array(4x2), ...],
            'rec_boxes': array(N, 4),
            'textline_orientation_angles': [0, ...],
        }

    标准化输出:
        {
            "file": str,
            "text": "识别的纯文本\\n拼接",
            "lines": [{"text": str, "confidence": float, "bbox": [[x1,y1],...]}, ...],
            "confidence": float,
            "line_count": int,
        }
    """
    if not paddle_result or not isinstance(paddle_result, list):
        return {"file": "", "text": "", "lines": [], "confidence": 0.0, "line_count": 0}

    lines = []
    confidences = []

    for page_data in paddle_result:
        if not isinstance(page_data, dict):
            continue
        rec_texts = page_data.get("rec_texts") or []
        rec_scores = page_data.get("rec_scores") or []
        dt_polys = page_data.get("dt_polys") or []

        for i, text in enumerate(rec_texts):
            confidence = rec_scores[i] if i < len(rec_scores) else 0.0
            bbox = (dt_polys[i].tolist()
                    if i < len(dt_polys) and hasattr(dt_polys[i], "tolist")
                    else [])
            lines.append({
                "text": str(text),
                "confidence": round(float(confidence), 4),
                "bbox": bbox,
            })
            confidences.append(float(confidence))

    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    full_text = "\n".join(l["text"] for l in lines)

    return {
        "file": paddle_result[0].get("input_path", "") if paddle_result else "",
        "text": full_text,
        "lines": lines,
        "confidence": round(avg_confidence, 4),
        "line_count": len(lines),
    }


# ---------------------------------------------------------------------------
# OCR 引擎 — 懒惰初始化单例
# ---------------------------------------------------------------------------
class _OCREngine:
    """PaddleOCR 引擎单例，避免重复加载模型"""

    _instance = None
    _ocr = None
    _lang = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_ocr(cls, lang: str = "ch") -> Optional[Any]:
        """获取（或创建）PaddleOCR 实例

        自动检测 GPU 加速能力：
        - NVIDIA CUDA → 自动启用 GPU
        - AMD ROCm    → 自动启用 GPU
        - 无 GPU      → CPU 模式
        """
        if cls._ocr is not None and cls._lang == lang:
            return cls._ocr
        if not PADDLEOCR_AVAILABLE:
            return None
        cls._lang = lang

        # 主动检测可用 GPU 后端，决定是否启用 GPU 加速
        backend = _detect_gpu_backend()
        use_gpu = backend != "cpu"
        if backend in {"nvidia_cuda", "amd_rocm"}:
            device = "gpu"
        elif backend.startswith("custom_"):
            device = backend.removeprefix("custom_")
        else:
            device = "cpu"
        _patch_paddlex_paddle_dependency_check()
        cls._ocr = PaddleOCR(lang=lang, device=device)

        # 日志输出 GPU 状态
        gpu_info = get_gpu_info()
        print(f"[OCR] 加速模式: {gpu_info['description']}")
        if use_gpu:
            try:
                import paddle
                if backend == "nvidia_cuda":
                    gpu_name = paddle.device.cuda.get_device_name()
                    gpu_count = paddle.device.cuda.device_count()
                    print(f"[OCR] 检测到 {gpu_count} 个 NVIDIA GPU: {gpu_name}")
                elif backend == "amd_rocm":
                    gpu_count = paddle.device.cuda.device_count()
                    print(f"[OCR] 检测到 {gpu_count} 个 AMD GPU (ROCm)")
            except Exception:
                pass

        return cls._ocr


def _direct_ocr_image(image_path: str, lang: str = "ch") -> Dict[str, Any]:
    """不做子进程回退的 OCR 入口，供 worker 使用"""
    path = Path(image_path)
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {image_path}"}
    if not is_image_file(image_path):
        return {
            "success": False,
            "error": f"不支持的图片格式: {path.suffix}，"
                     f"支持的格式: {', '.join(sorted(SUPPORTED_IMAGE_EXTENSIONS))}",
        }
    if not PADDLEOCR_AVAILABLE:
        return {
            "success": False,
            "error": "未安装 PaddleOCR，请安装 paddlepaddle 和 paddleocr",
        }

    try:
        backend = _detect_gpu_backend()
        if backend in {"nvidia_cuda", "amd_rocm"}:
            device = "gpu"
        elif backend.startswith("custom_"):
            device = backend.removeprefix("custom_")
        else:
            device = "cpu"

        _patch_paddlex_paddle_dependency_check()
        ocr = PaddleOCR(lang=lang, device=device)
        raw = ocr.predict(str(path))
        if not raw or not isinstance(raw, list) or len(raw) == 0:
            return {
                "success": True,
                "result": {
                    "file": str(path), "text": "", "lines": [],
                    "confidence": 0.0, "line_count": 0,
                },
                "message": "未识别到文字",
            }

        normalized = _normalize_v3_result(raw)
        normalized["file"] = str(path)
        return {
            "success": True,
            "result": normalized,
            "message": f"识别完成，共 {normalized['line_count']} 行文字，"
                       f"平均置信度 {normalized['confidence']:.1%}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"OCR worker 识别失败: {str(e)}",
            "traceback": traceback.format_exc(),
        }


# ---------------------------------------------------------------------------
# OCRHandler — 核心逻辑
# ---------------------------------------------------------------------------
class OCRHandler:
    """OCR 核心处理器"""

    def __init__(self, lang: str = "ch"):
        self.lang = lang
        if not PADDLEOCR_AVAILABLE:
            self._init_error = (
                "未安装 PaddleOCR，请执行:\n"
                "  pip install paddlepaddle==3.2.2\n"
                "  pip install paddleocr"
            )
        else:
            self._init_error = None

    def _ensure_engine(self):
        if self._init_error:
            return None
        return _OCREngine.get_ocr(lang=self.lang)

    def _should_fallback_to_subprocess(self, error: Any) -> bool:
        text = str(error)
        markers = (
            "paddle_static",
            "paddle_dynamic",
            "paddlepaddle",
            "PaddleOCR",
            "paddleocr",
            "No module named 'paddle'",
            "No module named \"paddle\"",
        )
        return any(marker in text for marker in markers)

    def _run_via_subprocess(self, image_path: str, require_gpu: bool = False) -> Dict[str, Any]:
        last_error = ""
        script_path = Path(__file__).resolve()
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        for python_exe in _candidate_ocr_pythons():
            if python_exe == Path(sys.executable).resolve():
                continue
            backend_info = _probe_python_backend(python_exe)
            if require_gpu and not backend_info.get("compiled_cuda"):
                continue
            try:
                result = subprocess.run(
                    [
                        str(python_exe),
                        "-X", "utf8",
                        str(script_path),
                        "__ocr_worker",
                        str(image_path),
                        self.lang,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=180,
                )
                if result.returncode != 0:
                    last_error = (
                        f"{python_exe} 退出码 {result.returncode}: "
                        f"{(result.stderr or result.stdout)[-1000:]}"
                    )
                    continue

                worker_result = _parse_worker_result(result.stdout)
                if worker_result.get("success"):
                    worker_result["subprocess_python"] = str(python_exe)
                    worker_result["subprocess_backend"] = backend_info
                    return _attach_auto_output(worker_result, image_path)
                last_error = f"{python_exe}: {worker_result.get('error', '未知错误')}"
            except subprocess.TimeoutExpired:
                last_error = f"{python_exe}: OCR 子进程超时"
            except Exception as e:
                last_error = f"{python_exe}: {str(e)}"

        return {
            "success": False,
            "error": f"OCR 子进程回退失败: {last_error or '没有可用的 Python 环境'}",
        }

    def ocr_image(self, image_path: str) -> Dict[str, Any]:
        """识别单张图片"""
        path = Path(image_path)
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {image_path}"}
        if not is_image_file(image_path):
            return {
                "success": False,
                "error": f"不支持的图片格式: {path.suffix}，"
                         f"支持的格式: {', '.join(sorted(SUPPORTED_IMAGE_EXTENSIONS))}",
            }

        if _detect_gpu_backend() == "cpu" and _has_gpu_capable_subprocess():
            gpu_result = self._run_via_subprocess(image_path, require_gpu=True)
            if gpu_result.get("success"):
                return gpu_result

        try:
            ocr = self._ensure_engine()
        except Exception as e:
            if self._should_fallback_to_subprocess(e):
                return self._run_via_subprocess(image_path)
            return {
                "success": False,
                "error": f"OCR 引擎初始化失败: {str(e)}",
                "traceback": traceback.format_exc(),
            }
        if not ocr:
            fallback = self._run_via_subprocess(image_path)
            if fallback.get("success"):
                return fallback
            return {"success": False, "error": self._init_error or fallback.get("error")}

        try:
            # PaddleOCR 3.x 推荐使用 predict()
            raw = ocr.predict(str(path))
            if not raw or not isinstance(raw, list) or len(raw) == 0:
                return _attach_auto_output({
                    "success": True,
                    "result": {
                        "file": str(path), "text": "", "lines": [],
                        "confidence": 0.0, "line_count": 0,
                    },
                    "message": "未识别到文字",
                }, str(path))

            normalized = _normalize_v3_result(raw)
            normalized["file"] = str(path)
            return _attach_auto_output({
                "success": True,
                "result": normalized,
                "message": f"识别完成，共 {normalized['line_count']} 行文字，"
                          f"平均置信度 {normalized['confidence']:.1%}",
            }, str(path))
        except Exception as e:
            if self._should_fallback_to_subprocess(e):
                return self._run_via_subprocess(image_path)
            return {
                "success": False,
                "error": f"OCR 识别失败: {str(e)}",
                "traceback": traceback.format_exc(),
            }

    def ocr_images(self, image_paths: List[str]) -> Dict[str, Any]:
        """批量识别多张图片"""
        results = []
        errors = []
        output_files = []
        success_count = 0
        for img_path in image_paths:
            res = self.ocr_image(img_path)
            if res["success"]:
                results.append(res["result"])
                if res.get("output_files"):
                    output_files.append({"file": img_path, **res["output_files"]})
                success_count += 1
            else:
                errors.append({"file": img_path, "error": res.get("error", "未知错误")})

        summary = {
            "success": True,
            "results": results,
            "total": len(image_paths),
            "success_count": success_count,
            "error_count": len(errors),
            "message": f"批量识别完成: {success_count}/{len(image_paths)} 成功",
        }
        if errors:
            summary["errors"] = errors
        if output_files:
            summary["output_files"] = output_files
        return summary

    def ocr_directory(self, dir_path: str, recursive: bool = False,
                      extensions: Optional[List[str]] = None) -> Dict[str, Any]:
        """扫描目录并识别所有图片"""
        path = Path(dir_path)
        if not path.exists() or not path.is_dir():
            return {"success": False, "error": f"目录不存在: {dir_path}"}

        if extensions is None:
            extensions = list(SUPPORTED_IMAGE_EXTENSIONS)

        image_files = []
        for ext in extensions:
            pattern = f"*{ext}"
            if recursive:
                image_files.extend(path.rglob(pattern))
                image_files.extend(path.rglob(pattern.upper()))
            else:
                image_files.extend(path.glob(pattern))
                image_files.extend(path.glob(pattern.upper()))

        image_files = sorted(set(str(f) for f in image_files))
        if not image_files:
            return {
                "success": True, "results": [], "total": 0,
                "success_count": 0, "error_count": 0,
                "message": f"目录 '{dir_path}' 中未找到支持的图片文件",
            }
        return self.ocr_images(image_files)

    def ocr_image_to_text(self, image_path: str) -> Dict[str, Any]:
        """简化接口：仅返回纯文本"""
        result = self.ocr_image(image_path)
        if not result["success"]:
            return {"success": False, "error": result.get("error", "识别失败")}
        payload = {
            "success": True,
            "text": result["result"]["text"],
            "confidence": result["result"]["confidence"],
            "line_count": result["result"]["line_count"],
        }
        if result.get("output_files"):
            payload["output_files"] = result["output_files"]
        for key in ("subprocess_python", "subprocess_backend"):
            if key in result:
                payload[key] = result[key]
        return payload

    def get_language_info(self) -> Dict[str, Any]:
        return {
            "success": True,
            "current_lang": self.lang,
            "available_languages": list(PADDLEOCR_LANG_MAP.keys()),
            "paddleocr_available": PADDLEOCR_AVAILABLE,
        }

    def get_gpu_info(self) -> Dict[str, Any]:
        """获取当前 GPU 加速能力信息"""
        return {
            "success": True,
            "gpu_info": get_gpu_info(),
        }


# ---------------------------------------------------------------------------
# OCRToolManager — 外部接口
# ---------------------------------------------------------------------------
class OCRToolManager:
    """OCR 工具管理器 — 同步 + 异步 OCR。

    同步方法（阻塞）:
        ocr_image / ocr_images / ocr_directory / ocr_image_to_text

    异步方法（线程池 + 轮询池，不阻塞）:
        ocr_image_async / ocr_images_async / ocr_status / ocr_wait

    所有异步任务完成后自动推送结果到 MessagePollingPool。
    """

    def __init__(self, lang: str = "ch"):
        self.handler = OCRHandler(lang=lang)
        # ── 异步状态 ──
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._state_file = OCR_STATE_FILE
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._executor = ThreadPoolExecutor(max_workers=MAX_OCR_THREADS)
        self._futures: Dict[str, Any] = {}
        self._load_state()

    # ═══════════════════════════════════════════════════════════ #
    #  异步 OCR — 线程池 + 轮询池
    # ═══════════════════════════════════════════════════════════ #

    def ocr_image_async(
        self, image_path: str, background: bool = True
    ) -> Dict[str, Any]:
        """异步 OCR 识别单张图片（默认不阻塞）。

        后台模式 (background=True)：OCR 在线程池中执行，立即返回 task_id。
        结果完成后自动推送到 MessagePollingPool。

        Args:
            image_path: 图片文件路径
            background: True → 后台执行，立即返回 task_id
                        False → 同步执行（阻塞等待）

        Returns:
            后台模式: {"success": True, "task_id": "...", "status": "pending"}
            同步模式: 完整 OCR 结果字典

        💡 调用建议：单张图片且不急于获取结果时，优先使用此异步方法。
           大量图片请用 ocr_images_async 批量异步。
        """
        path = Path(image_path)
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {image_path}"}
        if not is_image_file(image_path):
            return {
                "success": False,
                "error": f"不支持的图片格式: {path.suffix}，"
                         f"支持的格式: {', '.join(sorted(SUPPORTED_IMAGE_EXTENSIONS))}",
            }

        task_id = uuid.uuid4().hex[:8]

        if not background:
            result = self.handler.ocr_image(image_path)
            result["task_id"] = task_id
            return result

        # 后台模式：提交到线程池
        self._register_task(task_id, {
            "type": "ocr_single",
            "image_path": str(path),
            "status": "pending",
            "created_at": time.time(),
        })

        future = self._executor.submit(self._ocr_worker, task_id, image_path)
        self._futures[task_id] = future

        return {
            "success": True,
            "task_id": task_id,
            "status": "pending",
            "message": f"OCR 任务已提交: {task_id}",
        }

    def ocr_images_async(
        self, image_paths: List[str], background: bool = True
    ) -> Dict[str, Any]:
        """异步批量 OCR（默认不阻塞，线程池并行处理）。

        每张图片独立提交到线程池，MAX_OCR_THREADS 控制并发度。

        Args:
            image_paths: 图片路径列表
            background: True → 批量并行后台执行
                        False → 同步批量（阻塞）

        Returns:
            后台模式: {"success": True, "task_ids": [...], "total": N, "status": "pending"}
            同步模式: 完整批量结果字典

        💡 调用建议：大量图片（≥3张）请优先使用此异步方法，线程池并行处理不阻塞。
        """
        if not image_paths:
            return {"success": False, "error": "image_paths 不能为空"}

        if not background:
            return self.handler.ocr_images(image_paths)

        task_ids = []
        for img_path in image_paths:
            r = self.ocr_image_async(img_path, background=True)
            if r.get("success"):
                task_ids.append(r["task_id"])
            else:
                task_ids.append(None)

        valid_count = len([t for t in task_ids if t])
        return {
            "success": True,
            "task_ids": task_ids,
            "total": len(image_paths),
            "success_count": valid_count,
            "error_count": len(image_paths) - valid_count,
            "status": "pending",
            "message": f"已提交 {valid_count}/{len(image_paths)} 个 OCR 任务",
        }

    def ocr_status(self, task_id: str) -> Dict[str, Any]:
        """查询 OCR 异步任务状态。

        Args:
            task_id: 任务 ID

        Returns:
            {"success": True, "data": {"task_id": ..., "status": "pending|running|completed|failed", ...}}
        """
        with self._lock:
            task = self._tasks.get(task_id)
        if not task:
            return {"success": False, "error": f"任务不存在: {task_id}"}
        return {"success": True, "data": dict(task)}

    def ocr_wait(
        self, task_id: str, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """等待 OCR 异步任务完成。

        Args:
            task_id: 任务 ID
            timeout: 超时秒数，None 表示无限等待

        Returns:
            任务结果，超时时返回当前状态
        """
        with self._lock:
            task = self._tasks.get(task_id)
        if not task:
            return {"success": False, "error": f"任务不存在: {task_id}"}

        future = self._futures.get(task_id)
        if not future:
            return {"success": True, "data": dict(task)}

        try:
            future.result(timeout=timeout)
        except Exception:
            pass  # 超时或取消，返回当前状态

        with self._lock:
            task = self._tasks.get(task_id, {})
        return {"success": True, "data": dict(task)}

    # ═══════════════════════════════════════════════════════════ #
    #  内部 — OCR worker
    # ═══════════════════════════════════════════════════════════ #

    def _ocr_worker(self, task_id: str, image_path: str) -> None:
        """OCR 工作线程：执行同步 OCR 并推送结果。

        在线程池中运行，完成后自动：
          1. 更新任务状态为 completed/failed
          2. 推送结果到 MessagePollingPool
        """
        try:
            self._update_task(task_id, {"status": "running"})
            result = self.handler.ocr_image(image_path)
            result["task_id"] = task_id
            self._update_task(task_id, {
                "status": "completed",
                "result": result,
                "completed_at": time.time(),
            })
            self._push_to_pool(task_id, result)
        except Exception as e:
            error_result = {
                "success": False,
                "error": f"OCR 任务异常: {str(e)}",
                "task_id": task_id,
                "traceback": traceback.format_exc(),
            }
            self._update_task(task_id, {
                "status": "failed",
                "error": str(e),
                "failed_at": time.time(),
            })
            self._push_to_pool(task_id, error_result)

    # ═══════════════════════════════════════════════════════════ #
    #  内部 — 轮询池推送
    # ═══════════════════════════════════════════════════════════ #

    def _push_to_pool(self, task_id: str, result: Dict[str, Any]) -> None:
        """将 OCR 任务结果推送到消息轮询池。"""
        try:
            from xenon_core.polling_pool import get_pool, PoolMessage

            pool = get_pool()
            is_success = result.get("success", False)
            pool.push(
                PoolMessage(
                    source="ocr_tool",
                    scenario="ocr",
                    msg_type="result",
                    payload={"task_id": task_id, **result},
                    priority=2 if not is_success else 1,
                    ttl=3600,  # 1 小时后过期
                )
            )
        except ImportError:
            pass  # 轮询池未初始化（如在独立脚本中运行）
        except Exception:
            pass  # 推送失败非致命

    # ═══════════════════════════════════════════════════════════ #
    #  内部 — 任务状态管理
    # ═══════════════════════════════════════════════════════════ #

    def _register_task(self, task_id: str, task_data: Dict[str, Any]) -> None:
        """注册新任务并持久化。"""
        with self._lock:
            task_data["task_id"] = task_id
            self._tasks[task_id] = task_data
            self._trim_history()
            self._save_state()

    def _update_task(self, task_id: str, updates: Dict[str, Any]) -> None:
        """更新任务状态并持久化。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.update(updates)
                task["updated_at"] = time.time()
            self._save_state()

    def _trim_history(self) -> None:
        """超出上限时清理最旧的任务。"""
        if len(self._tasks) <= MAX_OCR_TASK_HISTORY:
            return
        sorted_tasks = sorted(
            self._tasks.items(),
            key=lambda kv: kv[1].get("created_at", 0),
            reverse=True,
        )
        keep_ids = {tid for tid, _ in sorted_tasks[:MAX_OCR_TASK_HISTORY]}
        for tid in list(self._tasks.keys()):
            if tid not in keep_ids:
                del self._tasks[tid]

    def _load_state(self) -> None:
        """从磁盘恢复任务状态。"""
        try:
            if self._state_file.exists():
                with open(self._state_file, "r", encoding="utf-8") as f:
                    self._tasks = json.load(f)
        except Exception:
            pass

    def _save_state(self) -> None:
        """持久化任务状态到磁盘。"""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self._state_file, "w", encoding="utf-8") as f:
                    json.dump(self._tasks, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            pass

    def ocr_image(self, image_path: str) -> Dict[str, Any]:
        """识别单张图片，结果自动保存到 output/ocr/（json + txt）
        
        返回结果中 output_files 包含：
          - json / txt: 本次识别结果文件路径
          - latest_json / latest_txt: 最近一次识别结果（覆盖更新）
        
        Args:
            image_path: 图片文件路径（支持 jpg/png/bmp/tiff/webp 等）
        
        Returns:
            Dict with keys: success, result (含 text/confidence/line_count/lines),
            output_files (自动保存的文件路径), message

        💡 调用建议：单张或少量图片用此同步方法即可；
           大量图片（≥3张）请改用 ocr_images_async 异步方法，避免长时间阻塞。
        """
        try:
            return self.handler.ocr_image(image_path)
        except Exception as e:
            return {"success": False, "error": f"OCR 识别失败: {str(e)}"}

    def ocr_images(self, image_paths: List[str]) -> Dict[str, Any]:
        """批量识别多张图片，每张图片结果自动保存到 output/ocr/
        
        Args:
            image_paths: 图片路径列表
        
        Returns:
            Dict with keys: success, results (列表), total, success_count, error_count,
            output_files (各图片的保存路径)

        💡 调用建议：少量图片可用此同步方法；
           大量图片（≥3张）请改用 ocr_images_async 异步方法（线程池并行，不阻塞）。
        """
        try:
            return self.handler.ocr_images(image_paths)
        except Exception as e:
            return {"success": False, "error": f"批量 OCR 失败: {str(e)}"}

    def ocr_directory(self, dir_path: str, recursive: bool = False,
                      extensions: Optional[List[str]] = None) -> Dict[str, Any]:
        """扫描目录并识别所有图片，结果自动保存到 output/ocr/
        
        Args:
            dir_path: 目标目录路径
            recursive: 是否递归子目录（默认 False）
            extensions: 要识别的图片扩展名列表（默认为所有支持的格式）
        
        Returns:
            Dict with keys: success, results (列表), total, success_count, error_count
        """
        try:
            return self.handler.ocr_directory(dir_path, recursive, extensions)
        except Exception as e:
            return {"success": False, "error": f"目录 OCR 失败: {str(e)}"}

    def ocr_image_to_text(self, image_path: str) -> Dict[str, Any]:
        """简化接口：识别图片并返回纯文本（结果自动保存到 output/ocr/）
        
        Args:
            image_path: 图片文件路径
        
        Returns:
            Dict with keys: success, text (纯文本), confidence, line_count,
            output_files (自动保存的文件路径)
        """
        try:
            return self.handler.ocr_image_to_text(image_path)
        except Exception as e:
            return {"success": False, "error": f"文本提取失败: {str(e)}"}

    def get_language_info(self) -> Dict[str, Any]:
        try:
            return self.handler.get_language_info()
        except Exception as e:
            return {"success": False, "error": f"获取语言信息失败: {str(e)}"}

    def get_gpu_info(self) -> Dict[str, Any]:
        """获取当前 GPU 加速能力信息"""
        try:
            return self.handler.get_gpu_info()
        except Exception as e:
            return {"success": False, "error": f"获取 GPU 信息失败: {str(e)}"}

    def list_images(self, base_path: str = ".", recursive: bool = False) -> Dict[str, Any]:
        """列出目录中的图片文件"""
        try:
            from pathlib import Path
            base = Path(base_path)
            if not base.exists():
                return {"success": False, "error": f"路径不存在: {base_path}"}
            extensions = set(ext.lower() for ext in SUPPORTED_IMAGE_EXTENSIONS)
            if recursive:
                files = sorted(str(p) for p in base.rglob("*") if p.suffix.lower() in extensions and p.is_file())
            else:
                files = sorted(str(p) for p in base.glob("*") if p.suffix.lower() in extensions and p.is_file())
            return {"success": True, "files": files, "total_files": len(files), "current_path": str(base),
                    "message": f"找到 {len(files)} 个图片文件"}
        except Exception as e:
            return {"success": False, "error": f"列出图片文件失败: {str(e)}"}

    def save_results(self, results: Dict[str, Any], output_path: str,
                     format: str = "json") -> Dict[str, Any]:
        """保存 OCR 结果到文件"""
        try:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if format == "txt":
                text_parts = []
                if "result" in results and isinstance(results["result"], dict):
                    text_parts.append(results["result"].get("text", ""))
                elif "results" in results:
                    for res in results["results"]:
                        if isinstance(res, dict):
                            text_parts.append(res.get("text", ""))
                output_path.write_text("\n".join(text_parts), encoding="utf-8")
            else:
                output_path.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            return {"success": True, "file": str(output_path),
                    "message": f"结果已保存到 {output_path}"}
        except Exception as e:
            return {"success": False, "error": f"保存结果失败: {str(e)}"}


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------
def create_ocr_tool_manager(lang: str = "ch") -> OCRToolManager:
    """创建 OCR 工具管理器实例"""
    return OCRToolManager(lang=lang)


def _run_worker_main() -> None:
    image_path = sys.argv[2] if len(sys.argv) >= 3 else ""
    lang = sys.argv[3] if len(sys.argv) >= 4 else "ch"
    result = _direct_ocr_image(image_path, lang=lang)
    print("__OCR_RESULT__" + json.dumps(result, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "__ocr_worker":
        _run_worker_main()
        return

    if len(sys.argv) < 3:
        print(json.dumps({
            "success": False, "error": "参数不足",
            "usage": [
                "python ocr_tool.py ocr_image <图片路径>",
                "python ocr_tool.py ocr_images '[路径1, 路径2]'",
                "python ocr_tool.py ocr_directory <目录路径> [--recursive]",
                "python ocr_tool.py ocr_text <图片路径>",
                "python ocr_tool.py get_gpu_info dummy",
                "python ocr_tool.py get_language_info dummy",
                "默认会把完整结果保存到 output；如需打印完整 JSON，可追加 --full",
            ],
        }, ensure_ascii=False))
        sys.exit(1)

    action = sys.argv[1]
    manager = create_ocr_tool_manager()

    if action == "ocr_image" and len(sys.argv) >= 3:
        result = manager.ocr_image(sys.argv[2])
        _print_cli_result(result)
    elif action == "ocr_images" and len(sys.argv) >= 3:
        result = manager.ocr_images(json.loads(sys.argv[2]))
        _print_cli_result(result)
    elif action == "ocr_directory" and len(sys.argv) >= 3:
        recursive = "--recursive" in sys.argv
        result = manager.ocr_directory(sys.argv[2], recursive=recursive)
        _print_cli_result(result)
    elif action == "ocr_text" and len(sys.argv) >= 3:
        result = manager.ocr_image_to_text(sys.argv[2])
        _print_cli_result(result)
    elif action == "list_images" and len(sys.argv) >= 3:
        recursive = "--recursive" in sys.argv
        result = manager.list_images(sys.argv[2], recursive=recursive)
        _print_cli_result(result)
    elif action == "get_gpu_info":
        result = manager.get_gpu_info()
        _print_cli_result(result)
    elif action == "get_language_info":
        result = manager.get_language_info()
        _print_cli_result(result)
    elif action == "save_results" and len(sys.argv) >= 4:
        results = json.loads(sys.argv[2])
        output_path = sys.argv[3]
        fmt = json.loads(sys.argv[4]) if len(sys.argv) >= 5 else "json"
        result = manager.save_results(results, output_path, format=fmt)
        _print_cli_result(result)
    else:
        print(json.dumps({
            "success": False, "error": f"未知操作或参数不足: {action}",
        }, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
