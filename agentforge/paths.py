"""安全地解析工作目录内的路径。

文件工具和评测器都需要同一套边界规则。单纯拼接字符串再调用
``realpath`` 容易在创建新文件、处理符号链接或遇到 Windows 驱动器路径时
出现不一致，因此所有调用方都通过本模块解析路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PathResult:
    """路径解析结果，``path`` 为空表示拒绝。"""

    path: str | None
    reason: str | None = None


def _inside(base: str, target: str) -> bool:
    return target == base or target.startswith(base + os.sep)


def _existing_components(base: str, target: str):
    """返回从 base 到 target 的已存在组件（含 target，如果它存在）。"""
    current = base
    try:
        relative = os.path.relpath(target, base)
    except ValueError:
        return
    if relative == os.curdir:
        yield current
        return
    for part in relative.split(os.sep):
        if part in ("", os.curdir):
            continue
        current = os.path.join(current, part)
        if not os.path.lexists(current):
            break
        yield current


def resolve_path(
    workdir: str,
    relpath: str | None,
    *,
    allow_missing: bool = True,
    reject_symlink: bool = True,
    reject_hardlink: bool = False,
) -> PathResult:
    """将相对路径解析到 workdir 内并执行边界检查。

    ``allow_missing`` 允许写入尚不存在的最终文件，但其已存在的父目录仍
    必须通过检查。拒绝符号链接组件可以避免检查后再跟随链接的 TOCTOU
    绕过；对已存在文件还可以选择拒绝多链接 inode。
    """
    if relpath is None or relpath == "":
        relpath = "."
    if not isinstance(relpath, str):
        return PathResult(None, "path must be a string")
    if "\x00" in relpath:
        return PathResult(None, "path contains NUL")

    base = os.path.realpath(os.path.abspath(workdir))
    if not os.path.isdir(base):
        return PathResult(None, "workdir is not a directory")
    if os.path.isabs(relpath):
        return PathResult(None, "absolute paths are not allowed")

    lexical = os.path.abspath(os.path.join(base, relpath))
    if not _inside(base, lexical):
        return PathResult(None, "path escapes workdir")
    target = os.path.realpath(lexical)
    if not _inside(base, target):
        return PathResult(None, "path escapes workdir")

    if not allow_missing and not os.path.exists(lexical):
        return PathResult(None, "path does not exist")

    if reject_symlink:
        for component in _existing_components(base, lexical):
            if os.path.islink(component):
                return PathResult(None, "symlink paths are not allowed")

    if reject_hardlink and os.path.isfile(lexical):
        try:
            if os.stat(lexical, follow_symlinks=False).st_nlink > 1:
                return PathResult(None, "hard-linked files are not allowed")
        except OSError:
            return PathResult(None, "cannot stat path")

    if not _inside(base, target):
        return PathResult(None, "path escapes workdir")
    return PathResult(target)


def is_safe_existing_file(workdir: str, relpath: str) -> bool:
    result = resolve_path(
        workdir,
        relpath,
        allow_missing=False,
        reject_symlink=True,
        reject_hardlink=True,
    )
    return bool(result.path and os.path.isfile(result.path))
