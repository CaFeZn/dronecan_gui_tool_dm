#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
dmsdk（达妙 dmcan）**可选依赖的唯一入口**。

全工程只有本模块 `import dmcan`，把"可选"收在一个点上：

* 没装 `dmcan_sdk` Python 包  -> `load()` 抛 `SdkUnavailable`，GUI 照常启动，
  只是接口列表里不会出现 `dmcan:` 项；控制台与 DM 面板给出可直接照做的提示。
* 装了包但找不到 `libdmcan` 原生库 -> 同样只影响 dmcan 后端。
* 两者都在 -> `probe()` 返回 `available=True`，并给出库路径与库版本。

原生库查找顺序（前三项是本 fork 补的，方便本地免配置）：

1. 环境变量 `DMCAN_LIBRARY_PATH`（给**完整文件路径**）；
2. 程序目录 / 包目录下的 `dlls/`（`tools/fetch_dmcan_lib.py` 会往这里放）；
3. `dmcan` 包内自带的 `dmcan/dlls/`；
4. 交给系统动态库搜索路径。

@note Windows 上 `libdmcan.dll` 依赖 `libusb-1.0.dll` / `libstdc++-6.dll` /
      `libgcc_s_seh-1.dll` / `libwinpthread-1.dll`，这几个必须与它**同目录**；
      `tools/fetch_dmcan_lib.py` 会一起拷。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ['ENV_LIBRARY_PATH', 'SdkUnavailable', 'SdkStatus', 'load', 'probe',
           'get_context', 'close_context', 'resolve_library_path', 'import_error']

#: 指到 `libdmcan` 完整路径的环境变量（与 dmcan_sdk 的约定一致）。
ENV_LIBRARY_PATH = 'DMCAN_LIBRARY_PATH'

#: SDK 终端日志级别（`dmcan_set_log_level` 的取值）。
LOG_LEVELS = {'trace': 0, 'debug': 1, 'info': 2, 'warn': 3, 'error': 4, 'critical': 5, 'off': 6}

#: 离线安装提示（本仓库不转发 dmcan_sdk，指向 SDK 仓库里的 wheel）。
INSTALL_HINT = ('pip install dmcan_sdk；离线时用 '
                'dm-device-sdk/Python/pack/dmcan_sdk-2.1.1-py3-none-any.whl')

LIBRARY_NAME_HINT = ('设置 DMCAN_LIBRARY_PATH 指向 libdmcan 完整路径，'
                     '或运行 tools/fetch_dmcan_lib.py 把原生库拷到本工程的 dlls/')

#: Windows 上 `libdmcan.dll` 的运行期依赖，**必须与它同目录**。
#: 顺序 = 依赖顺序（winpthread -> gcc -> stdc++ -> usb），先加载被依赖的。
WINDOWS_DEPENDENCIES = ('libwinpthread-1.dll', 'libgcc_s_seh-1.dll',
                        'libstdc++-6.dll', 'libusb-1.0.dll')

#: 已经预加载过的依赖：文件名 -> 实际落盘的路径（用来诊断"用错了哪一份"）。
loaded_dependencies: Dict[str, str] = {}


class SdkUnavailable(RuntimeError):
    """dmsdk 不可用：Python 包缺失、原生库找不到或加载失败。"""


@dataclass
class SdkStatus:
    """dmsdk 可用性快照（GUI/CLI 拿它渲染人读提示，不抛异常）。"""

    available: bool = False
    #: 不可用原因（中文，已带上具体的缺失项）
    reason: str = ''
    #: 排错建议（中文，可直接照做）
    hint: str = ''
    #: `dmcan_sdk` 包版本；没装时为空
    package_version: str = ''
    #: 实际加载的 libdmcan 路径
    lib_path: str = ''
    #: 库自报的版本字符串
    sdk_version: str = ''

    def describe(self) -> str:
        """@brief 一行式人读描述，用于日志与面板标题。"""
        if self.available:
            return 'dmsdk 可用（libdmcan %s：%s）' % (self.sdk_version or '版本未知', self.lib_path)
        return 'dmsdk 不可用：%s' % (self.reason or '原因未知')


_module: Optional[Any] = None
_module_error: Optional[BaseException] = None
_context: Optional[Any] = None
_status_cache: Optional[SdkStatus] = None


def _library_names() -> tuple:
    """@brief 当前平台要找的原生库文件名（按顺序尝试）。"""
    if sys.platform == 'win32':
        return ('libdmcan.dll', 'dmcan.dll')
    if sys.platform == 'darwin':
        return ('libdmcan.dylib',)
    return ('libdmcan.so',)


def _candidate_dirs() -> List[str]:
    """
    @brief 本项目补的查找目录：程序目录与包目录下的 `dlls/`。

    @note 打包成 exe（PyInstaller）后 `__file__` 指向临时解包目录，用户真正会去放
          `libdmcan` 的地方是 **exe 旁边**（`<exe目录>/dlls/`），所以冻结时要优先看那里。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    package_root = os.path.dirname(here)
    app_root = os.path.dirname(package_root)
    ordered: List[str] = []
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        unpack_dir = getattr(sys, '_MEIPASS', '')
        ordered += [os.path.join(exe_dir, 'dlls'), exe_dir,
                    os.path.join(unpack_dir, 'dlls') if unpack_dir else '', unpack_dir]
    ordered += [os.path.join(app_root, 'dlls'),
                os.path.join(package_root, 'dlls'),
                here, app_root,
                os.path.join(os.getcwd(), 'dlls'), os.getcwd()]
    out: List[str] = []
    for directory in ordered:
        if directory and directory not in out and os.path.isdir(directory):
            out.append(directory)
    return out


def resolve_library_path() -> Optional[str]:
    """
    @brief 找 `libdmcan` 的完整路径。

    @return 找到则返回绝对路径；一处都没有返回 `None`（此时交给 dmcan_sdk 自身的
            查找顺序与系统搜索路径处理）
    """
    override = os.environ.get(ENV_LIBRARY_PATH)
    if override:
        return override
    for directory in _candidate_dirs():
        for name in _library_names():
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def import_error() -> Optional[BaseException]:
    """@brief 最近一次 import dmcan 失败的原因（没试过或成功时为 None）。"""
    return _module_error


def load():
    """
    @brief 导入并缓存 `dmcan` 模块。

    @return `dmcan` 模块对象
    @raise SdkUnavailable 没装 `dmcan_sdk`（原因里带上原始异常）
    """
    global _module, _module_error
    if _module is not None:
        return _module
    try:
        import dmcan  # noqa: F401  （故意放在函数里：可选依赖不能影响任何其他后端）
    except Exception as ex:  # pragma: no cover - 依赖环境
        _module_error = ex
        raise SdkUnavailable('未安装 dmcan_sdk Python 包（%s: %s）' % (type(ex).__name__, ex)) from ex
    _module = dmcan
    return dmcan


def _package_version() -> str:
    """@brief `dmcan_sdk` 的包版本；读不到返回空串。"""
    try:
        import importlib.metadata as md
        return md.version('dmcan_sdk')
    except Exception:
        try:
            dmcan = load()
            return str(getattr(dmcan, '__version__', '') or '')
        except Exception:
            return ''


def _module_file_path(module) -> str:
    """@brief 取一个已加载 DLL 的真实落盘路径（`GetModuleFileNameW`）。"""
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        buffer = ctypes.create_unicode_buffer(2048)
        written = kernel32.GetModuleFileNameW(ctypes.c_void_p(module._handle), buffer, 2048)
        return buffer.value if written else ''
    except Exception:  # pragma: no cover - 依赖环境
        return ''


def preload_dependencies(library_path: Optional[str]) -> Dict[str, str]:
    """
    @brief 在加载 `libdmcan` 之前，按**绝对路径**预加载它同目录的依赖 DLL。

    为什么必须这么做：dmcan_sdk 用 `CDLL(path, winmode=0)` 加载原生库，
    这个标志走的是 Windows 的**标准搜索顺序**——依赖 DLL 只会从"exe 所在目录 /
    System32 / 当前目录 / PATH"里找，**不会**从 `libdmcan.dll` 自己的目录找。
    于是同一台机器上 PATH 里只要有一份版本不对的 `libstdc++-6.dll` /
    `libusb-1.0.dll`（装了 ST-Link、QEMU、MSYS2、OpenOCD 的机器基本都有），
    就会报 `WinError 127 找不到指定的程序`（找到了文件但缺导出符号）。

    先按绝对路径把正确的几份装进进程模块表，之后 `libdmcan.dll` 按名字引用它们时
    会直接命中已加载的那一份，结果与 PATH 无关。

    @param library_path `libdmcan` 的完整路径；None 时不做任何事
    @return 文件名 -> 实际加载的路径
    @raise SdkUnavailable 某个依赖存在但加载失败（版本损坏/位数不匹配）
    """
    loaded_dependencies.clear()
    if sys.platform != 'win32' or not library_path:
        return {}
    directory = os.path.dirname(os.path.abspath(library_path))
    for name in WINDOWS_DEPENDENCIES:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            logger.debug('依赖 %s 不在 %s（交给系统搜索路径）', name, directory)
            continue
        try:
            module = ctypes.WinDLL(path)
        except OSError as ex:
            raise SdkUnavailable('依赖 DLL 加载失败：%s（%s）：%s' % (name, path, ex)) from ex
        resolved = _module_file_path(module) or path
        loaded_dependencies[name] = resolved
        if os.path.normcase(os.path.abspath(resolved)) != os.path.normcase(os.path.abspath(path)):
            # 系统里已有同名模块（先被别的组件加载了），这一份用不上
            logger.warning('依赖 %s 用的是进程里已有的 %s（不是 %s）', name, resolved, path)
        else:
            logger.debug('依赖 %s 已从 %s 预加载', name, resolved)
    return dict(loaded_dependencies)


def dependency_report() -> str:
    """@brief 依赖 DLL 的解析结果（一行一条），用于 doctor 与报错。"""
    if not loaded_dependencies:
        return '（未预加载依赖；可能是非 Windows，或还没成功定位 libdmcan）'
    lines = []
    for name, path in loaded_dependencies.items():
        lines.append('%s -> %s' % (name, path))
    return '\n    '.join(lines)


def get_context(refresh: bool = False):
    """
    @brief 取**进程内唯一的** `DmCanContext`（设备枚举与所有会话共用）。

    只创建一个上下文是有意为之：多个上下文会各自持有 libusb 状态，容易在
    "枚举 vs 正在收发"之间互相干扰；这里一个进程一个上下文，设备句柄由它持有。

    @param refresh 为 True 时即使已有上下文也不重建（保留句柄），仅在还没有时创建
    @return `dmcan.DmCanContext`
    @raise SdkUnavailable 包缺失、原生库找不到或加载失败
    """
    global _context
    if _context is not None:
        return _context
    dmcan = load()
    path = resolve_library_path()
    # 先把同目录的依赖按绝对路径装进进程，避免被 PATH 里的同名 DLL 顶掉（见 preload_dependencies）
    preload_dependencies(path)
    try:
        _context = dmcan.DmCanContext(path) if path else dmcan.DmCanContext()
    except Exception as ex:
        where = path or '（自动查找：包内 dlls/ -> 系统路径）'
        detail = ''
        if sys.platform == 'win32':
            detail = ('\n    WinError 127/126 多半是依赖 DLL 的问题——'
                      '需要的 4 个 DLL 必须与 libdmcan 同目录且版本配套，'
                      '而 PATH 里其它软件（ST-Link/QEMU/MSYS2/OpenOCD 等）的同名 DLL 会干扰解析。'
                      '\n    当前依赖解析：\n    %s' % dependency_report())
        raise SdkUnavailable('libdmcan 原生库加载失败（%s）：%s: %s%s'
                             % (where, type(ex).__name__, ex, detail)) from ex
    # SDK 自己的终端日志默认是 info，会把 USB 细节刷到 stdout；这里默认压到 warn。
    # 用户显式设了 DMCAN_LOG_LEVEL 就尊重用户（C 侧初始化时已经读过它）。
    if not os.environ.get('DMCAN_LOG_LEVEL'):
        try:
            _context.set_log_level(LOG_LEVELS['warn'])
        except Exception as ex:  # pragma: no cover - 依赖环境
            logger.debug('设置 SDK 日志级别失败：%s', ex)
    logger.info('dmsdk 就绪：%s（库 %s）', _context.dll_path, _context.sdk_version())
    return _context


def close_context() -> None:
    """@brief 销毁进程内唯一的上下文（CLI 退出、测试收尾用）。"""
    global _context, _status_cache
    if _context is not None:
        try:
            _context.destroy()
        except Exception:
            logger.warning('销毁 dmcan 上下文失败', exc_info=True)
        _context = None
    _status_cache = None


def set_log_level(level) -> None:
    """
    @brief 设置 SDK 自身的终端日志级别（`dmcan_set_log_level`）。

    @param level 级别名（见 `LOG_LEVELS`）或整数
    @note 默认在 `get_context()` 里压到 `warn`；用户设了 `DMCAN_LOG_LEVEL` 环境变量时不覆盖。
    """
    value = LOG_LEVELS.get(str(level).lower(), level) if not isinstance(level, int) else level
    try:
        get_context().set_log_level(int(value))
    except Exception as ex:  # pragma: no cover - 依赖环境
        logger.debug('设置 SDK 日志级别失败：%s', ex)


def reset() -> None:
    """
    @brief 忘掉缓存的模块、上下文与探测结果。

    @note 给测试与"用户点了重新探测"用；正常运行时不需要调。
    """
    global _module, _module_error, _status_cache
    close_context()
    _module = None
    _module_error = None
    _status_cache = None


def probe(refresh: bool = False) -> SdkStatus:
    """
    @brief 探测 dmsdk 是否可用（**不抛异常**，结果带原因与建议）。

    @param refresh 忽略缓存重新探测
    @return `SdkStatus`
    """
    global _status_cache
    if _status_cache is not None and not refresh:
        return _status_cache

    status = SdkStatus()
    try:
        load()
    except SdkUnavailable as ex:
        status.reason = str(ex)
        status.hint = INSTALL_HINT
        _status_cache = status
        return status

    status.package_version = _package_version()
    status.lib_path = resolve_library_path() or ''
    try:
        context = get_context()
    except SdkUnavailable as ex:
        status.reason = str(ex)
        status.hint = LIBRARY_NAME_HINT
        _status_cache = status
        return status

    try:
        status.sdk_version = context.sdk_version()
        status.lib_path = context.dll_path
    except Exception as ex:  # pragma: no cover - 依赖环境
        logger.warning('读取 dmsdk 版本失败：%s', ex)
    status.available = True
    _status_cache = status
    return status


def log_status() -> SdkStatus:
    """@brief 探测并打一条日志（GUI/CLI 启动时调用），返回状态。"""
    status = probe()
    if status.available:
        logger.info('%s', status.describe())
    else:
        logger.info('%s（%s）', status.describe(), status.hint)
    return status
