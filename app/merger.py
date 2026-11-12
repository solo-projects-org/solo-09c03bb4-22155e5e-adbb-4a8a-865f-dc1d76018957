"""三向合并核心：底稿 + 地服更正 + 航司值班员更正。

业务规则
--------
- 三份 CSV 沿用清洗表头（见 solver.REQUIRED_COLUMNS），以 (tail_number, seq)
  作为航段定位键；每份文件内部定位键不得重复，三份文件必须包含同一组航段。
- 逐字段对照底稿（tail_number / seq 为定位键，不参与比对）：
  * 三方一致           → unchanged（沿用原值）
  * 仅一方改动         → auto（自动采用改动值）
  * 两方改成同一值     → auto（自动采用共同改动值）
  * 两方改成不同值     → conflict（由值班员从三份文件给出的值里三选一）
- 自动合并得到的值也允许改选（值班员可覆盖成底稿/另一方给出的值）。
- 仅对「任一方改过」的机尾重跑整链清洗；完全没动过的机尾不重跑。
- 冲突全部选完、且所有受影响机尾整链唯一解，才允许下载合并后的当地时间 CSV。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .solver import (
    CsvFormatError,
    RawRow,
    read_rows,
    solve_csv,
)

# 参与逐字段比对的字段（tail_number / seq 是定位键，不参与）
MERGE_FIELDS = [
    "dep_local",
    "dep_tz",
    "arr_local",
    "arr_tz",
    "duration_min_minutes",
    "duration_max_minutes",
]

# 字段在 RawRow 上的属性名
_FIELD_ATTR = {
    "dep_local": "dep_local_raw",
    "dep_tz": "dep_tz",
    "arr_local": "arr_local_raw",
    "arr_tz": "arr_tz",
    "duration_min_minutes": "dur_min_raw",
    "duration_max_minutes": "dur_max_raw",
}

# 值班员选择值的来源
SOURCE_BASE = "base"
SOURCE_GROUND = "ground"
SOURCE_AIRLINE = "airline"
SOURCES = (SOURCE_BASE, SOURCE_GROUND, SOURCE_AIRLINE)

# 单元格合并状态
ST_UNCHANGED = "unchanged"
ST_AUTO = "auto"
ST_CONFLICT = "conflict"


class MergeChoiceError(Exception):
    """值班员提交的选值不合法（定位键不存在、来源非法、值与文件不符）。"""


@dataclass
class MergeCell:
    field: str
    status: str
    base: str
    ground: str
    airline: str
    # auto 时为自动采用值；conflict 未选时为 None
    chosen: str | None
    chosen_source: str | None
    changed_by: list  # 相对底稿发生改动的来源，如 ["ground"] / ["ground", "airline"]


def _index_rows(rows: list[RawRow], label: str) -> dict:
    """把 RawRow 列表按 (tail, seq) 建索引，重复定位键直接报错。"""
    out: dict[tuple[str, str], RawRow] = {}
    for raw in rows:
        key = (raw.tail, raw.seq_raw)
        if key in out:
            raise CsvFormatError(
                f"{label}中定位键重复：机尾 {raw.tail!r}、seq={raw.seq_raw!r}"
                f"（第 {out[key].row_number} 行与第 {raw.row_number} 行），"
                "同一机尾的航段序号不得重复"
            )
        out[key] = raw
    return out


def _index_all(base_rows, ground_rows, airline_rows):
    base = _index_rows(base_rows, "底稿")
    ground = _index_rows(ground_rows, "地服更正")
    airline = _index_rows(airline_rows, "航司更正")

    base_keys, g_keys, a_keys = set(base), set(ground), set(airline)
    if g_keys != base_keys or a_keys != base_keys:
        problems = []
        diffs = (
            ("仅底稿存在", base_keys - g_keys - a_keys),
            ("仅地服更正存在", g_keys - base_keys),
            ("仅航司更正存在", a_keys - base_keys),
            ("底稿与地服不一致", (base_keys ^ g_keys) & base_keys),
        )
        for label, keys in diffs:
            if keys:
                problems.append(label + ": "
                                + ", ".join(f"{t}/seq={s}" for t, s in sorted(keys)))
        raise CsvFormatError(
            "三份文件必须包含同一组航段（机尾号 + 航段序号）："
            + "；".join(problems)
        )
    return base, ground, airline


def _cell(field: str, base_val: str, ground_val: str, airline_val: str) -> MergeCell:
    changed_by = []
    if ground_val != base_val:
        changed_by.append(SOURCE_GROUND)
    if airline_val != base_val:
        changed_by.append(SOURCE_AIRLINE)

    if not changed_by:
        return MergeCell(field, ST_UNCHANGED, base_val, ground_val, airline_val,
                         base_val, SOURCE_BASE, [])

    if ground_val == airline_val:
        # 双方改成同一值（含仅一方改动、另一方与底稿一致的情形）
        chosen, source = ground_val, SOURCE_GROUND
        if airline_val != base_val and ground_val == base_val:
            source = SOURCE_AIRLINE
        return MergeCell(field, ST_AUTO, base_val, ground_val, airline_val,
                         chosen, source, changed_by)

    if len(changed_by) == 1:
        # 仅一方改动
        source = changed_by[0]
        chosen = ground_val if source == SOURCE_GROUND else airline_val
        return MergeCell(field, ST_AUTO, base_val, ground_val, airline_val,
                         chosen, source, changed_by)

    # 双方都改且改成不同值 → 冲突，候选为三份文件给出的值（去重保序）
    return MergeCell(field, ST_CONFLICT, base_val, ground_val, airline_val,
                     None, None, changed_by)


def _cell_view(cell: MergeCell) -> dict:
    options = []
    for source in SOURCES:
        val = {"base": cell.base, "ground": cell.ground,
               "airline": cell.airline}[source]
        if not options or options[-1]["value"] != val:
            options.append({"source": source, "value": val})
    return {
        "field": cell.field,
        "status": cell.status,
        "base": cell.base,
        "ground": cell.ground,
        "airline": cell.airline,
        "options": options,
        "chosen": cell.chosen,
        "chosen_source": cell.chosen_source,
        "changed_by": cell.changed_by,
    }


def analyze_merge(base_text: str, ground_text: str, airline_text: str) -> dict:
    """解析三份 CSV 并完成字段级三向比对（不执行清洗求解）。"""
    base_rows = read_rows(base_text)
    ground_rows = read_rows(ground_text)
    airline_rows = read_rows(airline_text)
    base_idx, ground_idx, airline_idx = _index_all(
        base_rows, ground_rows, airline_rows)

    # 机尾按底稿首次出现顺序展示，机尾内按 seq 数值排序
    tail_order: list[str] = []
    tail_seqs: dict[str, list[str]] = {}
    for key in base_idx:
        tail, seq = key
        if tail not in tail_seqs:
            tail_order.append(tail)
            tail_seqs[tail] = []
        tail_seqs[tail].append(seq)

    tails = []
    conflict_count = 0
    auto_count = 0
    for tail in tail_order:
        segments = []
        tail_touched = False
        for seq in sorted(tail_seqs[tail], key=_seq_sort_key):
            key = (tail, seq)
            rb, rg, ra = base_idx[key], ground_idx[key], airline_idx[key]
            try:
                seq_int = int(seq)
            except ValueError:
                raise CsvFormatError(
                    f"机尾 {tail!r} 的 seq 不是整数: {seq!r}，无法作为航段定位键")
            cells = []
            for field in MERGE_FIELDS:
                attr = _FIELD_ATTR[field]
                cell = _cell(field, getattr(rb, attr),
                             getattr(rg, attr), getattr(ra, attr))
                if cell.status == ST_CONFLICT:
                    conflict_count += 1
                elif cell.status == ST_AUTO:
                    auto_count += 1
                if cell.status != ST_UNCHANGED:
                    tail_touched = True
                cells.append(_cell_view(cell))
            segments.append({
                "seq": seq_int,
                "row": rb.row_number,
                "cells": cells,
            })
        tails.append({
            "tail": tail,
            "touched": tail_touched,
            "segments": segments,
        })

    return {
        "summary": {
            "rows": len(base_idx),
            "total_tails": len(tail_order),
            "touched_tails": sum(1 for t in tails if t["touched"]),
            "auto_cells": auto_count,
            "conflict_cells": conflict_count,
        },
        "tails": tails,
    }


def _seq_sort_key(seq_raw: str):
    try:
        return (0, int(seq_raw))
    except ValueError:
        return (1, seq_raw)


def _apply_choices(analysis: dict, choices: list[dict]) -> dict:
    """把值班员选值写回分析结构，返回 (tail -> seq -> field -> 选中值)。"""
    selected: dict[str, dict[int, dict[str, dict]]] = {}
    for t in analysis["tails"]:
        selected[t["tail"]] = {}
        for seg in t["segments"]:
            selected[t["tail"]][seg["seq"]] = {
                c["field"]: c for c in seg["cells"]}

    for ch in choices or []:
        tail = ch.get("tail")
        seq = ch.get("seq")
        field = ch.get("field")
        source = ch.get("source")
        if tail not in selected or seq not in selected[tail] \
                or field not in selected[tail][seq]:
            raise MergeChoiceError(
                f"选值定位不存在：机尾 {tail!r}、seq={seq!r}、字段 {field!r}")
        if source not in SOURCES:
            raise MergeChoiceError(
                f"选值来源非法：{source!r}，只允许 base / ground / airline")
        cell = selected[tail][seq][field]
        value = cell[source]
        # 来源给出的值必须与该文件中的原值一致，杜绝伪造取值
        if not any(o["source"] == source and o["value"] == value
                   for o in cell["options"]):
            raise MergeChoiceError(
                f"选值与文件内容不符：机尾 {tail!r}、seq={seq!r}、"
                f"字段 {field!r} 的 {source} 值为 {value!r}")
        cell["chosen"] = value
        cell["chosen_source"] = source
    return selected


def _unresolved_conflicts(analysis: dict) -> list[dict]:
    out = []
    for t in analysis["tails"]:
        for seg in t["segments"]:
            for cell in seg["cells"]:
                if cell["status"] == ST_CONFLICT and cell["chosen"] is None:
                    out.append({"tail": t["tail"], "seq": seg["seq"],
                                "field": cell["field"]})
    return out


def build_merged_csv(analysis: dict, choices: list[dict]) -> str:
    """按当前选值生成合并后的当地时间 CSV（沿用清洗表头）。"""
    _apply_choices(analysis, choices)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "tail_number", "seq", "dep_local", "dep_tz", "arr_local", "arr_tz",
        "duration_min_minutes", "duration_max_minutes",
    ])
    for t in analysis["tails"]:
        for seg in sorted(t["segments"], key=lambda s: s["seq"]):
            vals = {c["field"]: c["chosen"] for c in seg["cells"]}
            writer.writerow([
                t["tail"], seg["seq"],
                vals["dep_local"], vals["dep_tz"],
                vals["arr_local"], vals["arr_tz"],
                vals["duration_min_minutes"], vals["duration_max_minutes"],
            ])
    return buf.getvalue()


def verify_merge(analysis: dict, choices: list[dict]) -> dict:
    """按当前合并稿重跑清洗，返回冲突选值状态与受影响机尾的整链求解结果。"""
    _apply_choices(analysis, choices)
    unresolved = _unresolved_conflicts(analysis)

    affected_tails = [t["tail"] for t in analysis["tails"] if t["touched"]]
    merged_text = build_merged_csv(analysis, choices)
    solve_result = solve_csv(merged_text)
    solve_by_tail = {t["tail"]: t for t in solve_result["tails"]}

    checks = []
    all_unique = not unresolved
    # 仍有冲突未选时，合并稿含空值，行级报错没有参考意义；此时只报未解决冲突
    skip_solve = bool(unresolved)
    if not skip_solve:
        for tail in affected_tails:
            r = solve_by_tail.get(tail)
            if r is None:
                continue
            reasons = _tail_reasons(r)
            ok = r["status"] == "unique"
            all_unique = all_unique and ok
            checks.append({
                "tail": tail,
                "status": r["status"],
                "unique": ok,
                "reasons": reasons,
            })

    return {
        "unresolved_conflicts": unresolved,
        "all_conflicts_resolved": not unresolved,
        "affected_tails": affected_tails,
        "checks": checks,
        "all_unique": all_unique,
        "download_allowed": not unresolved and all_unique,
        "solve_result": solve_result,
    }


def _tail_reasons(tail_result: dict) -> list[str]:
    """把求解结果翻译成给值班员看的未通过原因。"""
    status = tail_result["status"]
    if status == "unique":
        return []
    if status == "error":
        reasons = []
        for e in tail_result.get("errors", []):
            prefix = f"第 {e['row']} 行：" if e.get("row") else ""
            reasons.append(prefix + "；".join(e["messages"]))
        return reasons or ["行级数据错误"]
    if status == "no_solution":
        return [tail_result["conflict"]["message"]]
    if status == "multiple":
        n = tail_result["solution_count"]
        plus = "+" if tail_result.get("solution_count_truncated") else ""
        return [f"整链存在 {n}{plus} 条可行候选，无法确定唯一入仓时刻"]
    return [f"未知求解状态：{status}"]


def export_merged(analysis: dict, choices: list[dict]) -> str:
    """最终下载入口：复核通过后导出当地时间 CSV（供原清洗流程使用）。

    返回 (csv_text, affected_tails)；未选完冲突或存在非唯一解机尾时抛 MergeChoiceError。
    """
    verification = verify_merge(analysis, choices)
    if verification["unresolved_conflicts"]:
        items = "；".join(
            f"{c['tail']}/seq={c['seq']}/{c['field']}"
            for c in verification["unresolved_conflicts"])
        raise MergeChoiceError("仍有冲突未选择，禁止下载：" + items)
    failed = [c for c in verification["checks"] if not c["unique"]]
    if failed:
        detail = "；".join(
            f"{c['tail']}（{'；'.join(c['reasons']) or c['status']}）"
            for c in failed)
        raise MergeChoiceError("以下机尾整链非唯一解，禁止下载：" + detail)
    csv_text = build_merged_csv(analysis, choices)
    return csv_text, verification["affected_tails"]
