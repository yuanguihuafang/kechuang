"""优先协调同一 Backend worker 内的检索与 MySQL 索引同步。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class _MilvusPriorityGate:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active_readers = 0
        self.waiting_readers = 0
        self.active_writer = False
        self.waiting_writers = 0

    @contextmanager
    def search(self) -> Iterator[None]:
        with self.condition:
            self.waiting_readers += 1
            self.condition.notify_all()
            try:
                self.condition.wait_for(lambda: not self.active_writer)
            finally:
                self.waiting_readers -= 1
            self.active_readers += 1
        try:
            yield
        finally:
            with self.condition:
                self.active_readers -= 1
                self.condition.notify_all()

    @contextmanager
    def mysql_sync_write(self) -> Iterator[None]:
        with self.condition:
            self.waiting_writers += 1
            self.condition.notify_all()
            try:
                self.condition.wait_for(
                    lambda: (
                        not self.active_writer
                        and self.active_readers == 0
                        and self.waiting_readers == 0
                    )
                )
            finally:
                self.waiting_writers -= 1
            self.active_writer = True
        try:
            yield
        finally:
            with self.condition:
                self.active_writer = False
                self.condition.notify_all()


_GATES: dict[str, _MilvusPriorityGate] = {}
_GATES_LOCK = threading.Lock()


def _gate_for(key: str) -> _MilvusPriorityGate:
    with _GATES_LOCK:
        gate = _GATES.get(key)
        if gate is None:
            gate = _MilvusPriorityGate()
            _GATES[key] = gate
        return gate


@contextmanager
def milvus_search(key: str) -> Iterator[None]:
    """允许同一集合的检索并行；检索优先于等待中的同步写。"""

    with _gate_for(key).search():
        yield


@contextmanager
def mysql_sync_write(key: str) -> Iterator[None]:
    """在单次同步写 RPC 边界让检索先行，退出后同步自然继续。"""

    with _gate_for(key).mysql_sync_write():
        yield
