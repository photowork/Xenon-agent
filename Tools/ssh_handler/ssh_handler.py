#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SSH 连接工具
支持保存和管理 SSH 连接信息，执行远程命令
支持通用远程文件代理，让本地工具通过SSH操作远程文件
"""

import base64
import json
import os
import logging
import time
import hashlib
import sys
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List, Union, Callable
import tempfile
import shutil

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    logging.warning("paramiko 库不可用，SSH 功能将受限。请尝试安装: pip install paramiko")


class SSHConnectionConfig:
    def __init__(self):
        """
        SSH 连接配置管理器初始化
        """
        self.connections = {}
        script_dir = Path(__file__).parent
        self.config_file = script_dir / "ssh_connections.json"
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_connections()

    def _protect_password(self, password: str) -> Dict[str, str]:
        """
        保护密码。Windows 优先使用 DPAPI，其它环境回退到兼容编码。
        """
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import byref
                from ctypes import wintypes

                class DATA_BLOB(ctypes.Structure):
                    _fields_ = [
                        ("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))
                    ]

                def _make_blob(data: bytes):
                    buffer = ctypes.create_string_buffer(data)
                    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
                    return blob, buffer

                crypt32 = ctypes.windll.crypt32
                kernel32 = ctypes.windll.kernel32
                CRYPTPROTECT_UI_FORBIDDEN = 0x01

                in_blob, in_buffer = _make_blob(password.encode("utf-8"))
                out_blob = DATA_BLOB()
                success = crypt32.CryptProtectData(
                    byref(in_blob),
                    "Xenon SSH Password",
                    None,
                    None,
                    None,
                    CRYPTPROTECT_UI_FORBIDDEN,
                    byref(out_blob)
                )
                if not success:
                    raise OSError(ctypes.GetLastError())

                try:
                    encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                    return {
                        "password_encrypted": base64.b64encode(encrypted).decode("ascii"),
                        "password_mode": "dpapi"
                    }
                finally:
                    if out_blob.pbData:
                        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
            except Exception as e:
                logging.warning(f"DPAPI 加密失败，回退到兼容编码存储: {str(e)}")

        return {
            "password_encrypted": base64.b64encode(password.encode("utf-8")).decode("ascii"),
            "password_mode": "base64"
        }

    def _unprotect_password(self, info: Dict[str, Any]) -> str:
        """
        读取密码，兼容旧版明文配置。
        """
        if "password" in info:
            return info["password"]

        encrypted = info.get("password_encrypted")
        if not encrypted:
            raise ValueError("连接配置中缺少密码")

        encrypted_bytes = base64.b64decode(encrypted)
        mode = info.get("password_mode", "base64")

        if mode == "dpapi":
            import ctypes
            from ctypes import byref
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))
                ]

            def _make_blob(data: bytes):
                buffer = ctypes.create_string_buffer(data)
                blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
                return blob, buffer

            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            CRYPTPROTECT_UI_FORBIDDEN = 0x01

            in_blob, in_buffer = _make_blob(encrypted_bytes)
            out_blob = DATA_BLOB()
            success = crypt32.CryptUnprotectData(
                byref(in_blob),
                None,
                None,
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                byref(out_blob)
            )
            if not success:
                raise OSError(ctypes.GetLastError())

            try:
                decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                return decrypted.decode("utf-8")
            finally:
                if out_blob.pbData:
                    kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))

        if mode == "base64":
            return encrypted_bytes.decode("utf-8")

        raise ValueError(f"不支持的密码存储模式: {mode}")

    def _normalize_connection_record(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """
        兼容旧配置，并将明文密码迁移到受保护字段。
        """
        normalized = {
            "host": info.get("host", ""),
            "username": info.get("username", ""),
            "port": int(info.get("port", 22))
        }

        if info.get("password_encrypted"):
            normalized["password_encrypted"] = info["password_encrypted"]
            normalized["password_mode"] = info.get("password_mode", "base64")
            return normalized

        if info.get("password"):
            normalized.update(self._protect_password(info["password"]))
            return normalized

        return normalized
    
    def _load_connections(self):
        """
        从配置文件加载连接信息
        """
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    raw_connections = json.load(f)
                    normalized_connections = {}
                    needs_save = False
                    for name, info in raw_connections.items():
                        normalized = self._normalize_connection_record(info)
                        normalized_connections[name] = normalized
                        if normalized != info:
                            needs_save = True

                    self.connections = normalized_connections
                    if needs_save:
                        self._save_connections()
            except Exception as e:
                logging.warning(f"加载 SSH 连接配置失败: {str(e)}")
                self.connections = {}
    
    def _save_connections(self):
        """
        保存连接信息到配置文件
        """
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.connections, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.warning(f"保存 SSH 连接配置失败: {str(e)}")
    
    def save_connection(self, name: str, host: str, username: str, password: str, port: int = 22) -> Dict[str, Any]:
        """
        保存 SSH 连接信息
        
        :param name: 连接名称（用于标识）
        :param host: 主机 IP 地址或域名
        :param username: 用户名
        :param password: 密码
        :param port: SSH 端口，默认 22
        :return: 包含成功状态和结果的字典
        """
        try:
            if not name or not host or not username or not password:
                return {
                    "success": False,
                    "error": "连接名称、主机地址、用户名和密码不能为空"
                }
            
            if port < 1 or port > 65535:
                return {
                    "success": False,
                    "error": "端口号必须在 1-65535 之间"
                }
            
            self.connections[name] = {
                "host": host,
                "username": username,
                "port": port
            }
            self.connections[name].update(self._protect_password(password))
            
            self._save_connections()
            
            return {
                "success": True,
                "message": f"SSH 连接 '{name}' 保存成功",
                "connection_name": name,
                "host": host,
                "username": username,
                "port": port
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"保存 SSH 连接失败: {str(e)}"
            }
    
    def list_connections(self) -> Dict[str, Any]:
        """
        列出所有已保存的 SSH 连接
        
        :return: 包含成功状态和连接列表的字典
        """
        try:
            if not self.connections:
                return {
                    "success": True,
                    "message": "暂无已保存的 SSH 连接",
                    "connections": []
                }
            
            connections_list = []
            for name, info in self.connections.items():
                connections_list.append({
                    "name": name,
                    "host": info["host"],
                    "username": info["username"],
                    "port": info["port"]
                })
            
            return {
                "success": True,
                "message": f"找到 {len(connections_list)} 个已保存的 SSH 连接",
                "connections": connections_list
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"列出 SSH 连接失败: {str(e)}"
            }
    
    def delete_connection(self, name: str) -> Dict[str, Any]:
        """
        删除已保存的 SSH 连接
        
        :param name: 连接名称
        :return: 包含成功状态和结果的字典
        """
        try:
            if name not in self.connections:
                return {
                    "success": False,
                    "error": f"未找到名为 '{name}' 的 SSH 连接"
                }
            
            del self.connections[name]
            self._save_connections()
            
            return {
                "success": True,
                "message": f"SSH 连接 '{name}' 已删除",
                "connection_name": name
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"删除 SSH 连接失败: {str(e)}"
            }
    
    def get_connection(self, name: str) -> Dict[str, Any]:
        """
        获取指定连接的信息
        
        :param name: 连接名称
        :return: 包含连接信息的字典
        """
        try:
            if name not in self.connections:
                return {
                    "success": False,
                    "error": f"未找到名为 '{name}' 的 SSH 连接"
                }
            
            info = self.connections[name]
            return {
                "success": True,
                "connection_name": name,
                "host": info["host"],
                "username": info["username"],
                "port": info["port"]
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"获取 SSH 连接信息失败: {str(e)}"
            }


    def get_connection_credentials(self, name: str) -> Dict[str, Any]:
        """
        获取完整连接信息，供内部连接逻辑使用。
        """
        try:
            if name not in self.connections:
                return {
                    "success": False,
                    "error": f"鏈壘鍒板悕涓?'{name}' 鐨?SSH 杩炴帴"
                }

            info = self.connections[name]
            password = self._unprotect_password(info)
            return {
                "success": True,
                "connection_name": name,
                "host": info["host"],
                "username": info["username"],
                "password": password,
                "port": info["port"]
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"鑾峰彇 SSH 杩炴帴璁ゅ瘑淇℃伅澶辫触: {str(e)}"
            }


class SSHHandler:
    def __init__(self):
        """
        SSH 处理器初始化
        """
        self.client = None
        self.connection_manager = SSHConnectionConfig()
        self.current_host = None
        self.current_username = None
        self.current_port = None

    def _clear_connection_metadata(self):
        self.current_host = None
        self.current_username = None
        self.current_port = None

    def _quote_remote_path(self, remote_path: str) -> str:
        return shlex.quote(str(remote_path))

    def _build_scp_destination(self, remote_path: str) -> str:
        if not self.current_host or not self.current_username:
            raise ValueError("当前 SSH 连接信息不可用，无法构造 SCP 目标")
        return f"{self.current_username}@{self.current_host}:{shlex.quote(str(remote_path))}"
    
    def connect(self, host: str, username: str, password: str, port: int = 22, timeout: int = 10) -> Dict[str, Any]:
        """
        连接到 SSH 服务器
        
        :param host: 主机 IP 地址或域名
        :param username: 用户名
        :param password: 密码
        :param port: SSH 端口，默认 22
        :param timeout: 连接超时时间（秒），默认 10
        :return: 包含成功状态和结果的字典
        """
        if not PARAMIKO_AVAILABLE:
            return {
                "success": False,
                "error": "paramiko 库不可用，无法建立 SSH 连接"
            }
        
        try:
            if self.client is not None:
                self.disconnect()
            
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            self.client.connect(
                hostname=host,
                username=username,
                password=password,
                port=port,
                timeout=timeout
            )
            self.current_host = host
            self.current_username = username
            self.current_port = port
            
            return {
                "success": True,
                "message": f"成功连接到 {host}:{port}",
                "host": host,
                "port": port,
                "username": username
            }
        except paramiko.AuthenticationException:
            if self.client is not None:
                self.client.close()
                self.client = None
            self._clear_connection_metadata()
            return {
                "success": False,
                "error": "SSH 认证失败：用户名或密码错误",
                "host": host,
                "port": port
            }
        except paramiko.SSHException as e:
            if self.client is not None:
                self.client.close()
                self.client = None
            self._clear_connection_metadata()
            return {
                "success": False,
                "error": f"SSH 连接错误: {str(e)}",
                "host": host,
                "port": port
            }
        except Exception as e:
            if self.client is not None:
                self.client.close()
                self.client = None
            self._clear_connection_metadata()
            return {
                "success": False,
                "error": f"连接失败: {str(e)}",
                "host": host,
                "port": port
            }
    
    def connect_by_name(self, name: str, timeout: int = 10) -> Dict[str, Any]:
        """
        使用已保存的连接名称连接到 SSH 服务器
        
        :param name: 连接名称
        :param timeout: 连接超时时间（秒），默认 10
        :return: 包含成功状态和结果的字典
        """
        try:
            if name not in self.connection_manager.connections:
                return {
                    "success": False,
                    "error": f"未找到名为 '{name}' 的 SSH 连接"
                }
            
            info_result = self.connection_manager.get_connection_credentials(name)
            if not info_result["success"]:
                return info_result
            return self.connect(
                host=info_result["host"],
                username=info_result["username"],
                password=info_result["password"],
                port=info_result["port"],
                timeout=timeout
            )
        except Exception as e:
            return {
                "success": False,
                "error": f"通过名称连接失败: {str(e)}"
            }
    
    def execute_command(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """
        在已连接的 SSH 服务器上执行命令
        
        :param command: 要执行的命令
        :param timeout: 命令执行超时时间（秒），默认 30
        :return: 包含成功状态和结果的字典
        """
        if not PARAMIKO_AVAILABLE:
            return {
                "success": False,
                "error": "paramiko 库不可用，无法执行命令"
            }
        
        if self.client is None or not self.client.get_transport() or not self.client.get_transport().is_active():
            return {
                "success": False,
                "error": "未连接到 SSH 服务器，请先建立连接"
            }
        
        result_container = {
            'exit_code': None,
            'output': '',
            'error_output': '',
            'exception': None,
            'completed': False,
            'execution_time': 0,
            'channel': None
        }
        
        def _execute():
            try:
                start_time = time.time()
                stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
                result_container['channel'] = stdout.channel
                
                result_container['exit_code'] = stdout.channel.recv_exit_status()
                result_container['output'] = stdout.read().decode('utf-8', errors='replace')
                result_container['error_output'] = stderr.read().decode('utf-8', errors='replace')
                result_container['execution_time'] = time.time() - start_time
                result_container['completed'] = True
            except Exception as e:
                result_container['exception'] = e
                result_container['completed'] = True
        
        try:
            thread = threading.Thread(target=_execute)
            thread.daemon = True
            thread.start()
            thread.join(timeout=timeout)
            
            if not result_container['completed']:
                channel = result_container.get('channel')
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:
                        pass
                return {
                    "success": False,
                    "error": f"命令执行超时 ({timeout}秒): {command}",
                    "command": command,
                    "timeout": timeout
                }
            
            if result_container['exception']:
                raise result_container['exception']
            
            output = result_container['output']
            error_output = result_container['error_output']
            exit_code = result_container['exit_code']
            execution_time = result_container['execution_time']
            
            output_lines = output.strip().split('\n') if output.strip() else []
            error_lines = error_output.strip().split('\n') if error_output.strip() else []
            
            status_msg = "成功" if exit_code == 0 else f"失败(退出码: {exit_code})"
            output_summary = f"{len(output_lines)} 行输出" if output_lines else "无输出"
            error_summary = f"{len(error_lines)} 行错误" if error_lines else "无错误"
            
            return {
                "success": exit_code == 0,
                "command": command,
                "exit_code": exit_code,
                "stdout": output,
                "stderr": error_output,
                "execution_time": round(execution_time, 2),
                "output_lines": len(output_lines),
                "error_lines": len(error_lines),
                "output_preview": output_lines[:5] if output_lines else [],
                "error_preview": error_lines[:5] if error_lines else [],
                "message": f"命令执行{status_msg} | 耗时: {execution_time:.2f}秒 | {output_summary} | {error_summary}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"执行命令失败: {str(e)}",
                "command": command
            }
    
    def disconnect(self) -> Dict[str, Any]:
        """
        断开 SSH 连接
        
        :return: 包含成功状态和结果的字典
        """
        try:
            if self.client is not None:
                self.client.close()
                self.client = None
                self._clear_connection_metadata()
                return {
                    "success": True,
                    "message": "SSH 连接已断开"
                }
            self._clear_connection_metadata()
            return {
                "success": True,
                "message": "当前没有活动的 SSH 连接"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"断开连接失败: {str(e)}"
            }
    
    def check_connection(self) -> Dict[str, Any]:
        """
        检查 SSH 连接状态
        
        :return: 包含连接状态的字典
        """
        try:
            if self.client is None:
                return {
                    "success": True,
                    "connected": False,
                    "message": "当前没有活动的 SSH 连接"
                }
            
            transport = self.client.get_transport()
            if transport is None or not transport.is_active():
                return {
                    "success": True,
                    "connected": False,
                    "message": "SSH 连接已断开"
                }
            
            return {
                "success": True,
                "connected": True,
                "message": "SSH 连接正常"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"检查连接状态失败: {str(e)}"
            }
    
    def upload_file_sftp(self, local_path: str, remote_path: str, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 SFTP 上传文件到远程服务器
        
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param timeout: 上传超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        import time
        import os
        import threading
        from pathlib import Path
        
        start_time = time.time()
        
        if not PARAMIKO_AVAILABLE:
            return {
                "success": False,
                "error": "paramiko 库不可用，无法上传文件"
            }
        
        if self.client is None or not self.client.get_transport() or not self.client.get_transport().is_active():
            return {
                "success": False,
                "error": "未连接到 SSH 服务器，请先建立连接"
            }
        
        local_file = Path(local_path)
        if not local_file.exists():
            return {
                "success": False,
                "error": f"本地文件不存在: {local_path}"
            }
        
        if not local_file.is_file():
            return {
                "success": False,
                "error": f"本地路径不是文件: {local_path}"
            }
        
        result_container = {
            'success': False,
            'error': None,
            'completed': False,
            'uploaded_size': 0,
            'sftp': None
        }
        
        def _upload():
            sftp = None
            try:
                sftp = self.client.open_sftp()
                result_container['sftp'] = sftp
                sftp.put(str(local_path), remote_path)
                result_container['success'] = True
                result_container['uploaded_size'] = local_file.stat().st_size
                result_container['completed'] = True
            except Exception as e:
                result_container['error'] = e
                result_container['completed'] = True
            finally:
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass
                result_container['sftp'] = None
        
        try:
            thread = threading.Thread(target=_upload)
            thread.daemon = True
            thread.start()
            thread.join(timeout=timeout)
            
            if not result_container['completed']:
                sftp = result_container.get('sftp')
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass
                return {
                    "success": False,
                    "error": f"SFTP 上传超时 ({timeout}秒): {local_path}",
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "timeout": timeout
                }
            
            if result_container['error']:
                raise result_container['error']
            
            execution_time = time.time() - start_time
            file_size_mb = round(result_container['uploaded_size'] / (1024 * 1024), 2)
            
            return {
                "success": True,
                "local_path": local_path,
                "remote_path": remote_path,
                "file_size": result_container['uploaded_size'],
                "file_size_mb": file_size_mb,
                "execution_time": round(execution_time, 2),
                "message": f"SFTP 上传成功 | 文件: {local_file.name} | 大小: {file_size_mb:.2f}MB | 耗时: {execution_time:.2f}秒"
            }
        except Exception as e:
            execution_time = time.time() - start_time
            return {
                "success": False,
                "error": f"SFTP 上传失败: {str(e)}",
                "local_path": local_path,
                "remote_path": remote_path,
                "execution_time": round(execution_time, 2)
            }
    
    def download_file_sftp(self, remote_path: str, local_path: str, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 SFTP 从远程服务器下载文件
        
        :param remote_path: 远程文件路径
        :param local_path: 本地文件路径
        :param timeout: 下载超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        import time
        import os
        import threading
        from pathlib import Path
        
        start_time = time.time()
        
        if not PARAMIKO_AVAILABLE:
            return {
                "success": False,
                "error": "paramiko 库不可用，无法下载文件"
            }
        
        if self.client is None or not self.client.get_transport() or not self.client.get_transport().is_active():
            return {
                "success": False,
                "error": "未连接到 SSH 服务器，请先建立连接"
            }
        
        local_file = Path(local_path)
        local_file.parent.mkdir(parents=True, exist_ok=True)
        
        result_container = {
            'success': False,
            'error': None,
            'completed': False,
            'downloaded_size': 0,
            'sftp': None
        }
        
        def _download():
            sftp = None
            try:
                sftp = self.client.open_sftp()
                result_container['sftp'] = sftp
                sftp.get(remote_path, str(local_path))
                result_container['success'] = True
                result_container['downloaded_size'] = local_file.stat().st_size
                result_container['completed'] = True
            except Exception as e:
                result_container['error'] = e
                result_container['completed'] = True
            finally:
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass
                result_container['sftp'] = None
        
        try:
            thread = threading.Thread(target=_download)
            thread.daemon = True
            thread.start()
            thread.join(timeout=timeout)
            
            if not result_container['completed']:
                sftp = result_container.get('sftp')
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass
                return {
                    "success": False,
                    "error": f"SFTP 下载超时 ({timeout}秒): {remote_path}",
                    "remote_path": remote_path,
                    "local_path": local_path,
                    "timeout": timeout
                }
            
            if result_container['error']:
                raise result_container['error']
            
            execution_time = time.time() - start_time
            file_size_mb = round(result_container['downloaded_size'] / (1024 * 1024), 2)
            
            return {
                "success": True,
                "remote_path": remote_path,
                "local_path": local_path,
                "file_size": result_container['downloaded_size'],
                "file_size_mb": file_size_mb,
                "execution_time": round(execution_time, 2),
                "message": f"SFTP 下载成功 | 文件: {local_file.name} | 大小: {file_size_mb:.2f}MB | 耗时: {execution_time:.2f}秒"
            }
        except Exception as e:
            execution_time = time.time() - start_time
            return {
                "success": False,
                "error": f"SFTP 下载失败: {str(e)}",
                "remote_path": remote_path,
                "local_path": local_path,
                "execution_time": round(execution_time, 2)
            }
    
    def upload_file_scp(self, local_path: str, remote_path: str, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 SCP 命令上传文件到远程服务器
        
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param timeout: 上传超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        import time
        from pathlib import Path
        
        start_time = time.time()
        
        if not PARAMIKO_AVAILABLE:
            return {
                "success": False,
                "error": "paramiko 库不可用，无法上传文件"
            }
        
        if self.client is None or not self.client.get_transport() or not self.client.get_transport().is_active():
            return {
                "success": False,
                "error": "未连接到 SSH 服务器，请先建立连接"
            }
        
        local_file = Path(local_path)
        if not local_file.exists():
            return {
                "success": False,
                "error": f"本地文件不存在: {local_path}"
            }
        
        if not local_file.is_file():
            return {
                "success": False,
                "error": f"本地路径不是文件: {local_path}"
            }
        
        try:
            scp_destination = self._build_scp_destination(remote_path)
            result_container = {
                'success': False,
                'error': None,
                'completed': False,
                'exit_code': None,
                'stdout': '',
                'stderr': '',
                'process': None
            }
            
            def _scp_upload():
                try:
                    command = ['scp', '-o', 'StrictHostKeyChecking=no']
                    if self.current_port and self.current_port != 22:
                        command.extend(['-P', str(self.current_port)])
                    command.extend([str(local_path), scp_destination])

                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        text=True
                    )
                    result_container['process'] = process
                    stdout, stderr = process.communicate()
                    result_container['exit_code'] = process.returncode
                    result_container['stdout'] = stdout
                    result_container['stderr'] = stderr
                    result_container['success'] = process.returncode == 0
                    result_container['completed'] = True
                except Exception as e:
                    result_container['error'] = e
                    result_container['completed'] = True
                finally:
                    result_container['process'] = None
            
            thread = threading.Thread(target=_scp_upload)
            thread.daemon = True
            thread.start()
            thread.join(timeout=timeout)
            
            if not result_container['completed']:
                process = result_container.get('process')
                if process is not None:
                    try:
                        process.kill()
                    except Exception:
                        pass
                return {
                    "success": False,
                    "error": f"SCP 上传超时 ({timeout}秒): {local_path}",
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "timeout": timeout
                }
            
            if result_container['error']:
                raise result_container['error']
            
            execution_time = time.time() - start_time
            file_size_mb = round(local_file.stat().st_size / (1024 * 1024), 2)
            
            if result_container['success']:
                return {
                    "success": True,
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "scp_destination": scp_destination,
                    "file_size": local_file.stat().st_size,
                    "file_size_mb": file_size_mb,
                    "execution_time": round(execution_time, 2),
                    "message": f"SCP 上传成功 | 文件: {local_file.name} | 大小: {file_size_mb:.2f}MB | 耗时: {execution_time:.2f}秒"
                }
            else:
                return {
                    "success": False,
                    "error": f"SCP 上传失败 (退出码: {result_container['exit_code']}): {result_container['stderr']}",
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "scp_destination": scp_destination,
                    "execution_time": round(execution_time, 2)
                }
        except Exception as e:
            execution_time = time.time() - start_time
            return {
                "success": False,
                "error": f"SCP 上传失败: {str(e)}",
                "local_path": local_path,
                "remote_path": remote_path,
                "execution_time": round(execution_time, 2)
            }
    
    def upload_file_ftp(self, ftp_host: str, ftp_username: str, ftp_password: str, 
                       local_path: str, remote_path: str, port: int = 21, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 FTP 上传文件到远程服务器
        
        :param ftp_host: FTP 服务器地址
        :param ftp_username: FTP 用户名
        :param ftp_password: FTP 密码
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param port: FTP 端口，默认 21
        :param timeout: 上传超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        import time
        from pathlib import Path
        
        start_time = time.time()
        
        try:
            from ftplib import FTP, error_perm
            import os
            
            local_file = Path(local_path)
            if not local_file.exists():
                return {
                    "success": False,
                    "error": f"本地文件不存在: {local_path}"
                }
            
            if not local_file.is_file():
                return {
                    "success": False,
                    "error": f"本地路径不是文件: {local_path}"
                }
            
            result_container = {
                'success': False,
                'error': None,
                'completed': False,
                'uploaded_size': 0,
                'ftp': None
            }
            
            def _ftp_upload():
                ftp = None
                try:
                    ftp = FTP()
                    result_container['ftp'] = ftp
                    ftp.connect(ftp_host, port=port, timeout=timeout)
                    ftp.login(ftp_username, ftp_password)
                    
                    remote_dir = os.path.dirname(remote_path)
                    remote_filename = os.path.basename(remote_path)
                    
                    if remote_dir:
                        ftp.cwd(remote_dir)
                    
                    with open(local_path, 'rb') as f:
                        ftp.storbinary(f'STOR {remote_filename}', f)
                    
                    result_container['success'] = True
                    result_container['uploaded_size'] = local_file.stat().st_size
                    result_container['completed'] = True
                except Exception as e:
                    result_container['error'] = e
                    result_container['completed'] = True
                finally:
                    if ftp is not None:
                        try:
                            ftp.quit()
                        except Exception:
                            try:
                                ftp.close()
                            except Exception:
                                pass
                    result_container['ftp'] = None
            
            thread = threading.Thread(target=_ftp_upload)
            thread.daemon = True
            thread.start()
            thread.join(timeout=timeout)
            
            if not result_container['completed']:
                ftp = result_container.get('ftp')
                if ftp is not None:
                    try:
                        ftp.close()
                    except Exception:
                        pass
                return {
                    "success": False,
                    "error": f"FTP 上传超时 ({timeout}秒): {local_path}",
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "ftp_host": ftp_host,
                    "timeout": timeout
                }
            
            if result_container['error']:
                raise result_container['error']
            
            execution_time = time.time() - start_time
            file_size_mb = round(result_container['uploaded_size'] / (1024 * 1024), 2)
            
            return {
                "success": True,
                "local_path": local_path,
                "remote_path": remote_path,
                "ftp_host": ftp_host,
                "ftp_port": port,
                "file_size": result_container['uploaded_size'],
                "file_size_mb": file_size_mb,
                "execution_time": round(execution_time, 2),
                "message": f"FTP 上传成功 | 文件: {local_file.name} | 大小: {file_size_mb:.2f}MB | 耗时: {execution_time:.2f}秒"
            }
        except ImportError:
            execution_time = time.time() - start_time
            return {
                "success": False,
                "error": "ftplib 库不可用，无法使用 FTP",
                "local_path": local_path,
                "remote_path": remote_path,
                "execution_time": round(execution_time, 2)
            }
        except Exception as e:
            execution_time = time.time() - start_time
            return {
                "success": False,
                "error": f"FTP 上传失败: {str(e)}",
                "local_path": local_path,
                "remote_path": remote_path,
                "ftp_host": ftp_host,
                "execution_time": round(execution_time, 2)
            }


class SshFileProxy:
    """
    SSH 远程文件代理
    提供通用的远程文件操作接口，让本地工具通过SSH透明地操作远程文件
    """
    
    WRITE_SAFE_CHUNK_SIZE = 1024
    
    def __init__(self, ssh_handler: SSHHandler):
        """
        初始化远程文件代理
        
        :param ssh_handler: SSHHandler 实例
        """
        self.ssh_handler = ssh_handler
        self._temp_dir = tempfile.mkdtemp(prefix="ssh_proxy_")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出，清理临时文件"""
        self.cleanup()
    
    def cleanup(self):
        """清理临时文件"""
        try:
            if os.path.exists(self._temp_dir):
                shutil.rmtree(self._temp_dir)
        except Exception as e:
            logging.warning(f"清理临时文件失败: {str(e)}")
    
    def _get_temp_path(self, remote_path: str) -> str:
        """
        获取远程文件对应的本地临时路径
        
        :param remote_path: 远程文件路径
        :return: 本地临时文件路径
        """
        filename = os.path.basename(remote_path)
        return os.path.join(self._temp_dir, filename)
    
    def _check_connection(self) -> Dict[str, Any]:
        """
        检查 SSH 连接状态
        
        :return: 连接状态字典
        """
        if not PARAMIKO_AVAILABLE:
            return {
                "success": False,
                "error": "paramiko 库不可用"
            }
        
        if self.ssh_handler.client is None:
            return {
                "success": False,
                "error": "未建立 SSH 连接"
            }
        
        transport = self.ssh_handler.client.get_transport()
        if transport is None or not transport.is_active():
            return {
                "success": False,
                "error": "SSH 连接已断开"
            }
        
        return {"success": True}
    
    def read_file(self, remote_path: str, encoding: str = 'utf-8') -> Dict[str, Any]:
        """
        读取远程文件内容
        
        :param remote_path: 远程文件路径
        :param encoding: 文件编码，默认 utf-8
        :return: 包含文件内容的字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            local_temp_path = self._get_temp_path(remote_path)
            
            download_result = self.ssh_handler.download_file_sftp(remote_path, local_temp_path)
            if not download_result["success"]:
                return {
                    "success": False,
                    "error": f"下载远程文件失败: {download_result.get('error', '未知错误')}"
                }
            
            with open(local_temp_path, 'r', encoding=encoding) as f:
                content = f.read()
            
            return {
                "success": True,
                "content": content,
                "remote_path": remote_path,
                "local_temp_path": local_temp_path,
                "size": len(content),
                "encoding": encoding
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"读取远程文件失败: {str(e)}"
            }
    
    def write_file(self, remote_path: str, content: str, encoding: str = 'utf-8', 
                   backup: bool = True, chunk_size: int = None) -> Dict[str, Any]:
        """
        写入内容到远程文件（使用分块写入，安全可靠）
        
        :param remote_path: 远程文件路径
        :param content: 要写入的内容
        :param encoding: 文件编码，默认 utf-8
        :param backup: 是否备份原文件，默认 True
        :param chunk_size: 块大小，默认使用 WRITE_SAFE_CHUNK_SIZE（1KB）
        :return: 写入结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            local_temp_path = self._get_temp_path(remote_path)
            
            if backup:
                backup_result = self._backup_remote_file(remote_path)
                if not backup_result["success"]:
                    return backup_result
            
            if chunk_size is None:
                chunk_size = self.WRITE_SAFE_CHUNK_SIZE
            
            write_result = self._write_file_chunked(local_temp_path, content, encoding, chunk_size)
            if not write_result["success"]:
                return {
                    "success": False,
                    "error": f"写入本地临时文件失败: {write_result.get('error', '未知错误')}"
                }
            
            upload_result = self.ssh_handler.upload_file_sftp(local_temp_path, remote_path)
            if not upload_result["success"]:
                return {
                    "success": False,
                    "error": f"上传文件到远程失败: {upload_result.get('error', '未知错误')}"
                }
            
            return {
                "success": True,
                "message": f"成功写入远程文件: {remote_path}",
                "remote_path": remote_path,
                "size": len(content),
                "encoding": encoding,
                "chunks_written": write_result.get("chunks_written", 1),
                "chunk_size": chunk_size,
                "checksum": write_result.get("checksum"),
                "backup_path": backup_result.get("backup_path") if backup else None
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"写入远程文件失败: {str(e)}"
            }
    
    def _backup_remote_file(self, remote_path: str) -> Dict[str, Any]:
        """
        备份远程文件
        
        :param remote_path: 远程文件路径
        :return: 备份结果字典
        """
        try:
            timestamp = int(time.time() * 1000)
            backup_path = f"{remote_path}.backup_{timestamp}"
            
            quoted_remote_path = self.ssh_handler._quote_remote_path(remote_path)
            quoted_backup_path = self.ssh_handler._quote_remote_path(backup_path)
            cmd = f"cp -- {quoted_remote_path} {quoted_backup_path}"
            result = self.ssh_handler.execute_command(cmd)
            
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"备份文件失败: {result.get('error', result.get('stderr', '未知错误'))}"
                }
            
            return {
                "success": True,
                "backup_path": backup_path,
                "message": f"已备份到: {backup_path}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"备份远程文件失败: {str(e)}"
            }
    
    def _write_file_chunked(self, file_path: str, content: str, encoding: str = 'utf-8', 
                           chunk_size: int = None) -> Dict[str, Any]:
        """
        分块写入文件内容（安全可靠）
        :param file_path: 本地文件路径
        :param content: 要写入的内容
        :param encoding: 文件编码
        :param chunk_size: 块大小，默认使用 WRITE_SAFE_CHUNK_SIZE（1KB）
        :return: 写入结果，包含success、message、total_size、chunks_written、chunk_size、encoding、checksum等字段
        """
        if chunk_size is None:
            chunk_size = self.WRITE_SAFE_CHUNK_SIZE
            
        try:
            checksum = hashlib.md5(content.encode(encoding)).hexdigest()
            
            file_path_obj = Path(file_path)
            file_path_obj.parent.mkdir(parents=True, exist_ok=True)
            
            total_chunks = (len(content) + chunk_size - 1) // chunk_size
            
            chunks_written = 0
            written_content = ""
            
            with open(file_path, 'w', encoding=encoding) as f:
                for i in range(total_chunks):
                    start_pos = i * chunk_size
                    end_pos = min(start_pos + chunk_size, len(content))
                    chunk = content[start_pos:end_pos]
                    
                    f.write(chunk)
                    written_content += chunk
                    chunks_written += 1
                    
                    if chunks_written % 5 == 0:
                        print(f"已写入 {chunks_written}/{total_chunks} 块...", file=sys.stderr)
            
            verification_result = self._verify_chunked_write_integrity(written_content, content, checksum, encoding)
            if not verification_result["success"]:
                return {
                    "success": False,
                    "error": f"内容完整性验证失败: {verification_result['error']}",
                    "chunks_written": chunks_written
                }
            
            return {
                "success": True,
                "message": f"成功写入文件: {file_path}",
                "total_size": len(content),
                "chunks_written": chunks_written,
                "chunk_size": chunk_size,
                "encoding": encoding,
                "checksum": checksum
            }
            
        except Exception as e:
            return {
                "success": False, 
                "error": f"分块写入文件失败: {str(e)}",
                "chunks_written": chunks_written if 'chunks_written' in locals() else 0
            }
    
    def _verify_chunked_write_integrity(self, written_content: str, original_content: str, 
                                       expected_checksum: str, encoding: str = 'utf-8') -> Dict[str, Any]:
        """
        验证分块写入内容的完整性
        :param written_content: 实际写入的内容
        :param original_content: 原始内容
        :param expected_checksum: 期望的校验和
        :param encoding: 文件编码
        :return: 包含验证结果的字典
        """
        if len(written_content) != len(original_content):
            return {
                "success": False,
                "error": f"内容长度不匹配: 期望 {len(original_content)}, 实际 {len(written_content)}"
            }
        
        if written_content != original_content:
            return {
                "success": False,
                "error": "写入的内容与原始内容不匹配"
            }
        
        actual_checksum = hashlib.md5(written_content.encode(encoding)).hexdigest()
        
        if actual_checksum != expected_checksum:
            return {
                "success": False,
                "error": f"校验和不匹配: 期望 {expected_checksum}, 实际 {actual_checksum}"
            }
        
        return {"success": True}
    
    def edit_file(self, remote_path: str, operations: List[Dict[str, Any]], 
                  encoding: str = 'utf-8', backup: bool = True, chunk_size: int = None) -> Dict[str, Any]:
        """
        编辑远程文件（支持批量操作，支持分块写入）
        
        :param remote_path: 远程文件路径
        :param operations: 操作列表，支持的操作类型：
            - {'type': 'replace', 'old': 'old_text', 'new': 'new_text'}
            - {'type': 'insert', 'line': line_num, 'content': 'content'}
            - {'type': 'delete', 'pattern': 'pattern'}
        :param encoding: 文件编码
        :param backup: 是否备份原文件
        :param chunk_size: 分块大小（字节），默认 1024（1KB），设置为 None 使用默认值
        :return: 编辑结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            read_result = self.read_file(remote_path, encoding)
            if not read_result["success"]:
                return read_result
            
            content = read_result["content"]
            lines = content.split('\n')
            
            executed_count = 0
            for op in operations:
                op_type = op.get('type')
                
                if op_type == 'replace':
                    old_text = op.get('old') or op.get('old_text')
                    new_text = op.get('new') or op.get('new_text', '')
                    if old_text:
                        if '\n' in old_text:
                            if old_text in content:
                                content = content.replace(old_text, new_text, 1)
                                executed_count += 1
                                lines = content.split('\n')
                            else:
                                print(f"警告: 多行文本未找到匹配: {old_text[:50]}...")
                        else:
                            for i, line in enumerate(lines):
                                if old_text in line:
                                    lines[i] = line.replace(old_text, new_text, 1)
                                    executed_count += 1
                                    break
                
                elif op_type == 'insert':
                    line_num = op.get('line')
                    insert_content = op.get('content', '')
                    if line_num is not None and 0 <= line_num <= len(lines):
                        lines.insert(line_num, insert_content)
                        executed_count += 1
                
                elif op_type == 'delete':
                    pattern = op.get('pattern')
                    if pattern:
                        lines = [line for line in lines if pattern not in line]
                        executed_count += 1
            
            new_content = '\n'.join(lines)
            write_result = self.write_file(remote_path, new_content, encoding, backup, chunk_size)
            
            if not write_result["success"]:
                return write_result
            
            return {
                "success": True,
                "message": f"成功编辑远程文件，执行了 {executed_count} 个操作",
                "remote_path": remote_path,
                "operations_executed": executed_count,
                "backup_path": write_result.get("backup_path")
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"编辑远程文件失败: {str(e)}"
            }
    
    def append_file(self, remote_path: str, content: str, encoding: str = 'utf-8', 
                    chunk_size: int = None) -> Dict[str, Any]:
        """
        追加内容到远程文件（支持分块写入）
        
        :param remote_path: 远程文件路径
        :param content: 要追加的内容
        :param encoding: 文件编码
        :param chunk_size: 分块大小（字节），默认 1024（1KB），设置为 None 使用默认值
        :return: 追加结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            read_result = self.read_file(remote_path, encoding)
            
            if read_result["success"]:
                existing_content = read_result["content"]
                new_content = existing_content + content
            else:
                new_content = content
            
            return self.write_file(remote_path, new_content, encoding, backup=False, chunk_size=chunk_size)
        except Exception as e:
            return {
                "success": False,
                "error": f"追加内容到远程文件失败: {str(e)}"
            }
    
    def file_exists(self, remote_path: str) -> Dict[str, Any]:
        """
        检查远程文件是否存在
        
        :param remote_path: 远程文件路径
        :return: 检查结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            quoted_remote_path = self.ssh_handler._quote_remote_path(remote_path)
            cmd = f"if [ -f {quoted_remote_path} ]; then printf exists; else printf not_exists; fi"
            result = self.ssh_handler.execute_command(cmd)
            
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"检查文件失败: {result.get('error', '未知错误')}"
                }
            
            exists = result.get("stdout", "").strip() == "exists"
            return {
                "success": True,
                "exists": exists,
                "remote_path": remote_path
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"检查远程文件失败: {str(e)}"
            }
    
    def delete_file(self, remote_path: str, backup: bool = True) -> Dict[str, Any]:
        """
        删除远程文件
        
        :param remote_path: 远程文件路径
        :param backup: 是否备份原文件
        :return: 删除结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            if backup:
                backup_result = self._backup_remote_file(remote_path)
                if not backup_result["success"]:
                    return backup_result
            
            quoted_remote_path = self.ssh_handler._quote_remote_path(remote_path)
            cmd = f"rm -- {quoted_remote_path}"
            result = self.ssh_handler.execute_command(cmd)
            
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"删除文件失败: {result.get('error', result.get('stderr', '未知错误'))}"
                }
            
            return {
                "success": True,
                "message": f"成功删除远程文件: {remote_path}",
                "remote_path": remote_path,
                "backup_path": backup_result.get("backup_path") if backup else None
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"删除远程文件失败: {str(e)}"
            }
    
    def list_directory(self, remote_path: str = ".") -> Dict[str, Any]:
        """
        列出远程目录内容
        
        :param remote_path: 远程目录路径，默认当前目录
        :return: 目录内容列表
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            quoted_remote_path = self.ssh_handler._quote_remote_path(remote_path)
            cmd = f"ls -la -- {quoted_remote_path}"
            result = self.ssh_handler.execute_command(cmd)
            
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"列出目录失败: {result.get('error', '未知错误')}"
                }
            
            lines = result.get("stdout", "").strip().split('\n')
            files = []
            
            for line in lines[1:]:
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 9:
                        files.append({
                            "permissions": parts[0],
                            "links": parts[1],
                            "owner": parts[2],
                            "group": parts[3],
                            "size": parts[4],
                            "date": " ".join(parts[5:8]),
                            "name": " ".join(parts[8:])
                        })
            
            return {
                "success": True,
                "remote_path": remote_path,
                "files": files,
                "count": len(files)
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"列出远程目录失败: {str(e)}"
            }
    
    def invoke_local_tool(self, remote_path: str, local_tool_handler: Callable, 
                          tool_args: Dict[str, Any] = None, 
                          sync_back: bool = True,
                          backup: bool = True,
                          encoding: str = 'utf-8') -> Dict[str, Any]:
        """
        调用本地工具处理远程文件
        
        工作流程：
        1. 下载远程文件到本地临时目录
        2. 调用本地工具处理该文件
        3. （可选）将处理后的文件上传回远程服务器
        
        :param remote_path: 远程文件路径
        :param local_tool_handler: 本地工具处理函数，签名为 (local_file_path: str, **kwargs) -> Dict[str, Any]
                                   返回值应包含 'success' 和可选的 'modified' (是否修改了文件)
        :param tool_args: 传递给本地工具的额外参数
        :param sync_back: 处理完成后是否同步回远程服务器，默认 True
        :param backup: 同步回远程时是否备份原文件，默认 True
        :param encoding: 文件编码，默认 utf-8
        :return: 处理结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        if tool_args is None:
            tool_args = {}
        
        local_temp_path = None
        original_checksum = None
        
        try:
            local_temp_path = self._get_temp_path(remote_path)
            
            download_result = self.ssh_handler.download_file_sftp(remote_path, local_temp_path)
            if not download_result["success"]:
                return {
                    "success": False,
                    "error": f"下载远程文件失败: {download_result.get('error', '未知错误')}",
                    "stage": "download"
                }
            
            with open(local_temp_path, 'rb') as f:
                original_checksum = hashlib.md5(f.read()).hexdigest()
            
            tool_result = local_tool_handler(local_temp_path, **tool_args)
            
            if not tool_result.get("success", True):
                return {
                    "success": False,
                    "error": f"本地工具执行失败: {tool_result.get('error', '未知错误')}",
                    "stage": "local_tool",
                    "tool_result": tool_result
                }
            
            file_modified = tool_result.get("modified", True)
            
            if sync_back and file_modified:
                with open(local_temp_path, 'rb') as f:
                    new_checksum = hashlib.md5(f.read()).hexdigest()
                
                if new_checksum != original_checksum:
                    if backup:
                        backup_result = self._backup_remote_file(remote_path)
                        if not backup_result["success"]:
                            return {
                                "success": False,
                                "error": f"备份远程文件失败: {backup_result.get('error', '未知错误')}",
                                "stage": "backup"
                            }
                    
                    upload_result = self.ssh_handler.upload_file_sftp(local_temp_path, remote_path)
                    if not upload_result["success"]:
                        return {
                            "success": False,
                            "error": f"上传文件到远程失败: {upload_result.get('error', '未知错误')}",
                            "stage": "upload"
                        }
                    
                    return {
                        "success": True,
                        "message": f"成功调用本地工具处理远程文件并同步回远程: {remote_path}",
                        "remote_path": remote_path,
                        "local_temp_path": local_temp_path,
                        "synced": True,
                        "backup_path": backup_result.get("backup_path") if backup else None,
                        "tool_result": tool_result
                    }
                else:
                    return {
                        "success": True,
                        "message": f"本地工具执行成功，文件未发生变化，无需同步: {remote_path}",
                        "remote_path": remote_path,
                        "local_temp_path": local_temp_path,
                        "synced": False,
                        "tool_result": tool_result
                    }
            
            return {
                "success": True,
                "message": f"成功调用本地工具处理远程文件: {remote_path}",
                "remote_path": remote_path,
                "local_temp_path": local_temp_path,
                "synced": False,
                "tool_result": tool_result
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"调用本地工具失败: {str(e)}",
                "stage": "unknown"
            }
    
    def invoke_local_tool_batch(self, remote_files: List[str], local_tool_handler: Callable,
                                tool_args: Dict[str, Any] = None,
                                sync_back: bool = True,
                                backup: bool = True,
                                encoding: str = 'utf-8',
                                stop_on_error: bool = False) -> Dict[str, Any]:
        """
        批量调用本地工具处理多个远程文件
        
        :param remote_files: 远程文件路径列表
        :param local_tool_handler: 本地工具处理函数
        :param tool_args: 传递给本地工具的额外参数
        :param sync_back: 处理完成后是否同步回远程服务器
        :param backup: 同步回远程时是否备份原文件
        :param encoding: 文件编码
        :param stop_on_error: 遇到错误时是否停止处理后续文件
        :return: 批量处理结果字典
        """
        if tool_args is None:
            tool_args = {}
        
        results = []
        success_count = 0
        error_count = 0
        
        for remote_path in remote_files:
            result = self.invoke_local_tool(
                remote_path=remote_path,
                local_tool_handler=local_tool_handler,
                tool_args=tool_args,
                sync_back=sync_back,
                backup=backup,
                encoding=encoding
            )
            
            results.append({
                "remote_path": remote_path,
                "success": result.get("success", False),
                "message": result.get("message", result.get("error", "未知结果")),
                "synced": result.get("synced", False)
            })
            
            if result.get("success"):
                success_count += 1
            else:
                error_count += 1
                if stop_on_error:
                    break
        
        return {
            "success": error_count == 0,
            "message": f"批量处理完成: {success_count} 成功, {error_count} 失败",
            "total": len(remote_files),
            "processed": len(results),
            "success_count": success_count,
            "error_count": error_count,
            "results": results
        }
    
    def invoke_local_tool_with_content(self, remote_path: str, content_processor: Callable[[str], str],
                                       encoding: str = 'utf-8',
                                       backup: bool = True) -> Dict[str, Any]:
        """
        使用内容处理函数处理远程文件内容（简化版，直接处理文本内容）
        
        :param remote_path: 远程文件路径
        :param content_processor: 内容处理函数，签名为 (content: str) -> str
                                  接收文件内容，返回处理后的内容
        :param encoding: 文件编码
        :param backup: 是否备份原文件
        :return: 处理结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            read_result = self.read_file(remote_path, encoding)
            if not read_result["success"]:
                return read_result
            
            original_content = read_result["content"]
            processed_content = content_processor(original_content)
            
            if processed_content == original_content:
                return {
                    "success": True,
                    "message": f"内容处理完成，无变化: {remote_path}",
                    "remote_path": remote_path,
                    "modified": False
                }
            
            write_result = self.write_file(remote_path, processed_content, encoding, backup)
            if not write_result["success"]:
                return write_result
            
            return {
                "success": True,
                "message": f"内容处理完成并已写入: {remote_path}",
                "remote_path": remote_path,
                "modified": True,
                "original_size": len(original_content),
                "new_size": len(processed_content),
                "backup_path": write_result.get("backup_path")
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"内容处理失败: {str(e)}"
            }
    
    def get_local_copy(self, remote_path: str) -> Dict[str, Any]:
        """
        获取远程文件的本地副本路径（仅下载，不处理）
        
        :param remote_path: 远程文件路径
        :return: 包含本地文件路径的字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            local_temp_path = self._get_temp_path(remote_path)
            
            download_result = self.ssh_handler.download_file_sftp(remote_path, local_temp_path)
            if not download_result["success"]:
                return {
                    "success": False,
                    "error": f"下载远程文件失败: {download_result.get('error', '未知错误')}"
                }
            
            return {
                "success": True,
                "remote_path": remote_path,
                "local_path": local_temp_path,
                "message": f"已下载远程文件到本地: {local_temp_path}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"获取本地副本失败: {str(e)}"
            }
    
    def push_local_copy(self, local_path: str, remote_path: str, backup: bool = True) -> Dict[str, Any]:
        """
        将本地文件推送到远程（仅上传，用于手动处理后的同步）
        
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param backup: 是否备份远程原文件
        :return: 上传结果字典
        """
        conn_check = self._check_connection()
        if not conn_check["success"]:
            return conn_check
        
        try:
            if not os.path.exists(local_path):
                return {
                    "success": False,
                    "error": f"本地文件不存在: {local_path}"
                }
            
            if backup:
                backup_result = self._backup_remote_file(remote_path)
                if not backup_result["success"]:
                    return backup_result
            
            upload_result = self.ssh_handler.upload_file_sftp(local_path, remote_path)
            if not upload_result["success"]:
                return upload_result
            
            return {
                "success": True,
                "message": f"已推送本地文件到远程: {remote_path}",
                "local_path": local_path,
                "remote_path": remote_path,
                "backup_path": backup_result.get("backup_path") if backup else None
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"推送本地文件失败: {str(e)}"
            }


class SshToolManager:
    def __init__(self):
        """
        SSH 工具管理器
        """
        self.handler = SSHHandler()
    
    def save_ssh_connection(self, name: str, host: str, username: str, password: str, port: int = 22) -> Dict[str, Any]:
        """
        保存 SSH 连接信息
        
        :param name: 连接名称（用于标识）
        :param host: 主机 IP 地址或域名
        :param username: 用户名
        :param password: 密码
        :param port: SSH 端口，默认 22
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.connection_manager.save_connection(name, host, username, password, port)
        except Exception as e:
            return {"success": False, "error": f"保存 SSH 连接失败: {str(e)}"}
    
    def list_ssh_connections(self) -> Dict[str, Any]:
        """
        列出所有已保存的 SSH 连接
        
        :return: 包含成功状态和连接列表的字典
        """
        try:
            return self.handler.connection_manager.list_connections()
        except Exception as e:
            return {"success": False, "error": f"列出 SSH 连接失败: {str(e)}"}
    
    def delete_ssh_connection(self, name: str) -> Dict[str, Any]:
        """
        删除已保存的 SSH 连接
        
        :param name: 连接名称
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.connection_manager.delete_connection(name)
        except Exception as e:
            return {"success": False, "error": f"删除 SSH 连接失败: {str(e)}"}
    
    def get_ssh_connection(self, name: str) -> Dict[str, Any]:
        """
        获取指定连接的信息
        
        :param name: 连接名称
        :return: 包含连接信息的字典
        """
        try:
            return self.handler.connection_manager.get_connection(name)
        except Exception as e:
            return {"success": False, "error": f"获取 SSH 连接信息失败: {str(e)}"}
    
    def ssh_connect(self, host: str, username: str, password: str, port: int = 22, timeout: int = 10) -> Dict[str, Any]:
        """
        连接到 SSH 服务器
        
        :param host: 主机 IP 地址或域名
        :param username: 用户名
        :param password: 密码
        :param port: SSH 端口，默认 22
        :param timeout: 连接超时时间（秒），默认 10
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.connect(host, username, password, port, timeout)
        except Exception as e:
            return {"success": False, "error": f"SSH 连接失败: {str(e)}"}
    
    def ssh_connect_by_name(self, name: str, timeout: int = 10) -> Dict[str, Any]:
        """
        使用已保存的连接名称连接到 SSH 服务器
        
        :param name: 连接名称
        :param timeout: 连接超时时间（秒），默认 10
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.connect_by_name(name, timeout)
        except Exception as e:
            return {"success": False, "error": f"通过名称连接失败: {str(e)}"}
    
    def ssh_execute_command(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """
        在已连接的 SSH 服务器上执行命令
        
        :param command: 要执行的命令
        :param timeout: 命令执行超时时间（秒），默认 30
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.execute_command(command, timeout)
        except Exception as e:
            return {"success": False, "error": f"执行 SSH 命令失败: {str(e)}"}
    
    def ssh_disconnect(self) -> Dict[str, Any]:
        """
        断开 SSH 连接
        
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.disconnect()
        except Exception as e:
            return {"success": False, "error": f"断开 SSH 连接失败: {str(e)}"}
    
    def ssh_check_connection(self) -> Dict[str, Any]:
        """
        检查 SSH 连接状态
        
        :return: 包含连接状态的字典
        """
        try:
            return self.handler.check_connection()
        except Exception as e:
            return {"success": False, "error": f"检查 SSH 连接状态失败: {str(e)}"}
    
    def ssh_upload_file_sftp(self, local_path: str, remote_path: str, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 SFTP 上传文件到远程服务器
        
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param timeout: 上传超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.upload_file_sftp(local_path, remote_path, timeout)
        except Exception as e:
            return {"success": False, "error": f"SFTP 上传文件失败: {str(e)}"}
    
    def ssh_upload_file_scp(self, local_path: str, remote_path: str, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 SCP 上传文件到远程服务器
        
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param timeout: 上传超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.upload_file_scp(local_path, remote_path, timeout)
        except Exception as e:
            return {"success": False, "error": f"SCP 上传文件失败: {str(e)}"}
    
    def ssh_upload_file_ftp(self, ftp_host: str, ftp_username: str, ftp_password: str, 
                            local_path: str, remote_path: str, port: int = 21, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 FTP 上传文件到远程服务器
        
        :param ftp_host: FTP 服务器地址
        :param ftp_username: FTP 用户名
        :param ftp_password: FTP 密码
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param port: FTP 端口，默认 21
        :param timeout: 上传超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.upload_file_ftp(ftp_host, ftp_username, ftp_password, local_path, remote_path, port, timeout)
        except Exception as e:
            return {"success": False, "error": f"FTP 上传文件失败: {str(e)}"}
    
    def _get_file_proxy(self) -> SshFileProxy:
        """
        获取远程文件代理实例
        
        :return: SshFileProxy 实例
        """
        return SshFileProxy(self.handler)
    
    def ssh_read_remote_file(self, remote_path: str, encoding: str = 'utf-8') -> Dict[str, Any]:
        """
        读取远程文件内容
        
        :param remote_path: 远程文件路径
        :param encoding: 文件编码，默认 utf-8
        :return: 包含文件内容的字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.read_file(remote_path, encoding)
        except Exception as e:
            return {"success": False, "error": f"读取远程文件失败: {str(e)}"}
    
    def ssh_write_remote_file(self, remote_path: str, content: str, encoding: str = 'utf-8', 
                              backup: bool = True, chunk_size: int = None) -> Dict[str, Any]:
        """
        写入内容到远程文件（支持分块写入）
        
        :param remote_path: 远程文件路径
        :param content: 要写入的内容
        :param encoding: 文件编码，默认 utf-8
        :param backup: 是否备份原文件，默认 True
        :param chunk_size: 分块大小（字节），默认 1024（1KB），设置为 None 使用默认值
        :return: 写入结果字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.write_file(remote_path, content, encoding, backup, chunk_size)
        except Exception as e:
            return {"success": False, "error": f"写入远程文件失败: {str(e)}"}
    
    def ssh_edit_remote_file(self, remote_path: str, operations: List[Dict[str, Any]], 
                             encoding: str = 'utf-8', backup: bool = True, chunk_size: int = None) -> Dict[str, Any]:
        """
        编辑远程文件（支持批量操作，支持分块写入）
        
        :param remote_path: 远程文件路径
        :param operations: 操作列表，支持的操作类型：
            - {'type': 'replace', 'old': 'old_text', 'new': 'new_text'}
            - {'type': 'insert', 'line': line_num, 'content': 'content'}
            - {'type': 'delete', 'pattern': 'pattern'}
        :param encoding: 文件编码
        :param backup: 是否备份原文件
        :param chunk_size: 分块大小（字节），默认 1024（1KB），设置为 None 使用默认值
        :return: 编辑结果字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.edit_file(remote_path, operations, encoding, backup, chunk_size)
        except Exception as e:
            return {"success": False, "error": f"编辑远程文件失败: {str(e)}"}
    
    def ssh_append_remote_file(self, remote_path: str, content: str, encoding: str = 'utf-8', 
                                chunk_size: int = None) -> Dict[str, Any]:
        """
        追加内容到远程文件（支持分块写入）
        
        :param remote_path: 远程文件路径
        :param content: 要追加的内容
        :param encoding: 文件编码
        :param chunk_size: 分块大小（字节），默认 1024（1KB），设置为 None 使用默认值
        :return: 追加结果字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.append_file(remote_path, content, encoding, chunk_size)
        except Exception as e:
            return {"success": False, "error": f"追加内容到远程文件失败: {str(e)}"}
    
    def ssh_remote_file_exists(self, remote_path: str) -> Dict[str, Any]:
        """
        检查远程文件是否存在
        
        :param remote_path: 远程文件路径
        :return: 检查结果字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.file_exists(remote_path)
        except Exception as e:
            return {"success": False, "error": f"检查远程文件失败: {str(e)}"}
    
    def ssh_delete_remote_file(self, remote_path: str, backup: bool = True) -> Dict[str, Any]:
        """
        删除远程文件
        
        :param remote_path: 远程文件路径
        :param backup: 是否备份原文件
        :return: 删除结果字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.delete_file(remote_path, backup)
        except Exception as e:
            return {"success": False, "error": f"删除远程文件失败: {str(e)}"}
    
    def ssh_list_remote_directory(self, remote_path: str = ".") -> Dict[str, Any]:
        """
        列出远程目录内容
        
        :param remote_path: 远程目录路径，默认当前目录
        :return: 目录内容列表
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.list_directory(remote_path)
        except Exception as e:
            return {"success": False, "error": f"列出远程目录失败: {str(e)}"}
    
    def ssh_download_file_sftp(self, remote_path: str, local_path: str, timeout: int = 300) -> Dict[str, Any]:
        """
        使用 SFTP 从远程服务器下载文件
        
        :param remote_path: 远程文件路径
        :param local_path: 本地文件路径
        :param timeout: 下载超时时间（秒），默认 300
        :return: 包含成功状态和结果的字典
        """
        try:
            return self.handler.download_file_sftp(remote_path, local_path, timeout)
        except Exception as e:
            return {"success": False, "error": f"SFTP 下载文件失败: {str(e)}"}
    
    def ssh_invoke_local_tool(self, remote_path: str, local_tool_handler: Callable, 
                               tool_args: Dict[str, Any] = None, 
                               sync_back: bool = True,
                               backup: bool = True,
                               encoding: str = 'utf-8') -> Dict[str, Any]:
        """
        调用本地工具处理远程文件
        
        工作流程：
        1. 下载远程文件到本地临时目录
        2. 调用本地工具处理该文件
        3. （可选）将处理后的文件上传回远程服务器
        
        :param remote_path: 远程文件路径
        :param local_tool_handler: 本地工具处理函数，签名为 (local_file_path: str, **kwargs) -> Dict[str, Any]
                                   返回值应包含 'success' 和可选的 'modified' (是否修改了文件)
        :param tool_args: 传递给本地工具的额外参数
        :param sync_back: 处理完成后是否同步回远程服务器，默认 True
        :param backup: 同步回远程时是否备份原文件，默认 True
        :param encoding: 文件编码，默认 utf-8
        :return: 处理结果字典
        
        使用示例:
            def my_formatter(local_path: str, **kwargs) -> Dict[str, Any]:
                # 使用本地工具处理文件
                import subprocess
                result = subprocess.run(['black', local_path], capture_output=True)
                return {
                    "success": result.returncode == 0,
                    "modified": True,
                    "output": result.stdout.decode()
                }
            
            manager.ssh_invoke_local_tool(
                remote_path="/remote/project/main.py",
                local_tool_handler=my_formatter,
                sync_back=True
            )
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.invoke_local_tool(
                    remote_path=remote_path,
                    local_tool_handler=local_tool_handler,
                    tool_args=tool_args,
                    sync_back=sync_back,
                    backup=backup,
                    encoding=encoding
                )
        except Exception as e:
            return {"success": False, "error": f"调用本地工具失败: {str(e)}"}
    
    def ssh_invoke_local_tool_batch(self, remote_files: List[str], local_tool_handler: Callable,
                                     tool_args: Dict[str, Any] = None,
                                     sync_back: bool = True,
                                     backup: bool = True,
                                     encoding: str = 'utf-8',
                                     stop_on_error: bool = False) -> Dict[str, Any]:
        """
        批量调用本地工具处理多个远程文件
        
        :param remote_files: 远程文件路径列表
        :param local_tool_handler: 本地工具处理函数
        :param tool_args: 传递给本地工具的额外参数
        :param sync_back: 处理完成后是否同步回远程服务器
        :param backup: 同步回远程时是否备份原文件
        :param encoding: 文件编码
        :param stop_on_error: 遇到错误时是否停止处理后续文件
        :return: 批量处理结果字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.invoke_local_tool_batch(
                    remote_files=remote_files,
                    local_tool_handler=local_tool_handler,
                    tool_args=tool_args,
                    sync_back=sync_back,
                    backup=backup,
                    encoding=encoding,
                    stop_on_error=stop_on_error
                )
        except Exception as e:
            return {"success": False, "error": f"批量调用本地工具失败: {str(e)}"}
    
    def ssh_invoke_local_tool_with_content(self, remote_path: str, content_processor: Callable[[str], str],
                                           encoding: str = 'utf-8',
                                           backup: bool = True) -> Dict[str, Any]:
        """
        使用内容处理函数处理远程文件内容（简化版，直接处理文本内容）
        
        :param remote_path: 远程文件路径
        :param content_processor: 内容处理函数，签名为 (content: str) -> str
                                  接收文件内容，返回处理后的内容
        :param encoding: 文件编码
        :param backup: 是否备份原文件
        :return: 处理结果字典
        
        使用示例:
            def add_header(content: str) -> str:
                header = "# Auto-generated header\\n"
                return header + content
            
            manager.ssh_invoke_local_tool_with_content(
                remote_path="/remote/project/main.py",
                content_processor=add_header
            )
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.invoke_local_tool_with_content(
                    remote_path=remote_path,
                    content_processor=content_processor,
                    encoding=encoding,
                    backup=backup
                )
        except Exception as e:
            return {"success": False, "error": f"内容处理失败: {str(e)}"}
    
    def ssh_get_local_copy(self, remote_path: str) -> Dict[str, Any]:
        """
        获取远程文件的本地副本路径（仅下载，不处理）
        
        用于需要手动处理或使用其他本地工具的场景
        
        :param remote_path: 远程文件路径
        :return: 包含本地文件路径的字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.get_local_copy(remote_path)
        except Exception as e:
            return {"success": False, "error": f"获取本地副本失败: {str(e)}"}
    
    def ssh_push_local_copy(self, local_path: str, remote_path: str, backup: bool = True) -> Dict[str, Any]:
        """
        将本地文件推送到远程（仅上传，用于手动处理后的同步）
        
        配合 ssh_get_local_copy 使用，实现手动处理流程：
        1. ssh_get_local_copy 获取本地副本
        2. 手动处理本地文件
        3. ssh_push_local_copy 推送回远程
        
        :param local_path: 本地文件路径
        :param remote_path: 远程文件路径
        :param backup: 是否备份远程原文件
        :return: 上传结果字典
        """
        try:
            with self._get_file_proxy() as proxy:
                return proxy.push_local_copy(local_path, remote_path, backup)
        except Exception as e:
            return {"success": False, "error": f"推送本地文件失败: {str(e)}"}


def create_ssh_tool_manager():
    """
    创建 SSH 工具管理器实例
    """
    return SshToolManager()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "缺少参数"}, ensure_ascii=False))
        sys.exit(1)
    
    action = sys.argv[1]
    manager = SshToolManager()
    
    try:
        if action == "save":
            if len(sys.argv) < 6:
                print(json.dumps({"success": False, "error": "缺少参数"}, ensure_ascii=False))
                sys.exit(1)
            
            name = sys.argv[2]
            host = sys.argv[3]
            username = sys.argv[4]
            password = sys.argv[5]
            port = int(sys.argv[6]) if len(sys.argv) > 6 else 22
            
            result = manager.save_ssh_connection(name, host, username, password, port)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        elif action == "list":
            result = manager.list_ssh_connections()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        elif action == "delete":
            if len(sys.argv) < 3:
                print(json.dumps({"success": False, "error": "缺少连接名称参数"}, ensure_ascii=False))
                sys.exit(1)
            
            name = sys.argv[2]
            result = manager.delete_ssh_connection(name)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        elif action == "connect":
            if len(sys.argv) < 5:
                print(json.dumps({"success": False, "error": "缺少参数"}, ensure_ascii=False))
                sys.exit(1)
            
            host = sys.argv[2]
            username = sys.argv[3]
            password = sys.argv[4]
            port = int(sys.argv[5]) if len(sys.argv) > 5 else 22
            timeout = int(sys.argv[6]) if len(sys.argv) > 6 else 10
            
            result = manager.ssh_connect(host, username, password, port, timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        elif action == "connect_by_name":
            if len(sys.argv) < 3:
                print(json.dumps({"success": False, "error": "缺少连接名称参数"}, ensure_ascii=False))
                sys.exit(1)
            
            name = sys.argv[2]
            timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 10
            
            result = manager.ssh_connect_by_name(name, timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        elif action == "execute":
            if len(sys.argv) < 3:
                print(json.dumps({"success": False, "error": "缺少命令参数"}, ensure_ascii=False))
                sys.exit(1)
            
            command = sys.argv[2]
            timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 30
            
            result = manager.ssh_execute_command(command, timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        elif action == "disconnect":
            result = manager.ssh_disconnect()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        elif action == "check":
            result = manager.ssh_check_connection()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        
        else:
            print(json.dumps({"success": False, "error": f"未知操作: {action}"}, ensure_ascii=False))
            sys.exit(1)
    
    except Exception as e:
        print(json.dumps({"success": False, "error": f"执行时发生错误: {str(e)}"}, ensure_ascii=False))
        sys.exit(1)
