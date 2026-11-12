"""三方航段底稿合并核心逻辑（纯标准库）。

业务背景
--------
地服与航司值班员各自在同一份航段底稿上做了更正，入仓前需要把两份更正
合并成一份新的当地时间底稿，再走原有的整链 UTC 清洗流程。

合并规则
--------
- 三份 CSV 沿用清洗表头，以 (tail_number, seq) 作为航段定位键；
  三份文件必须包含同一组航段，且每份文件内定位键不能重复。
- 逐字段对照底稿：
  * 两边都没改            → 保留底稿值（unchanged）；
  * 仅一方改              → 自动取改动方的值（auto_ground / auto_airline）；
  * 两边都改且改成同一值  → 自动取该值（auto_agreed）；
  * 两边都改且值不同      → 冲突（conflict），由值班员从三份文件给出的值里选一个。
- 自动合并得到的值同样允许值班员改选，但选值必须来自三份文件之一。
- 合并稿只对「任一方改过的机尾」重跑整链清洗；全部冲突选完且这些机尾
  都是整链唯一解，才允许下载当地时间 CSV。
"""
from __future__ import annotations

import csv
import io

from .solver import REQUIRED_COLUMNS, CsvFormatError, read_rows, solve_csv

# 参与逐字段比对的列（tail_number / seq 是定位键，不参与比对）
MERGE_FIELDS = [c for c in REQUIRED_COLUMNS if c not in ("tail_number", "seq")]

SIDE_LABELS = {"base": "底稿", "ground": "地服", "airline": "航司"}

# 单元格状态 → 中文说明
CELL_STATUS_LABELS = {
    "unchanged": "未改动",
    "auto_ground": "地服单方修改 · 自动合并",
    "auto_airline": "航司单方修改 · 自动合并",
    "auto_agreed": "双方修改一致 · 自动合并",
    "conflict": "双方修改不一致 · 待值班员选值",
}
AUTO_STATUSES = {"auto_ground", "auto_airline", "auto_agreed"}


class MergeResolutionError(Exception):
    """值班员的选值不合法（不在三份文件给出的值内）。"""


# ---------------------------------------------------------------------------
# 三份文件的装载与一致性校验
# ---------------------------------------------------------------------------

def load_table(csv_text: str, label: str) -> tuple[dict, list]:
    """读取一份 CSV，返回 ({(tail, seq): 行dict}, 底稿顺序的 key 列表)。"""
    raws = read_rows(csv_text)  # 缺列/空文件等抛 CsvFormatError
    table: dict = {}
    order: list = []
    for raw in raws:
        try:
            seq = int(raw.seq_raw)
        except ValueError:
            raise CsvFormatError(
                f"{label}：seq 必须是整数航段序号，收到 {raw.seq_raw!r}")
        if not raw.tail:
            raise CsvFormatError(f"{label}：存在 tail_number（机尾号）为空的行")
        key = (raw.tail, seq)
        if key in table:
            raise CsvFormatError(
                f"{label}：机尾 {raw.tail} 的 seq={seq} 重复，"
                "定位键（机尾号 + 航段序号）不能重复")
        table[key] = {
            "tail_number": raw.tail,
            "seq": str(seq),
            "dep_local": raw.dep_local_raw,
            "dep_tz": raw.dep_tz,
            "arr_local": raw.arr_local_raw,
            "arr_tz": raw.arr_tz,
            "duration_min_minutes": raw.dur_min_raw,
            "duration_max_minutes": raw.dur_max_raw,
        }
        order.append(key)
    return table, order


def _format_key(key: tuple) -> str:
    return f"机尾 {key[0]} seq={key[1]}"


def _check_same_keys(base: dict, ground: dict, airline: dict) -> None:
    kb, kg, ka = set(base), set(ground), set(airline)
    problems = []
    for side, keys, label in (("地服更正", kg, SIDE_LABELS["ground"]),
                              ("航司更正", ka, SIDE_LABELS["airline"])):
        missing = sorted(kb - keys)
        extra = sorted(keys - kb)
        if missing:
            problems.append(
                f"{side}缺少 {len(missing)} 个航段："
                + "；".join(_format_key(k) for k in missing[:5])
                + ("……" if len(missing) > 5 else ""))
        if extra:
            problems.append(
                f"{side}多出 {len(extra)} 个底稿没有的航段："
                + "；".join(_format_key(k) for k in extra[:5])
                + ("……" if len(extra) > 5 else ""))
    if problems:
        raise CsvFormatError("三份文件必须包含同一组航段（机尾号 + 航段序号）："
                             + "；".join(problems))


# ---------------------------------------------------------------------------
# 逐字段比对
# ---------------------------------------------------------------------------

def _classify_cell(base_val: str, ground_val: str, airline_val: str) -> dict:
    ground_changed = ground_val != base_val
    airline_changed = airline_val != base_val

    if not ground_changed and not airline_changed:
        status, merged = "unchanged", base_val
    elif ground_changed and not airline_changed:
        status, merged = "auto_ground", ground_val
    elif airline_changed and not ground_changed:
        status, merged = "auto_airline", airline_val
    elif ground_val == airline_val:
        status, merged = "auto_agreed", ground_val
    else:
        status, merged = "conflict", None

    # 三份文件给出的全部候选值（去重、保持 底稿→地服→航司 顺序）
    options = []
    seen = {}
    for side, val in (("base", base_val), ("ground", ground_val),
                      ("airline", airline_val)):
        if val in seen:
            seen[val]["sources"].append(side)
        else:
            item = {"value": val, "sources": [side]}
            seen[val] = item
            options.append(item)

    return {
        "base": base_val,
        "ground": ground_val,
        "airline": airline_val,
        "status": status,
        "status_label": CELL_STATUS_LABELS[status],
        "merged": merged,
        "options": options,
    }


def analyze_merge(base_text: str, ground_text: str, airline_text: str) -> dict:
    """分析三份 CSV，返回逐航段逐字段的比对结果与冲突清单。"""
    base, base_order = load_table(base_text, "底稿")
    ground, _ = load_table(ground_text, "地服更正")
    airline, _ = load_table(airline_text, "航司更正")
    _check_same_keys(base, ground, airline)

    # 底稿中的数据行号，便于值班员对照原始文件
    raws = read_rows(base_text)
    row_number_by_key = {(r.tail, int(r.seq_raw)): r.row_number for r in raws}

    rows = []
    changed_tails = []
    changed_rows = 0
    conflict_cells = 0

    for key in base_order:
        tail, seq = key
        b_row, g_row, a_row = base[key], ground[key], airline[key]
        cells = {}
        row_changed = False
        row_conflict = False
        for col in MERGE_FIELDS:
            cell = _classify_cell(b_row[col], g_row[col], a_row[col])
            cells[col] = cell
            if cell["status"] != "unchanged":
                row_changed = True
            if cell["status"] == "conflict":
                row_conflict = True
                conflict_cells += 1
        if row_changed and tail not in changed_tails:
            changed_tails.append(tail)
        if row_changed:
            changed_rows += 1
        rows.append({
            "tail": tail,
            "seq": seq,
            "row": row_number_by_key[key],
            "changed": row_changed,
            "has_conflict": row_conflict,
            "cells": cells,
        })

    return {
        "columns": MERGE_FIELDS,
        "rows": rows,
        "changed_tails": changed_tails,
        "summary": {
            "rows": len(rows),
            "changed_rows": changed_rows,
            "conflict_cells": conflict_cells,
            "changed_tail_count": len(changed_tails),
        },
    }


# ---------------------------------------------------------------------------
# 选值落地 + 合并稿生成
# ---------------------------------------------------------------------------

def resolve_rows(analysis: dict, resolutions: dict):
    """把值班员选值合并进分析结果。

    resolutions 结构：{tail: {str(seq): {column: value}}}
    返回 (合并行列表, 未解决冲突列表, 非法选值列表)。
    """
    merged_rows = []
    unresolved = []
    invalid = []

    for row in analysis["rows"]:
        tail, seq = row["tail"], row["seq"]
        picked = resolutions.get(tail, {}).get(str(seq), {})
        out = {"tail_number": tail, "seq": str(seq)}
        for col in MERGE_FIELDS:
            cell = row["cells"][col]
            allowed = {o["value"] for o in cell["options"]}
            if col in picked and picked[col] != "":
                val = picked[col]
                if val not in allowed:
                    invalid.append({
                        "tail": tail, "seq": seq, "column": col,
                        "value": val,
                        "message": (f"机尾 {tail} seq={seq} 字段 {col} "
                                    f"的选值 {val!r} 不在三份文件给出的值内"),
                    })
                    out[col] = cell["merged"] if cell["merged"] is not None \
                        else cell["base"]
                else:
                    out[col] = val
            elif cell["status"] == "conflict":
                unresolved.append({"tail": tail, "seq": seq, "column": col})
                out[col] = cell["base"]  # 占位，下载前会被拦截
            else:
                out[col] = cell["merged"]
        merged_rows.append(out)

    return merged_rows, unresolved, invalid


def build_merged_csv(merged_rows: list) -> str:
    """按清洗表头写出当地时间合并稿 CSV。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(REQUIRED_COLUMNS)
    for row in merged_rows:
        writer.writerow([row[c] for c in REQUIRED_COLUMNS])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 重跑整链清洗 + 下载闸门
# ---------------------------------------------------------------------------

def _tail_block_reason(tail_result: dict):
    """提取非唯一解机尾的 human-readable 原因。"""
    status = tail_result["status"]
    if status == "unique":
        return None
    if status == "error":
        messages = []
        for e in tail_result.get("errors", []):
            prefix = f"第 {e['row']} 行：" if e.get("row") else ""
            messages.append(prefix + "；".join(e["messages"]))
        return "数据错误：" + " | ".join(messages)
    if status == "no_solution":
        return "整链无解：" + tail_result["conflict"]["message"]
    if status == "multiple":
        n = tail_result["solution_count"]
        more = "+" if tail_result.get("solution_count_truncated") else ""
        return f"整链存在 {n}{more} 条可行候选链，无法确定唯一入仓时刻"
    return f"状态 {status} 非唯一解"


def verify_merge(base_text: str, ground_text: str, airline_text: str,
                 resolutions: dict) -> dict:
    """按当前合并稿重跑清洗，核对改过的机尾，给出下载闸门结论。"""
    analysis = analyze_merge(base_text, ground_text, airline_text)
    merged_rows, unresolved, invalid = resolve_rows(analysis, resolutions)
    merged_csv = build_merged_csv(merged_rows)
    solve_result = solve_csv(merged_csv)
    by_tail = {t["tail"]: t for t in solve_result["tails"]}

    blockers = [u["message"] for u in invalid]
    for u in unresolved:
        blockers.append(
            f"未解决冲突：机尾 {u['tail']} seq={u['seq']} 字段 {u['column']} "
            "尚未从三份文件给出的值中选定一个")

    changed_results = []
    for tail in analysis["changed_tails"]:
        t = by_tail[tail]
        reason = _tail_block_reason(t)
        if reason:
            blockers.append(f"机尾 {tail}：{reason}")
        changed_results.append({
            "tail": tail,
            "status": t["status"],
            "reason": reason,
        })

    download_allowed = (not unresolved and not invalid
                        and all(r["status"] == "unique" for r in changed_results))
    return {
        "analysis_summary": analysis["summary"],
        "changed_tails": analysis["changed_tails"],
        "unresolved_conflicts": unresolved,
        "invalid_selections": invalid,
        "changed_tail_results": changed_results,
        "blockers": blockers,
        "download_allowed": download_allowed,
        "merged_csv": merged_csv,
        "analysis": analysis,
    }


def export_merge(base_text: str, ground_text: str, airline_text: str,
                 resolutions: dict) -> str:
    """闸门通过才返回当地时间合并稿 CSV 文本，否则抛 MergeResolutionError。"""
    verdict = verify_merge(base_text, ground_text, airline_text, resolutions)
    if not verdict["download_allowed"]:
        raise MergeResolutionError("；".join(verdict["blockers"])
                                   or "合并稿未通过核对，禁止下载")
    return verdict["merged_csv"]
