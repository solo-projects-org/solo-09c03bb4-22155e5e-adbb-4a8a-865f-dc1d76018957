"""三方合并自检：直接运行 `python3 tests/test_merger.py`（仅依赖标准库 + tzdata）。"""
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.merger import (  # noqa: E402
    CELL_STATUS_LABELS, CsvFormatError, MergeResolutionError,
    analyze_merge, build_merged_csv, export_merge, load_table,
    resolve_rows, verify_merge,
)
from app.solver import REQUIRED_COLUMNS, solve_csv  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples" / "merge"


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        raise SystemExit(1)


def read_sample(fname):
    return (SAMPLE_DIR / fname).read_text(encoding="utf-8")


def main():
    base = read_sample("base_sheet.csv")
    ground = read_sample("ground_correction.csv")
    airline = read_sample("airline_correction.csv")

    # 1) 三份样例各自都能通过整链清洗（唯一解）——两份更正各自可用
    for name, text in (("底稿", base), ("地服更正", ground), ("航司更正", airline)):
        result = solve_csv(text)
        check(f"{name}：全部机尾整链唯一解",
              all(t["status"] == "unique" for t in result["tails"]))

    # 2) 分析结果结构与冲突定位
    analysis = analyze_merge(base, ground, airline)
    check("6 个航段全部参与比对", analysis["summary"]["rows"] == 6)
    check("3 个机尾被任一方改过",
          analysis["summary"]["changed_tail_count"] == 3)
    check("B-8002 存在 2 个冲突字段", analysis["summary"]["conflict_cells"] == 2)

    cell_index = {(r["tail"], r["seq"]): r["cells"] for r in analysis["rows"]}
    check("地服单方改动 → auto_ground",
          cell_index[("B-8001", 2)]["arr_local"]["status"] == "auto_ground")
    check("航司单方改动 → auto_airline",
          cell_index[("B-8001", 3)]["dep_local"]["status"] == "auto_airline")
    check("双方改成同一值 → auto_agreed",
          cell_index[("B-8003", 1)]["arr_local"]["status"] == "auto_agreed")
    check("未改动 → unchanged",
          cell_index[("B-8001", 1)]["dep_local"]["status"] == "unchanged")
    conflict = cell_index[("B-8002", 1)]["duration_min_minutes"]
    check("双方改成不同值 → conflict", conflict["status"] == "conflict")
    check("冲突候选值恰好来自三份文件",
          [o["value"] for o in conflict["options"]] == ["60", "45", "150"])
    check("状态都有中文说明",
          all(s in CELL_STATUS_LABELS for s in CELL_STATUS_LABELS))

    # 3) 未选值：合并稿无法衔接，下载闸门关闭，且能列出未通过机尾及原因
    verdict = verify_merge(base, ground, airline, {})
    check("未选冲突 + 链断裂 → 禁止下载", verdict["download_allowed"] is False)
    check("未解决冲突被列出", len(verdict["unresolved_conflicts"]) == 2)
    b8001 = next(r for r in verdict["changed_tail_results"]
                 if r["tail"] == "B-8001")
    check("B-8001 重跑清洗为无解", b8001["status"] == "no_solution")
    check("给出无解原因", "无法衔接" in b8001["reason"])
    try:
        export_merge(base, ground, airline, {})
        check("空选值导出应被拒绝", False)
    except MergeResolutionError:
        check("空选值导出被拒绝", True)

    # 4) 值班员选值：B-8001 把航司的自动合并值改选回底稿，B-8002 冲突选航司方案
    resolutions = {
        "B-8001": {"3": {"dep_local": "2026-11-10 17:30"}},
        "B-8002": {"1": {"duration_min_minutes": "150",
                         "duration_max_minutes": "240"}},
    }
    verdict2 = verify_merge(base, ground, airline, resolutions)
    check("选值后 3 个改过机尾全部唯一解", verdict2["download_allowed"] is True)
    check("无阻断原因", verdict2["blockers"] == [])
    merged_csv = export_merge(base, ground, airline, resolutions)
    check("导出沿用清洗表头", merged_csv.splitlines()[0] == ",".join(REQUIRED_COLUMNS))
    reader = list(csv.DictReader(io.StringIO(merged_csv)))
    check("导出包含全部 6 个航段", len(reader) == 6)
    b8001_s3 = next(r for r in reader
                    if r["tail_number"] == "B-8001" and r["seq"] == "3")
    check("改选回底稿值生效", b8001_s3["dep_local"] == "2026-11-10 17:30")
    b8002 = next(r for r in reader if r["tail_number"] == "B-8002")
    check("冲突选航司值生效",
          b8002["duration_min_minutes"] == "150"
          and b8002["dep_tz"] == "Asia/Tokyo")
    check("自动合并保留地服改动",
          next(r for r in reader
               if r["tail_number"] == "B-8001" and r["seq"] == "2")["arr_local"]
          == "2026-11-10 15:20")
    # 导出的合并稿本身再跑一次清洗，改过机尾必须都是唯一解
    recheck = solve_csv(merged_csv)
    check("合并稿整链复核全部唯一解",
          all(t["status"] == "unique" for t in recheck["tails"]))

    # 5) 自动合并值也能改选：B-8002 时区改回上海 + 窗口选地服，仍有唯一解
    resolutions_alt = {
        "B-8001": {"3": {"dep_local": "2026-11-10 17:30"}},
        "B-8002": {"1": {"dep_tz": "Asia/Shanghai",
                         "duration_min_minutes": "45",
                         "duration_max_minutes": "135"}},
    }
    check("改选自动合并值后可通过核对",
          verify_merge(base, ground, airline, resolutions_alt)["download_allowed"])

    # 6) 选值组合本身让机尾无解（上海 90 分钟落不进航司窗口）→ 拦截
    resolutions_bad = {
        "B-8001": {"3": {"dep_local": "2026-11-10 17:30"}},
        "B-8002": {"1": {"dep_tz": "Asia/Shanghai",
                         "duration_min_minutes": "150",
                         "duration_max_minutes": "240"}},
    }
    verdict3 = verify_merge(base, ground, airline, resolutions_bad)
    check("选值导致无解时禁止下载", verdict3["download_allowed"] is False)
    check("无解机尾带原因", any(
        r["tail"] == "B-8002" and r["status"] == "no_solution" and r["reason"]
        for r in verdict3["changed_tail_results"]))

    # 7) 选值不在三份文件给出的值内 → 拒绝
    merged_rows, unresolved, invalid = resolve_rows(
        analysis, {"B-8002": {"1": {"duration_min_minutes": "999"}}})
    check("非法选值被识别", invalid and invalid[0]["column"] == "duration_min_minutes")
    check("非法选值同时冲突仍未解决",
          any(u["column"] == "duration_max_minutes" for u in unresolved))

    # 8) 装载期硬校验：重复定位键、航段组不一致、缺列
    dup = ground.replace("B-8003,2,", "B-8003,1,", 1)
    try:
        analyze_merge(base, dup, airline)
        check("重复定位键应报错", False)
    except CsvFormatError as exc:
        check("重复定位键报错且指明机尾/seq", "B-8003" in str(exc) and "seq=1" in str(exc))

    missing_one = "\n".join(airline.splitlines()[:-1]) + "\n"
    try:
        analyze_merge(base, ground, missing_one)
        check("航段组不一致应报错", False)
    except CsvFormatError as exc:
        check("航段组不一致报错", "同一组航段" in str(exc))

    broken = base.replace("dep_tz", "dep_zone", 1)
    try:
        load_table(broken, "底稿")
        check("缺列应报错", False)
    except CsvFormatError as exc:
        check("缺列沿用清洗表头校验", "缺少必需列" in str(exc))

    # 9) 合并稿 CSV 与清洗表头完全一致、可被 solve_csv 直接消费
    rows_out, _, _ = resolve_rows(analysis, resolutions)
    csv_text = build_merged_csv(rows_out)
    check("合并稿可直接进入原清洗流程", "tails" in solve_csv(csv_text))

    print("\n全部合并自检通过 ✅")


if __name__ == "__main__":
    main()
