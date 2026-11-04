"""在 backend 目录运行 python -m scripts.backfill_confirmed_index_sources [--apply]。

使用已配置的环境变量；部署前先保护旧任务来源并安排维护窗口。
"""

import argparse
import asyncio
import json
from types import SimpleNamespace


def _build_runtime():
    # 只装配回填需要的仓储，不导入 bootstrap/main，不启动清理或 Worker。
    from services.persistence.document_lifecycle import DocumentLifecycleStore
    from services.persistence.repositories.runtime_repository import PostgresStateStore
    from services.storage.minio.minio_storage import MinioUploadService

    lifecycle = DocumentLifecycleStore()
    return SimpleNamespace(
        require_lifecycle=lambda: lifecycle,
        task_store=PostgresStateStore(service="supervisor"),
        storage_service=MinioUploadService(),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="核验版本确认来源；默认 dry-run，--apply 才写版本引用")
    parser.add_argument("--apply", action="store_true", help="显式保存核验通过的版本引用")
    args = parser.parse_args(argv)
    from services.application.index_recovery_sources import backfill_confirmed_sources

    runtime = _build_runtime()
    try:
        report = asyncio.run(backfill_confirmed_sources(runtime, apply=args.apply))
    finally:
        runtime.require_lifecycle().engine.dispose()
        runtime.task_store.engine.dispose()
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report}, ensure_ascii=False))
    return 1 if report["rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
