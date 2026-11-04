"""从 MySQL 已标注记录生成可重复的检索问题集。"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import pymysql


TABLES = {
    "enterprise": ("xp_enterprise_profile", "enterprise_name"),
    "demand": ("xp_pub_content_simple", "title"),
    "achievement": ("xp_pub_result", "title"),
    "expert": ("xp_expert_library", "user_name"),
}


def text(row: dict[str, Any], *fields: str, limit: int = 120) -> str:
    for field in fields:
        value = str(row.get(field) or "").strip()
        if value:
            value = re.sub(r"\\[nr]", " ", value)
            value = re.sub(r"\s+", " ", value)
            value = re.sub(r"[\[\]{}\"']", "", value)
            return value[:limit].rstrip(" ，,：:。；;、和与及的")
    return ""


def source_id(case: dict[str, Any]) -> str:
    match = re.search(r"\|id:([^|]+)", case["expected_source_contains"][0])
    if not match:
        raise ValueError(f"无法解析标注来源: {case}")
    return match.group(1)


def build_query(kind: str, row: dict[str, Any], index: int) -> str:
    if kind == "enterprise":
        name = text(row, "enterprise_name") or "这家企业"
        desc = text(row, "business_scope")
        desc = re.sub(re.escape(name), "", desc).strip(" ，,：:。")
        if len(desc) >= 20:
            variants = (
                f"请寻找具备以下业务能力的企业：{desc}。",
                f"我想找一家能够开展以下业务的企业：{desc}。",
            )
        else:
            variants = (
                f"请介绍企业“{name}”的主营业务、产品和技术能力。",
                f"我想了解企业“{name}”能提供哪些产品和技术服务。",
            )
    elif kind == "demand":
        title = text(row, "title") or "这项需求"
        detail = text(row, "contect", "analyse_contect")
        product = text(row, "product", "product_names")
        application = text(row, "application", "scene_lable")
        variants = (
            title,
            f"{title}的具体技术需求是什么？",
            f"我们想找{product or title}相关的解决方案，需求描述是：{detail}" if detail else f"寻找{title}相关解决方案",
            f"{application or '相关应用场景'}需要解决什么问题？{title}",
            f"请匹配能满足“{title}”的技术、产品或服务。",
        )
    elif kind == "achievement":
        title = text(row, "title") or "这项成果"
        analysis = text(row, "analyse_contect", "main_function", "main_advantage")
        product = text(row, "product", "product_names")
        company = text(row, "company")
        application = text(row, "application", "aplication_field_scenario", "scene_lable")
        variants = (
            title,
            f"{title}的技术原理和主要优势是什么？",
            f"查找{product or title}相关成果，重点关注：{analysis}" if analysis else f"查找{product or title}相关技术成果",
            f"{company or '哪些单位'}有哪些与{title}相关的成果？",
            f"{application or '哪些应用场景'}可以采用{title}？",
        )
    else:
        field = text(row, "research_field", "technical_category", "label", limit=100)
        application = text(row, "application", "main_product", "technological_achievements", limit=80)
        variants = (
            f"寻找{field or '相关技术'}方向的专家。",
            f"我们遇到{field or '相关技术'}方面的问题，应该找什么方向的专家？",
            f"{application or field or '相关应用场景'}有哪些研究团队或专家？",
            f"需要解决{field or '相关技术'}相关技术问题，推荐合适的专家。",
            f"希望开展{field or '相关技术'}方向的技术合作，请推荐专家。",
        )
    return variants[index % len(variants)].strip()


def build_seed_query(kind: str, seed: str, index: int) -> str:
    """无数据库凭据时，仅用已有标注问题生成多样化表达。"""
    match = re.search(r"[“\"]([^“”\"]+)[”\"]", seed)
    subject = (match.group(1) if match else seed).strip().rstrip("。")
    templates = {
        "enterprise": (
            "请查找企业“{x}”的相关信息。",
            "企业{x}主要从事什么业务，具备哪些技术能力？",
            "我想了解{x}的主营方向、产品和应用行业。",
            "寻找与{x}业务相近或可以合作的企业。",
            "请判断{x}是否适合作为相关技术或产品的合作企业。",
        ),
        "demand": (
            "请查找需求“{x}”的相关资料。",
            "{x}具体要解决什么技术或产品问题？",
            "我们正在寻找能满足“{x}”的技术方案。",
            "有哪些企业、成果或专家可能适合应对{x}？",
            "请推荐与{x}相关的技术方向和应用案例。",
        ),
        "achievement": (
            "请查找与“{x}”相关的技术成果。",
            "{x}采用了什么技术，主要优势是什么？",
            "寻找可用于相关产业的成果，重点关注“{x}”。",
            "有哪些应用场景适合使用{x}？",
            "请推荐与{x}名称或技术方向相近的科研成果。",
        ),
        "expert": (
            "请查找专家“{x}”的研究方向和技术能力。",
            "{x}主要研究什么领域，有哪些技术特长？",
            "寻找与{x}研究方向相关的专家。",
            "请推荐能解决相关技术问题、方向接近{x}的专家。",
            "我想了解{x}的研究成果、专业方向和合作能力。",
        ),
    }[kind]
    return templates[index % len(templates)].format(x=subject)


def load_cases(path: Path, kind: str, limit: int) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [row for row in rows if row.get("knowledge_type") == kind][:limit]
    if len(rows) != limit:
        raise ValueError(f"{path} 只有 {len(rows)} 条 {kind} 用例，期望 {limit} 条")
    return rows


def fetch_rows(kind: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    table, _ = TABLES[kind]
    placeholders = ",".join(["%s"] * len(ids))
    config = {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DATABASE", "kechuang"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 10,
        "read_timeout": 30,
    }
    with pymysql.connect(**config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{table}` WHERE id IN ({placeholders})", ids)
            rows = cursor.fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def generate(kind: str, source_dir: Path, output_dir: Path, limit: int, *, offline: bool) -> None:
    cases = load_cases(source_dir / f"{kind}.jsonl", kind, limit)
    records: dict[str, dict[str, Any]] = {}
    if not offline:
        ids = [source_id(case) for case in cases]
        records = fetch_rows(kind, list(dict.fromkeys(ids)))
        missing = [item for item in dict.fromkeys(ids) if item not in records]
        if missing:
            raise ValueError(f"{kind} 在 MySQL 中找不到标注记录: {', '.join(missing[:10])}")

    output: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        row = dict(case)
        row["relevant_record_ids"] = [source_id(case)] if case.get("answerable", True) else []
        query = (
            build_seed_query(kind, str(case["query"]), index)
            if offline
            else build_query(kind, records[source_id(case)], index)
        )
        row["query"] = query
        row["status"] = "candidate"
        output.append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{kind}.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output),
        encoding="utf-8",
    )
    print(f"[{kind}] 已生成 {len(output)} 条，问题类型 {len({row['query'] for row in output})} 种以上")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("evaluation/retrieval_questions"))
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation/retrieval_questions"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--knowledge-type", choices=tuple(TABLES), help="只生成指定知识库")
    parser.add_argument("--offline", action="store_true", help="不连接 MySQL，仅基于现有标注问题改写表达")
    args = parser.parse_args()
    kinds = (args.knowledge_type,) if args.knowledge_type else tuple(TABLES)
    for kind in kinds:
        generate(kind, args.source_dir, args.output_dir, args.limit, offline=args.offline)


if __name__ == "__main__":
    main()
