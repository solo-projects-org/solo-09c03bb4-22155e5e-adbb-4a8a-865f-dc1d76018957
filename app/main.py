"""FastAPI 入口：航段时间清洗（DST 安全入仓）与三向更正合并。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .merger import (
    MergeChoiceError,
    analyze_merge,
    export_merged,
    verify_merge,
)
from .solver import CsvFormatError, build_export_csv, solve_csv

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR.parent / "samples" / "dst_sample.csv"
MERGE_SAMPLE_DIR = BASE_DIR.parent / "samples" / "merge"
INDEX_PATH = BASE_DIR / "static" / "index.html"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

app = FastAPI(title="航段时间清洗 · DST 安全入仓", version="1.0.0")


async def _read_csv_text(file: UploadFile) -> str:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB 上限")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是合法的 UTF-8 编码 CSV")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX_PATH)


@app.get("/api/sample", include_in_schema=False)
async def sample():
    """内置夏令时样例，可直接下载或在前端一键载入。"""
    return FileResponse(SAMPLE_PATH, media_type="text/csv; charset=utf-8",
                        filename="dst_sample.csv")


@app.post("/api/solve")
async def solve(file: UploadFile = File(...)):
    """上传 CSV，返回每个机尾的求解结果（唯一解/无解/多解/错误）。"""
    text = await _read_csv_text(file)
    try:
        return solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/export")
async def export(file: UploadFile = File(...), tail: str = Form(...)):
    """导出机尾的 UTC CSV。仅当整链唯一解时允许；tail=__all__ 导出全部可导出机尾。"""
    text = await _read_csv_text(file)
    try:
        result = solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))

    if tail == "__all__":
        tails = [t["tail"] for t in result["tails"] if t["status"] == "unique"]
        if not tails:
            raise HTTPException(409, "没有可导出的机尾：仅整链唯一解的机尾允许导出")
        filename = "utc_export_all.csv"
    else:
        match = next((t for t in result["tails"] if t["tail"] == tail), None)
        if match is None:
            raise HTTPException(404, f"CSV 中不存在机尾 {tail!r}")
        if match["status"] != "unique":
            raise HTTPException(
                409, f"机尾 {tail} 状态为 {match['status']}，非整链唯一解，禁止导出")
        tails = [tail]
        filename = f"utc_export_{tail}.csv"

    csv_text = build_export_csv(result, tails)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 三向合并：底稿 + 地服更正 + 航司值班员更正
# ---------------------------------------------------------------------------

_MERGE_SAMPLE_FILES = {
    "base": "merge_base.csv",
    "ground": "merge_ground.csv",
    "airline": "merge_airline.csv",
}


@app.get("/api/merge/sample/{kind}", include_in_schema=False)
async def merge_sample(kind: str):
    """内置三向合并样例：kind ∈ base / ground / airline。"""
    if kind not in _MERGE_SAMPLE_FILES:
        raise HTTPException(404, f"未知样例类型 {kind!r}")
    path = MERGE_SAMPLE_DIR / _MERGE_SAMPLE_FILES[kind]
    return FileResponse(path, media_type="text/csv; charset=utf-8",
                        filename=path.name)


async def _read_three(base: UploadFile, ground: UploadFile, airline: UploadFile):
    return (
        await _read_csv_text(base),
        await _read_csv_text(ground),
        await _read_csv_text(airline),
    )


@app.post("/api/merge/analyze")
async def merge_analyze(
    base: UploadFile = File(...),
    ground: UploadFile = File(...),
    airline: UploadFile = File(...),
):
    """上传底稿与两份更正，返回逐字段三向比对结果（含冲突定位与可选值）。"""
    texts = await _read_three(base, ground, airline)
    try:
        return analyze_merge(*texts)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/merge/verify")
async def merge_verify(
    base: UploadFile = File(...),
    ground: UploadFile = File(...),
    airline: UploadFile = File(...),
    choices: str = Form("[]"),
):
    """按值班员当前选值生成合并稿，对改过的机尾重跑整链清洗并给出未通过原因。"""
    texts = await _read_three(base, ground, airline)
    try:
        choice_list = json.loads(choices)
        if not isinstance(choice_list, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "choices 必须是 JSON 数组")
    try:
        analysis = analyze_merge(*texts)
        return verify_merge(analysis, choice_list)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))
    except MergeChoiceError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/merge/export")
async def merge_export(
    base: UploadFile = File(...),
    ground: UploadFile = File(...),
    airline: UploadFile = File(...),
    choices: str = Form("[]"),
):
    """下载合并后的当地时间 CSV；冲突未选完或机尾非唯一解时返回 409。"""
    texts = await _read_three(base, ground, airline)
    try:
        choice_list = json.loads(choices)
        if not isinstance(choice_list, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "choices 必须是 JSON 数组")
    try:
        analysis = analyze_merge(*texts)
        csv_text, affected = export_merged(analysis, choice_list)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))
    except MergeChoiceError as exc:
        raise HTTPException(409, str(exc))

    filename = "merged_local.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
