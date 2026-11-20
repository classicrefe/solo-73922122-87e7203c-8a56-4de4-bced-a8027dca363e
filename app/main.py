"""FastAPI 入口：OCR 修订后实体标签迁移工具。

流程：上传三件套 -> 分析（自动迁移/多落点/无法保留）-> 逐项选择 ->
确认（整份选择须被同一最优对齐支持，且 revision 未过期）-> 下载结果。
"""

from __future__ import annotations

import json
import os
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .alignment import analyze_labels, check_joint
from .schemas import (ValidationError, normalize_labels, normalize_selections,
                      validate_text)
from .stitch import Segment, StitchValidationError, normalize_request, plan_stitch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="OCR 标签迁移工具")


@app.exception_handler(ValidationError)
async def validation_handler(_req: Request, exc: ValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(StitchValidationError)
async def stitch_validation_handler(_req: Request, exc: StitchValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------------- 请求模型 ----------------

class InputsIn(BaseModel):
    original: str
    revised: str
    labels: object  # list 或 {"labels": [...]}，由 normalize_labels 校验


class SelectionsIn(BaseModel):
    revision: int
    selections: object


class ConfirmIn(BaseModel):
    revision: int


class StitchIn(BaseModel):
    segments: object    # [{session_id, revision}]，由 normalize_request 校验
    seams: object       # [{candidates: [int, ...]}]


# ---------------- 工具 ----------------

def _validate_inputs(payload: InputsIn):
    original = validate_text(payload.original, "原文")
    revised = validate_text(payload.revised, "修订文")
    labels = normalize_labels(payload.labels, original)
    return original, revised, labels


def _must_get_session(sid: str) -> dict:
    sess = db.get_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return sess


def _state(sess: dict) -> dict:
    analysis = analyze_labels(sess["original"], sess["revised"], sess["labels"])
    result = db.get_result(sess["id"], sess["revision"])
    return {
        "session_id": sess["id"],
        "revision": sess["revision"],
        "original": sess["original"],
        "revised": sess["revised"],
        "labels": sess["labels"],
        "analysis": analysis,
        "selections": db.get_selections(sess["id"], sess["revision"]),
        "has_result": result is not None,
        "download_url": f"/api/sessions/{sess['id']}/download" if result else None,
    }


# ---------------- 页面 ----------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------------- API ----------------

@app.post("/api/sessions", status_code=201)
def create_session(payload: InputsIn):
    original, revised, labels = _validate_inputs(payload)
    sess = db.create_session(original, revised, labels)
    return _state(sess)


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    return _state(_must_get_session(sid))


@app.put("/api/sessions/{sid}/inputs")
def update_inputs(sid: str, payload: InputsIn):
    original, revised, labels = _validate_inputs(payload)
    sess = db.update_inputs(sid, original, revised, labels)
    if sess is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return _state(sess)


@app.put("/api/sessions/{sid}/selections")
def put_selections(sid: str, payload: SelectionsIn):
    """保存选择。版本检查与写入在同一事务：若另一窗口正修改输入，
    要么本请求先落库（对方随后使其失效），要么直接看到新版本返回 409。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="会话不存在")
        if row["revision"] != payload.revision:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": "stale_revision",
                        "message": "输入已在别处修改，旧选择已失效，请基于最新分析重新选择",
                        "current_revision": row["revision"]})
        labels = json.loads(row["labels"])
        sels = normalize_selections(payload.selections, labels)
        # 落点必须真的在可能集合里，防止绕过前端伪造
        analysis = analyze_labels(row["original"], row["revised"], labels)
        possible_by_id = {l["id"]: l["possible"] for l in analysis["labels"]}
        for lid, sel in sels.items():
            if sel["choice"] == "position" and sel["position"] not in possible_by_id[lid]:
                conn.rollback()
                raise ValidationError(
                    f"标签 {lid!r} 的落点 {sel['position']} 不在可能位置集合中")
        conn.execute(
            "INSERT INTO selections(session_id, revision, data, updated_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(session_id, revision) DO UPDATE SET"
            " data=excluded.data, updated_at=excluded.updated_at",
            (sid, row["revision"], json.dumps(sels, ensure_ascii=False),
             db._now()))
        conn.commit()
        return {"ok": True, "revision": row["revision"]}
    except (HTTPException, ValidationError):
        raise
    finally:
        conn.close()


@app.post("/api/sessions/{sid}/confirm")
def confirm(sid: str, payload: ConfirmIn):
    """确认发布。整个"校验 revision + 校验联合一致 + 写结果"在一个
    IMMEDIATE 事务里：输入一旦被别的窗口改过（哪怕又改回去），revision
    必然不同，旧选择/在途确认都会被拒绝；任何失败都不会留下结果行。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="会话不存在")
        if row["revision"] != payload.revision:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": "stale_revision",
                        "message": "输入已在别处修改，本次确认已作废，请重新分析并选择",
                        "current_revision": row["revision"]})

        original, revised = row["original"], row["revised"]
        labels = json.loads(row["labels"])
        srow = conn.execute(
            "SELECT data FROM selections WHERE session_id=? AND revision=?",
            (sid, row["revision"])).fetchone()
        sels = json.loads(srow["data"]) if srow else {}

        # 幂等：同一 revision 已成功确认过，直接返回既有结果
        existing = conn.execute(
            "SELECT data FROM results WHERE session_id=? AND revision=?",
            (sid, row["revision"])).fetchone()
        if existing is not None:
            conn.commit()
            return {"ok": True, "already": True,
                    "result": json.loads(existing["data"]),
                    "download_url": f"/api/sessions/{sid}/download"}

        analysis = analyze_labels(original, revised, labels)
        info_by_id = {l["id"]: l for l in analysis["labels"]}

        kept, discarded, unresolved = [], [], []
        for lab in labels:
            lid = lab["id"]
            info = info_by_id[lid]
            sel = sels.get(lid)
            if info["status"] == "impossible":
                discarded.append(lid)          # 无法保留：只能丢弃
                continue
            if info["status"] == "auto":
                if sel and sel.get("choice") == "discard":
                    discarded.append(lid)
                else:
                    kept.append((lab["start"], lab["end"],
                                 info["possible"][0], lid))
                continue
            # choice：必须显式选择
            if sel is None:
                unresolved.append(lid)
            elif sel.get("choice") == "discard":
                discarded.append(lid)
            elif sel.get("choice") == "position" and \
                    sel.get("position") in info["possible"]:
                kept.append((lab["start"], lab["end"], sel["position"], lid))
            else:
                conn.rollback()
                raise HTTPException(
                    status_code=400,
                    detail=f"标签 {lid!r} 的选择无效：落点不在可能位置集合中")

        if unresolved:
            conn.rollback()
            raise HTTPException(
                status_code=422,
                detail={"code": "unresolved",
                        "message": "存在未处理的标签，请逐项选择位置或丢弃",
                        "labels": unresolved})

        kept.sort(key=lambda x: x[0])
        ok, conflict_idx = check_joint(
            original, revised, [(a, b, p) for a, b, p, _ in kept],
            analysis["distance"])
        if not ok:
            conn.rollback()
            conflict_ids = [kept[i][3] for i in conflict_idx]
            raise HTTPException(
                status_code=409,
                detail={"code": "conflict",
                        "message": "整份选择无法被同一个最优对齐支持，请调整下列冲突标签",
                        "conflicting_labels": conflict_ids})

        # 成功：原子写入结果，随后才允许下载
        out_labels = [{
            "id": lid,
            "start": p,
            "end": p + (b - a),
            "text": revised[p:p + (b - a)],
        } for a, b, p, lid in kept]
        result = {
            "revised": revised,
            "labels": out_labels,
            "meta": {
                "session_id": sid,
                "revision": row["revision"],
                "edit_distance": analysis["distance"],
                "migrated": len(out_labels),
                "discarded": discarded,
            },
        }
        conn.execute(
            "INSERT INTO results(session_id, revision, data, created_at)"
            " VALUES (?,?,?,?)",
            (sid, row["revision"], json.dumps(result, ensure_ascii=False),
             db._now()))
        conn.commit()
        return {"ok": True, "already": False, "result": result,
                "download_url": f"/api/sessions/{sid}/download"}
    except HTTPException:
        raise
    finally:
        conn.close()


@app.get("/api/sessions/{sid}/download")
def download(sid: str):
    """只有当前 revision 已成功确认才提供下载；输入改过后旧结果不可下载。"""
    sess = _must_get_session(sid)
    result = db.get_result(sid, sess["revision"])
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="当前输入尚未确认成功，无法下载；失败不会留下任何结果")
    return JSONResponse(
        content=result["data"],
        headers={
            "Content-Disposition":
                f'attachment; filename="labels_revised_r{sess["revision"]}.json"'
        })


# ---------------- 拼稿 ----------------

@app.post("/api/stitches", status_code=200)
def create_stitch(payload: StitchIn):
    """按阅读顺序拼合多个已确认会话。

    所有会话在同一个 IMMEDIATE 事务、同一次数据库快照中读取：
    会话不存在 / revision 过期 / 对应 revision 未确认，整次停下并指出
    是第几段的哪个会话。只有“唯一最短合法拼法”才落库并给出下载地址；
    并列最短或无解都不生成任何文件（数据库行）。
    """
    refs, candidates_per_seam = normalize_request(
        payload.segments, payload.seams)

    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        segments: list[Segment] = []
        for index, ref in enumerate(refs, start=1):
            row = conn.execute(
                "SELECT id, revision FROM sessions WHERE id=?",
                (ref["session_id"],)).fetchone()
            if row is None:
                conn.rollback()
                raise HTTPException(
                    status_code=404,
                    detail={"code": "session_not_found",
                            "message": f"第 {index} 段的会话 {ref['session_id']!r} 不存在",
                            "session_index": index,
                            "session_id": ref["session_id"]})
            if row["revision"] != ref["revision"]:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={"code": "stale_revision",
                            "message": f"第 {index} 段会话 {ref['session_id']!r} 的版本已过期"
                                       f"（提交 r{ref['revision']}，当前 r{row['revision']}），整次拼稿中止",
                            "session_index": index,
                            "session_id": ref["session_id"],
                            "submitted_revision": ref["revision"],
                            "current_revision": row["revision"]})
            rrow = conn.execute(
                "SELECT data FROM results WHERE session_id=? AND revision=?",
                (ref["session_id"], ref["revision"])).fetchone()
            if rrow is None:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={"code": "unconfirmed",
                            "message": f"第 {index} 段会话 {ref['session_id']!r} 的 r{ref['revision']} "
                                       "尚未确认成功，整次拼稿中止",
                            "session_index": index,
                            "session_id": ref["session_id"],
                            "revision": ref["revision"]})
            result_data = json.loads(rrow["data"])
            segments.append(Segment(
                session_index=index,
                session_id=ref["session_id"],
                revision=ref["revision"],
                text=result_data["revised"],
                labels=result_data["labels"],
            ))

        plan = plan_stitch(segments, candidates_per_seam)

        if plan["status"] != "unique":
            # 并列最短 / 无解：不落任何行
            conn.rollback()
            plan["download_url"] = None
            plan["stitch_id"] = None
            return plan

        stitch_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO stitches(id, data, created_at) VALUES (?,?,?)",
            (stitch_id, json.dumps(plan["result"], ensure_ascii=False),
             db._now()))
        conn.commit()
        plan["stitch_id"] = stitch_id
        plan["download_url"] = f"/api/stitches/{stitch_id}/download"
        return plan
    except HTTPException:
        raise
    finally:
        conn.close()


@app.get("/api/stitches/{stitch_id}/download")
def download_stitch(stitch_id: str):
    """只有唯一最短合法拼法落过库，才能下载。"""
    stitch = db.get_stitch(stitch_id)
    if stitch is None:
        raise HTTPException(
            status_code=404,
            detail="拼稿结果不存在：只有唯一最短的合法拼法才会生成文件，"
                   "并列最短与无解均不生成")
    return JSONResponse(
        content=stitch["data"],
        headers={
            "Content-Disposition":
            f'attachment; filename="stitched_{stitch_id[:8]}.json"'
        })


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
