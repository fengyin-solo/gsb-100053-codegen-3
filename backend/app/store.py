"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

事务约定（版本生效台发布/重算依赖这层语义）：
- ``transaction()`` 进入时对全表做快照，块内异常则整体回滚到快照，成功才放行；
- 发布主张（claim）这类“中断后也要保留”的进度，必须在独立事务里先行提交，
  不能和生效/重算混在同一个事务里，否则回滚会把恢复点一起抹掉。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._snapshot: dict[str, list[dict[str, Any]]] | None = None

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """支持嵌套的事务：只有最外层持有快照，内层失败向上抛、由最外层整体回滚。

        这样“发布主张/失败游标”可以在各自独立的顶层事务里先提交，
        随后的“生效+重算”事务回滚时不会把已提交的恢复点一起抹掉。
        """
        self._lock.acquire()
        outer = self._tx_depth == 0
        if outer:
            self._snapshot = copy.deepcopy(self._tables)
        self._tx_depth += 1
        try:
            yield
        except BaseException:
            self._tx_depth -= 1
            if outer:
                self._tables = self._snapshot  # type: ignore[assignment]
                self._snapshot = None
            self._lock.release()
            raise
        self._tx_depth -= 1
        if outer:
            self._snapshot = None
        self._lock.release()

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
