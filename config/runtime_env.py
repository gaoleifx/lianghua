# -*- coding: utf-8 -*-
"""读取运行凭据；兼容环境变量在掘金进程启动后才设置的情况。"""
import atexit
import json
import os
from datetime import datetime
from pathlib import Path


_INSTANCE_HANDLES = {}
_PID_FILES = {}


def user_environment(name, default=""):
    value = os.environ.get(name)
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
                return os.path.expandvars(str(value))
        except (OSError, ImportError):
            pass
    return default


def _automation_pid_directory():
    configured = os.environ.get("GOLDMINER_AUTOMATION_STATE_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "logs" / "automation_runtime"


def _remove_owned_pid_file(role, pid_path):
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
        if int(payload.get("pid", -1)) == os.getpid():
            pid_path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    _PID_FILES.pop(str(role), None)


def acquire_single_instance(role):
    """Keep exactly one local strategy process per trading role on Windows."""
    if os.name != "nt":
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex_name = "Local\\GoldMinerStrategy_" + str(role)
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法创建策略单实例锁")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise RuntimeError("已有同角色策略进程运行，拒绝重复启动: " + str(role))
    role = str(role)
    _INSTANCE_HANDLES[role] = handle
    pid_directory = _automation_pid_directory()
    pid_directory.mkdir(parents=True, exist_ok=True)
    pid_path = pid_directory / (role + ".pid.json")
    temp_path = pid_path.with_suffix(pid_path.suffix + ".tmp")
    payload = {
        "role": role,
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
    }
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, pid_path)
    _PID_FILES[role] = pid_path
    atexit.register(_remove_owned_pid_file, role, pid_path)
