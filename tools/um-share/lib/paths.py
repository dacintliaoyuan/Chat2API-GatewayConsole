# -*- coding: utf-8 -*-
"""tools/um-share 的共享路径 helper（只做路径算术，不改变工具语义）。

子包化后，脚本从 ``tools/um-share/<subdir>/<script>.py`` 运行：

* ``script_dir`` = 脚本所在子目录；
* ``um_share_dir`` = ``tools/um-share``；
* ``repo_root`` = 仓库根。

``bootstrap`` 按「脚本目录最前、um-share 其次、仓库根最后」的顺序插入
``sys.path``：同目录工具与 ``lib`` 优先，产品模块（``console`` / ``share`` /
``usagesrv``）仍从仓库根导入。
"""

from __future__ import annotations

import os
import sys


def script_dir(file_path):
    """脚本所在目录（绝对路径）。"""
    return os.path.dirname(os.path.abspath(file_path))


def um_share_dir(file_path):
    """``tools/um-share`` 目录（脚本目录的上一级）。"""
    return os.path.dirname(script_dir(file_path))


def repo_root(file_path):
    """仓库根（``tools/um-share`` 的上两级）。"""
    return os.path.dirname(os.path.dirname(um_share_dir(file_path)))


def legacy_repo_parent(file_path):
    """移动前 ``Path(__file__).resolve().parents[3]`` 指向的目录。

    ``ledger_row_recheck.py`` 的默认账本路径按旧布局写成 ``parents[3]``
    （仓库根的上一级）；移动一层后仍返回同一目录，默认路径保持逐字不变。
    """
    return os.path.dirname(repo_root(file_path))


def ensure_sys_path(*paths):
    """把给定目录插到 ``sys.path`` 前端；已存在则跳过。"""
    for path in paths:
        if path and path not in sys.path:
            sys.path.insert(0, path)


def bootstrap(file_path):
    """插入脚本目录 / um-share 目录 / 仓库根，返回三个绝对路径。"""
    here = script_dir(file_path)
    share = um_share_dir(file_path)
    root = repo_root(file_path)
    ensure_sys_path(root, share, here)
    return {"script": here, "um_share": share, "repo": root}
