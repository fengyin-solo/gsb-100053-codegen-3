"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

仓库提供两类能力：
- 业务表（``_tables``）：支持事务快照，``with store.transaction()`` 内的任何改动在抛错时整体回滚；
- 过程元数据（``_meta``）：发布检查点一类"中断后还要接着用"的信息不进快照，
  事务回滚不会把它一起抹掉，否则发布中断后就不知道该从哪一版重新取了。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 内部表：阈值版本、规则行不进运营概览的"业务模块"统计
INTERNAL_TABLES = {"quality_versions", "quality_rules"}


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """恢复到种子数据状态，供测试之间清场使用。"""
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._meta: dict[str, Any] = {}

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def meta(self, key: str, default: Any = None) -> Any:
        return self._meta.get(key, default)

    def set_meta(self, key: str, value: Any) -> None:
        """写过程元数据：不依附业务事务，业务回滚后检查点仍然在。"""
        if value is None:
            self._meta.pop(key, None)
        else:
            self._meta[key] = value

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """业务事务：进入时对全部业务表做深拷贝快照，块内抛错则整体回滚。

        ``_meta`` 不进快照——发布中断要靠里面的检查点续发，不能随失败一起回滚。
        """
        snapshot = copy.deepcopy(self._tables)
        try:
            yield
        except BaseException:
            self._tables = snapshot
            raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
