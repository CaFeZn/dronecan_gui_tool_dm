# -*- coding: utf-8 -*-
"""冻结版（PyInstaller）CLI 入口：`dmcan-dronecan-tool`。"""

import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()

    from dronecan_gui_tool.dmcan_cli import main
    sys.exit(main())
