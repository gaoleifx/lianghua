# -*- coding: utf-8 -*-
"""读取运行凭据；兼容环境变量在掘金进程启动后才设置的情况。"""
import os


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
