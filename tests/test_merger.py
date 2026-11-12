"""三向合并自检：`python3 tests/test_merger.py`（仅依赖标准库 + tzdata）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.merger import (  # noqa: E402
    MergeChoiceError,
    ST_AUTO,
    ST_CONFLICT,
    ST_UNCHANGED,
    analyze_merge,
    build_merged_csv,
    export_merged,
    verify_merge,
)
from app.solver import CsvFormatError, solve_csv  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples" / "merge"

HEADER = ("tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,"
          "duration_min_minutes,duration_max_minutes\n")


def csv_text(rows):
    return HEADER + "\n".join(",".join(map(str, r)) for r in rows) + "\n"


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        raise SystemExit(1)


def cell(analysis, tail, seq, field):
    t = next(x for x in analysis["tails"] if x["tail"] == tail)
    seg = next(s for s in t["segments"] if s["seq"] == seq)
    return next(c for c in seg["cells"] if c["field"] == field)


def main():
    base = csv_text([
        ["T1", 1, "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 14:00",
         "Asia/Dubai", 420, 540],
        ["T1", 2, "2026-06-01 16:00", "Asia/Dubai", "2026-06-02 02:00",
         "Asia/Shanghai", 300, 480],
    ])
    # 地服只改第一段落地；航司只改第二段起飞（互不重叠 → 全部自动合并）
    ground = csv_text([
        ["T1", 1, "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 14:30",
         "Asia/Dubai", 420, 540],
        ["T1", 2, "2026-06-01 16:00", "Asia/Dubai", "2026-06-02 02:00",
         "Asia/Shanghai", 300, 480],
    ])
    airline = csv_text([
        ["T1", 1, "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 14:00",
         "Asia/Dubai", 420, 540],
        ["T1", 2, "2026-06-01 16:30", "Asia/Dubai", "2026-06-02 02:00",
         "Asia/Shanghai", 300, 480],
    ])

    an = analyze_merge(base, ground, airline)
    check("自动合并场景无冲突", an["summary"]["conflict_cells"] == 0)
    check("自动合并单元格数 = 2", an["summary"]["auto_cells"] == 2)
    c1 = cell(an, "T1", 1, "arr_local")
    check("仅地服改动 → 自动采用地服值",
          c1["status"] == ST_AUTO and c1["chosen"] == "2026-06-01 14:30"
          and c1["chosen_source"] == "ground")
    c2 = cell(an, "T1", 2, "dep_local")
    check("仅航司改动 → 自动采用航司值",
          c2["status"] == ST_AUTO and c2["chosen"] == "2026-06-01 16:30"
          and c2["chosen_source"] == "airline")
    c3 = cell(an, "T1", 1, "dep_tz")
    check("三方一致 → unchanged", c3["status"] == ST_UNCHANGED)

    v = verify_merge(an, [])
    check("无冲突且唯一解 → 允许下载", v["download_allowed"] is True)
    check("只核对被改过的机尾", v["affected_tails"] == ["T1"])

    # 1) 双方改成同一值 → 自动合并
    ground2 = csv_text([
        ["T1", 1, "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 15:00",
         "Asia/Dubai", 420, 600],
        ["T1", 2, "2026-06-01 16:00", "Asia/Dubai", "2026-06-02 02:00",
         "Asia/Shanghai", 300, 480],
    ])
    airline2 = csv_text([
        ["T1", 1, "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 15:00",
         "Asia/Dubai", 420, 600],
        ["T1", 2, "2026-06-01 16:00", "Asia/Dubai", "2026-06-02 02:00",
         "Asia/Shanghai", 300, 480],
    ])
    an2 = analyze_merge(base, ground2, airline2)
    cs = cell(an2, "T1", 1, "arr_local")
    check("双方改成同一值 → 自动合并",
          cs["status"] == ST_AUTO and cs["chosen"] == "2026-06-01 15:00")

    # 2) 双方改成不同值 → 冲突，候选恰好来自三份文件
    airline3 = csv_text([
        ["T1", 1, "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 13:30",
         "Asia/Dubai", 420, 540],
        ["T1", 2, "2026-06-01 16:00", "Asia/Dubai", "2026-06-02 02:00",
         "Asia/Shanghai", 300, 480],
    ])
    an3 = analyze_merge(base, ground2, airline3)
    cf = cell(an3, "T1", 1, "arr_local")
    check("双方改成不同值 → 冲突", cf["status"] == ST_CONFLICT
          and cf["chosen"] is None)
    check("冲突候选取自三份文件",
          [o["value"] for o in cf["options"]]
          == ["2026-06-01 14:00", "2026-06-01 15:00", "2026-06-01 13:30"])
    v3 = verify_merge(an3, [])
    check("冲突未选 → 禁止下载", v3["download_allowed"] is False
          and len(v3["unresolved_conflicts"]) == 1)
    try:
        export_merged(analyze_merge(base, ground2, airline3), [])
        check("冲突未选时导出抛错", False)
    except MergeChoiceError:
        check("冲突未选时导出抛错", True)

    # 值班员三选一（含改选自动值）后放行
    choice = [{"tail": "T1", "seq": 1, "field": "arr_local", "source": "base"}]
    an4 = analyze_merge(base, ground2, airline3)
    v4 = verify_merge(an4, choice)
    check("选底稿值后冲突消解", v4["all_conflicts_resolved"] is True)
    csv_out, _ = export_merged(analyze_merge(base, ground2, airline3), choice)
    check("下载稿采用所选底稿值", "2026-06-01 14:00" in csv_out
          and "2026-06-01 15:00" not in csv_out)
    bad = [{"tail": "T1", "seq": 1, "field": "arr_local", "source": "mars"}]
    try:
        verify_merge(analyze_merge(base, ground2, airline3), bad)
        check("非法来源被拒", False)
    except MergeChoiceError:
        check("非法来源被拒", True)

    # 3) 结构校验：定位键重复 / 航段集合不一致 / 缺列
    dup = csv_text([
        ["T1", 1, "x", "Asia/Shanghai", "2026-06-01 14:00", "Asia/Dubai", 1, 2],
        ["T1", 1, "x", "Asia/Shanghai", "2026-06-01 14:00", "Asia/Dubai", 1, 2],
    ])
    try:
        analyze_merge(dup, ground, airline)
        check("重复定位键报错", False)
    except CsvFormatError:
        check("重复定位键报错", True)

    missing_row = csv_text([
        ["T1", 1, "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 14:00",
         "Asia/Dubai", 420, 540],
    ])
    try:
        analyze_merge(base, missing_row, airline)
        check("三份航段集合不一致报错", False)
    except CsvFormatError:
        check("三份航段集合不一致报错", True)

    # 底稿缺航段但两份更正都有（另一方向的集合差异）
    try:
        analyze_merge(missing_row, ground, airline)
        check("底稿缺航段时报错", False)
    except CsvFormatError:
        check("底稿缺航段时报错", True)

    try:
        analyze_merge(base, ground, missing_row)
        check("航司缺航段时报错", False)
    except CsvFormatError:
        check("航司缺航段时报错", True)

    non_int_seq = csv_text([
        ["T1", "1a", "2026-06-01 10:00", "Asia/Shanghai", "2026-06-01 14:00",
         "Asia/Dubai", 420, 540],
        ["T1", 2, "2026-06-01 16:00", "Asia/Dubai", "2026-06-02 02:00",
         "Asia/Shanghai", 300, 480],
    ])
    try:
        analyze_merge(non_int_seq, non_int_seq, non_int_seq)
        check("非整数 seq 报错", False)
    except CsvFormatError:
        check("非整数 seq 报错", True)

    bad_header = "tail_number,seq\nT1,1\n"
    try:
        analyze_merge(bad_header, ground, airline)
        check("缺列表头报错", False)
    except CsvFormatError:
        check("缺列表头报错", True)

    # 4) 内置样例：两份更正各自全机尾唯一解；合并稿 B-8001 无解 → 禁止下载
    b = (SAMPLE_DIR / "merge_base.csv").read_text(encoding="utf-8")
    g = (SAMPLE_DIR / "merge_ground.csv").read_text(encoding="utf-8")
    a = (SAMPLE_DIR / "merge_airline.csv").read_text(encoding="utf-8")
    for name, text in (("ground", g), ("airline", a)):
        statuses = {t["tail"]: t["status"] for t in solve_csv(text)["tails"]}
        check(f"样例 {name} 单独清洗全部唯一解",
              all(s == "unique" for s in statuses.values()))

    ans = analyze_merge(b, g, a)
    check("样例存在 1 个冲突单元格（B-8002）",
          ans["summary"]["conflict_cells"] == 1)
    resolve = [{"tail": "B-8002", "seq": 1, "field": "arr_local",
                "source": "base"}]
    vs = verify_merge(analyze_merge(b, g, a), resolve)
    b8001 = next(c for c in vs["checks"] if c["tail"] == "B-8001")
    check("合并后 B-8001 无法衔接（no_solution）",
          b8001["status"] == "no_solution" and not b8001["unique"])
    check("未通过原因指向 seq=2 衔接冲突", "seq=2" in b8001["reasons"][0])
    check("存在非唯一解机尾 → 禁止下载", vs["download_allowed"] is False)
    b8002 = next(c for c in vs["checks"] if c["tail"] == "B-8002")
    b8003 = next(c for c in vs["checks"] if c["tail"] == "B-8003")
    check("B-8002 选值后唯一解", b8002["unique"])
    check("B-8003 自动合并后唯一解", b8003["unique"])
    check("未改动机尾 B-8004 不参与核对",
          "B-8004" not in vs["affected_tails"])
    try:
        export_merged(analyze_merge(b, g, a), resolve)
        check("样例合并稿导出被拒", False)
    except MergeChoiceError:
        check("样例合并稿导出被拒", True)

    # 5) 合并稿表头沿用清洗表头
    merged = build_merged_csv(analyze_merge(base, ground, airline), [])
    check("合并稿沿用清洗表头",
          merged.splitlines()[0] == HEADER.strip())

    print("\n全部合并自检通过 ✅")


if __name__ == "__main__":
    main()
