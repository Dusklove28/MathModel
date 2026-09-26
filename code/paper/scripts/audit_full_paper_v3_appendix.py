"""Read-only official-result audit for the compact V3 paper appendix."""

from __future__ import annotations

import json
from pathlib import Path

from docx import Document

from audit_full_paper_v2 import rows, wide_table


ROOT = Path(__file__).resolve().parents[3]
DOCX = ROOT / "code/paper/draft/2026华为杯A题完整论文V3-精简附录.docx"


def main() -> None:
    doc = Document(DOCX)
    if len(doc.tables) != 16 or len(doc.inline_shapes) != 10:
        raise AssertionError("V3 must contain nine appendix tables and ten figures")
    q1 = rows(ROOT / "figures/q1/tables/appendix_problem1_wide.csv")
    q2 = rows(ROOT / "figures/q2/tables/selected_results.csv")
    q3 = rows(ROOT / "figures/q3/tables/case_core_2x2.csv")
    if (len(q1), len(q2), len(q3)) != (100, 500, 500):
        raise AssertionError("source CSV coverage has changed")
    source_1 = {row["case"]: row for row in q1}
    source_2 = {(row["case"], int(row["cores"])): row for row in q2}
    source_3 = {(row["case"], int(row["cores"])): row for row in q3}
    if len(source_1) != 100 or len(source_2) != 500 or len(source_3) != 500:
        raise AssertionError("duplicate or missing case-core keys")

    checked = {}
    for index, label, field in ((7, "A1", "makespan"),
                                (8, "A2", "added_copy_bytes")):
        expected = {(case, cores): f"{int(row[f'{field}_k{cores}']):,}"
                    for case, row in source_1.items() for cores in range(1, 6)}
        checked[label] = wide_table(doc.tables[index], expected, label)
    for index, label, field in ((9, "B1", "makespan_cycles"),
                                (10, "B2", "added_copy_bytes")):
        expected = {(case, cores): f"{int(row[field]):,}"
                    for (case, cores), row in source_2.items()}
        checked[label] = wide_table(doc.tables[index], expected, label)
    for index, label, field, kind in (
        (11, "C1", "final_no_l2_makespan", "integer"),
        (12, "C2", "final_l2_makespan", "integer"),
        (13, "C3", "final_no_l2_added_copy_bytes", "integer"),
        (14, "C4", "final_l2_added_copy_bytes", "integer"),
        (15, "C5", "final_l2_cache_hit_rate_bytes", "percent"),
    ):
        expected = {
            (case, cores): (f"{int(row[field]):,}" if kind == "integer"
                            else f"{100 * float(row[field]):.2f}")
            for (case, cores), row in source_3.items()
        }
        checked[label] = wide_table(doc.tables[index], expected, label)

    expected_captions = {
        "A1": "表 A1 100 个案例的 Makespan（cycle）",
        "A2": "表 A2 100 个案例的附加搬运量（byte）",
        "B1": "表 B1 问题二最终 Makespan（cycle）",
        "B2": "表 B2 问题二最终新增搬运（byte）",
        "C1": "表 C1 最终方案无 L2 的 Makespan（cycle）",
        "C2": "表 C2 最终方案有 L2 的 Makespan（cycle）",
        "C3": "表 C3 最终方案无 L2 的总额外搬运（byte）",
        "C4": "表 C4 最终方案有 L2 的总额外搬运（byte）",
        "C5": "表 C5 最终方案有 L2 的字节命中率（%）",
    }
    texts = [paragraph.text.strip() for paragraph in doc.paragraphs]
    for label, caption in expected_captions.items():
        if texts.count(caption) != 1:
            raise AssertionError(f"missing or duplicate caption {label}")
    if any(text.startswith(("表 B3 ", "表 C6 ", "表 C7 ", "表 C8 ", "表 C9 ", "表 C10 "))
           for text in texts):
        raise AssertionError("removed diagnostic captions still appear")
    if len(doc.element.xpath(".//m:oMath")) != 21:
        raise AssertionError("original equations changed")
    print(json.dumps({
        "appendix_tables": list(checked),
        "official_numeric_cells_checked": sum(checked.values()),
        "problem3_numeric_cells_checked": sum(checked[label] for label in checked if label.startswith("C")),
        "figures": len(doc.inline_shapes),
        "omml_equations": 21,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
