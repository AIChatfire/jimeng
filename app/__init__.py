#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jimeng-service —— 即梦（jimeng.jianying.com）的异步图片生成出口。

对外只有两条契约（见 `docs/INTERFACE.md`）：
  POST /async/v1/images/generations        受理，只回一个 task_id
  GET  /async/v1/images/generations/{id}   非终态回排队态；终态回 {data, created, usage}
"""

__version__ = "0.1.2"
