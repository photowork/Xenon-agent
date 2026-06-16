# [file name]: ds_balance.py
"""
DeepSeek 余额查询工具 — 调用官方 API 获取账户余额信息。

依赖: deepseekconfig (项目根目录)
API:  GET https://api.deepseek.com/user/balance
文档: https://api-docs.deepseek.com/zh-cn/api/get-user-balance
"""
import json
import sys
import urllib.request
import urllib.error
from typing import Dict, Any, Optional


# -------------------------------------------------------------------------- #
#  将项目根目录加入 path，以便 import deepseekconfig
# -------------------------------------------------------------------------- #
_PROJECT_ROOT = None
for _p in [__file__, sys.argv[0] if sys.argv else None]:
    if _p:
        _root_candidate = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(_p)))
        if __import__("os").path.isfile(__import__("os").path.join(_root_candidate, "deepseekconfig.py")):
            _PROJECT_ROOT = _root_candidate
            break
if _PROJECT_ROOT and _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    import deepseekconfig as dsc
except ImportError:
    dsc = None  # type: ignore


class DsBalanceToolManager:
    """DeepSeek 账户余额查询工具管理器

    依赖: deepseekconfig.py（项目根目录）提供 API Key 和 Base URL。
    使用方式:
        load_module("ds_balance")
        result = ds_balance_DsBalanceToolManager_check_balance()
    """

    # ---------------------------------------------------------------------- #
    #  内部工具
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _get_api_key() -> str:
        """从 deepseekconfig 获取 API Key"""
        if dsc is None:
            raise RuntimeError("无法导入 deepseekconfig.py，请确认项目结构正确。")
        if not dsc.API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY 为空或未设置环境变量。")
        return dsc.API_KEY

    @staticmethod
    def _get_base_url() -> str:
        """获取 API Base URL（默认 https://api.deepseek.com）"""
        return (dsc.BASE_URL if dsc else "https://api.deepseek.com").rstrip("/")

    # ---------------------------------------------------------------------- #
    #  对外接口
    # ---------------------------------------------------------------------- #

    def check_balance(self, raw: bool = False) -> Dict[str, Any]:
        """查询 DeepSeek 账户余额

        Parameters
        ----------
        raw : bool, optional
            为 True 时返回原始 JSON 响应（含所有字段）；
            为 False 时返回精简摘要（默认）。

        Returns
        -------
        Dict[str, Any]
            包含余额信息的字典。格式示例：
            {
                "success": True,
                "is_available": True,
                "total_balance": "24.63",
                "granted_balance": "0.00",
                "topped_up_balance": "24.63",
                "currency": "CNY",
                "source": "api"
            }
            或出错时：
            {
                "success": False,
                "error": "错误描述",
                "source": "api"
            }
        """
        api_key = self._get_api_key()
        base_url = self._get_base_url()
        url = f"{base_url}/user/balance"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            return {
                "success": False,
                "error": f"HTTP {e.code}: {error_body}",
                "source": "api",
            }
        except urllib.error.URLError as e:
            return {
                "success": False,
                "error": f"网络请求失败: {e.reason}",
                "source": "api",
            }
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "API 返回了非 JSON 格式的响应",
                "source": "api",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"未知错误: {e}",
                "source": "api",
            }

        # 原始响应格式参考:
        # {
        #   "is_available": True,
        #   "balance_infos": [
        #     {"currency": "CNY", "total_balance": "24.63",
        #      "granted_balance": "0.00", "topped_up_balance": "24.63"}
        #   ]
        # }
        if raw:
            return {
                "success": True,
                **data,
                "source": "api",
            }

        # 从 balance_infos 数组中提取第一条（通常只有一条）
        info = data.get("balance_infos", [{}])[0] if data.get("balance_infos") else {}
        return {
            "success": True,
            "is_available": data.get("is_available", None),
            "total_balance": info.get("total_balance"),
            "granted_balance": info.get("granted_balance"),
            "topped_up_balance": info.get("topped_up_balance"),
            "currency": info.get("currency", "CNY"),
            "source": "api",
        }
