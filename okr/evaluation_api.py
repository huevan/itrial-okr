"""HTTP boundary for the member-level monthly behavior and red-line review."""

import json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from .auth import get_current_user
from .db import get_db
from .evaluation_service import can_evaluate_member, read_member_evaluations, read_member_evaluation_report
from .evaluation_report import build_member_evaluation_pdf
from .models import MemberEvaluationSubmit

evaluation_router = APIRouter()
MONTH_PATTERN = r"^[0-9]{4}-(?:0[1-9]|1[0-2])$"


@evaluation_router.get("/api/okr/evaluations/monthly")
async def list_member_evaluations(
    period: str = Query(..., pattern=MONTH_PATTERN),
    dept_id: int = Query(None),
    company_id: int = Query(None),
    user: dict = Depends(get_current_user),
):
    with get_db() as db:
        members = read_member_evaluations(db, period, user, dept_id, company_id)
    return {"period": period, "members": members}


@evaluation_router.get("/api/okr/evaluations/monthly/{member_id}/pdf")
def export_member_evaluation_pdf(
    member_id: int,
    period: str = Query(..., pattern=MONTH_PATTERN),
    user: dict = Depends(get_current_user),
):
    # A sync handler keeps PDF rendering off the application's async event loop.
    with get_db() as db:
        member = read_member_evaluation_report(db, period, user, member_id)
    if member is None:
        raise HTTPException(404, "成员不存在或无权查看该成员")
    if member["status"] != "completed":
        raise HTTPException(409, "该员工当月总评尚未完成，无法导出评估表")
    try:
        content = build_member_evaluation_pdf(member)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    filename = quote(f"{member['user_name']}_{period}_目标评估表.pdf", safe="")
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="OKR-Evaluation-{period}-{member_id}.pdf"; filename*=UTF-8\'\'{filename}',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@evaluation_router.put("/api/okr/evaluations/monthly/{member_id}")
async def submit_member_evaluation(
    member_id: int,
    req: MemberEvaluationSubmit,
    user: dict = Depends(get_current_user),
):
    if req.redline_factor == 0 and not req.redline_reason.strip():
        raise HTTPException(400, "红线系数为 0 时请说明重大失误或主观过错")
    with get_db() as db:
        # Serialize revision checking and the write across concurrent reviewers.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT id, role, company_id, department_id FROM users WHERE id = ?", (member_id,),
        ).fetchone()
        if not row or not can_evaluate_member(user, dict(row)):
            raise HTTPException(403, "无权评估该成员，请由有权限的主管评分")
        existing = db.execute(
            "SELECT * FROM okr_member_evaluations WHERE user_id = ? AND year_month = ?",
            (member_id, req.year_month),
        ).fetchone()
        revision = existing["revision"] if existing else 0
        if revision != req.expected_revision:
            raise HTTPException(409, "评分已被其他人更新，请刷新后重新填写")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "INSERT INTO okr_member_evaluations "
            "(user_id, year_month, satisfaction_score, initiative_score, redline_factor, "
            "redline_reason, comment, reviewer_id, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, year_month) DO UPDATE SET "
            "satisfaction_score=excluded.satisfaction_score, initiative_score=excluded.initiative_score, "
            "redline_factor=excluded.redline_factor, redline_reason=excluded.redline_reason, "
            "comment=excluded.comment, reviewer_id=excluded.reviewer_id, "
            "revision=excluded.revision, updated_at=excluded.updated_at",
            (member_id, req.year_month, req.satisfaction_score, req.initiative_score,
             req.redline_factor, req.redline_reason.strip(), req.comment.strip(), user["id"],
             revision + 1, now, now),
        )
        updated = dict(db.execute(
            "SELECT * FROM okr_member_evaluations WHERE user_id = ? AND year_month = ?",
            (member_id, req.year_month),
        ).fetchone())
        # Retain previous values when a supervisor corrects an assessment.
        detail = json.dumps({"year_month": req.year_month,
                             "before": dict(existing) if existing else None,
                             "after": updated}, ensure_ascii=False)
        db.execute(
            "INSERT INTO audit_logs(user_id,user_name,action,target_type,target_id,detail) "
            "VALUES (?, ?, 'member_evaluation', 'user', ?, ?)",
            (user["id"], user["name"], member_id, detail),
        )
    return {"ok": True, "evaluation": updated}
