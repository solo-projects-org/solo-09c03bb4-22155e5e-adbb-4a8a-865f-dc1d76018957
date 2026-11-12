"""FastAPI 入口：航段时间清洗（DST 安全入仓）+ 地服/航司更正合并。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .merger import (
    CsvFormatError as MergeCsvFormatError,
    MergeResolutionError,
    analyze_merge,
    export_merge,
    verify_merge,
)
from .solver import CsvFormatError, build_export_csv, solve_csv

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR.parent / "samples" / "dst_sample.csv"
MERGE_SAMPLE_DIR = BASE_DIR.parent / "samples" / "merge"
INDEX_PATH = BASE_DIR / "static" / "index.html"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

app = FastAPI(title="航段时间清洗 · DST 安全入仓", version="2.0.0")


async def _read_csv_text(file: UploadFile) -> str:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB 上限")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是合法的 UTF-8 编码 CSV")


async def _read_three(base: UploadFile, ground: UploadFile, airline: UploadFile):
    return (await _read_csv_text(base),
            await _read_csv_text(ground),
            await _read_csv_text(airline))


def _parse_resolutions(raw: str) -> dict:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "resolutions 不是合法 JSON：" + raw[:100])
    if not isinstance(data, dict):
        raise HTTPException(400, "resolutions 必须是 {机尾: {seq: {字段: 值}}} 结构的对象")
    return data


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
# 地服 / 航司 两份更正合并
# ---------------------------------------------------------------------------

@app.get("/api/merge/sample/{name}", include_in_schema=False)
async def merge_sample(name: str):
    """合并样例：base / ground / airline 三份 CSV。"""
    allowed = {
        "base": ("base_sheet.csv", "merge_base_sheet.csv"),
        "ground": ("ground_correction.csv", "merge_ground_correction.csv"),
        "airline": ("airline_correction.csv", "merge_airline_correction.csv"),
    }
    if name not in allowed:
        raise HTTPException(404, f"未知样例 {name!r}，可选: {', '.join(allowed)}")
    src, download_name = allowed[name]
    path = MERGE_SAMPLE_DIR / src
    return FileResponse(path, media_type="text/csv; charset=utf-8",
                        filename=download_name)


@app.post("/api/merge/analyze")
async def merge_analyze(
    base: UploadFile = File(...),
    ground: UploadFile = File(...),
    airline: UploadFile = File(...),
):
    """上传底稿 + 两份更正，返回逐字段比对、自动合并结果与冲突清单。"""
    base_text, ground_text, airline_text = await _read_three(base, ground, airline)
    try:
        return analyze_merge(base_text, ground_text, airline_text)
    except MergeCsvFormatError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/merge/verify")
async def merge_verify(
    base: UploadFile = File(...),
    ground: UploadFile = File(...),
    airline: UploadFile = File(...),
    resolutions: str = Form(""),
):
    """按值班员当前选值生成合并稿，对改过的机尾重跑整链清洗并给出下载闸门。

    返回中始终带 download_allowed；未选完冲突或有机尾非唯一解时为 false，
    并在 blockers 中列出全部原因（含未通过机尾及原因）。
    """
    base_text, ground_text, airline_text = await _read_three(base, ground, airline)
    picked = _parse_resolutions(resolutions)
    try:
        verdict = verify_merge(base_text, ground_text, airline_text, picked)
    except MergeCsvFormatError as exc:
        raise HTTPException(400, str(exc))
    # 合并稿 CSV 不通过 JSON 回传，避免响应过大；下载走 /api/merge/export
    verdict.pop("merged_csv", None)
    verdict.pop("analysis", None)
    return verdict


@app.post("/api/merge/export")
async def merge_export(
    base: UploadFile = File(...),
    ground: UploadFile = File(...),
    airline: UploadFile = File(...),
    resolutions: str = Form(""),
):
    """下载当地时间合并稿 CSV。服务端重新核对：冲突选完且改过机尾全部唯一解。"""
    base_text, ground_text, airline_text = await _read_three(base, ground, airline)
    picked = _parse_resolutions(resolutions)
    try:
        csv_text = export_merge(base_text, ground_text, airline_text, picked)
    except MergeCsvFormatError as exc:
        raise HTTPException(400, str(exc))
    except MergeResolutionError as exc:
        raise HTTPException(409, "合并稿未通过核对，禁止下载：" + str(exc))
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="merged_local_sheet.csv"'},
    )
