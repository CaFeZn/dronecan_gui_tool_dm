# -*- coding: utf-8 -*-
"""冻结版（PyInstaller）GUI 入口。

单独一个文件是为了让打包器有个明确的 entry point，内容与原版 `bin/dronecan_gui_tool` 等价；
另外这里兜住启动期的异常并落一份崩溃日志——窗口模式下没有控制台，PyInstaller 只会弹一个
对话框，用户既看不到也拷不出来，写文件才能事后排查。
"""

import multiprocessing
import os
import sys
import traceback


def _write_crash_log(text: str) -> str:
    """
    @brief 把启动期异常写到 exe 旁边的 `dronecan-gui-tool-crash.log`（写不了就退到临时目录）。

    @param text 要写入的内容（含时间戳与完整回溯）
    @return 实际写入的路径（都写不了时返回空串）
    """
    import datetime
    import tempfile

    header = '===== %s =====\n' % datetime.datetime.now().isoformat(timespec='seconds')
    targets = []
    try:
        targets.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                    'dronecan-gui-tool-crash.log'))
    except Exception:
        pass
    targets.append(os.path.join(tempfile.gettempdir(), 'dronecan-gui-tool-crash.log'))
    for path in targets:
        try:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(header)
                handle.write(text)
                handle.write('\n')
            return path
        except Exception:
            continue
    return ''


if __name__ == '__main__':
    # Bus Monitor / Plotter 会 spawn 子进程：冻结后必须在最前面调用它，否则子进程会
    # 反复重新启动整个程序（PyInstaller 的文档要求）。
    multiprocessing.freeze_support()

    try:
        from dronecan_gui_tool.main import main
    except BaseException:
        _write_crash_log(traceback.format_exc())
        raise
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        _write_crash_log(traceback.format_exc())
        raise
