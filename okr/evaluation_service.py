"""Monthly member evaluation queries and permission policy within the OKR module."""

from collections import defaultdict

from .evaluation import member_scores, objective_score
from .progress import attach_key_results


def can_evaluate_member(user, member):
    if user["id"] == member["id"]:
        return (
            bool(user.get("is_company_head"))
            and user.get("company_id") is not None
            and user["company_id"] == member.get("company_id")
        )
    role = user["role"]
    if role == "superAdmin":
        return True
    if user.get("company_id") != member.get("company_id"):
        return False
    if user.get("is_company_head"):
        return True
    if member.get("role") == "dept_head":
        return False
    if role == "admin":
        return True
    return (
        role == "dept_head" and user.get("department_id") is not None
        and user["department_id"] == member.get("department_id")
    )


def read_member_evaluations(db, period, user, dept_id=None, company_id=None, member_id=None):
    """Batch all member, objective and evaluation reads; never create review rows."""
    if not db.in_transaction:
        db.execute("BEGIN")
    where, params = [], []
    role = user["role"]
    if role == "superAdmin":
        if company_id is not None:
            where.append("u.company_id = ?")
            params.append(company_id)
    else:
        where.append("u.company_id = ?")
        params.append(user.get("company_id"))
        if not user.get("is_company_head") and role != "admin":
            if role == "dept_head" and user.get("department_id") is not None:
                where.append("u.department_id = ?")
                params.append(user["department_id"])
            else:
                where.append("u.id = ?")
                params.append(user["id"])
    if dept_id is not None:
        where.append("u.department_id = ?")
        params.append(dept_id)
    if member_id is not None:
        where.append("u.id = ?")
        params.append(member_id)
    # Include inactive members with historical objectives; empty active members
    # remain visible so missing goals do not look like a completed assessment.
    where.append("(u.status = 'active' OR EXISTS (SELECT 1 FROM okr_objectives o "
                 "WHERE o.owner_id = u.id AND o.type = 'personal' AND o.period = ?))")
    params.append(period)
    members = [dict(row) for row in db.execute(
        "SELECT u.id, u.name, u.role, u.company_id, u.department_id, "
        "d.name AS department_name FROM users u "
        "LEFT JOIN departments d ON d.id = u.department_id WHERE "
        + " AND ".join(where) + " ORDER BY u.department_id, u.id", params,
    )]
    if not members:
        return []
    ids = [member["id"] for member in members]
    placeholders = ",".join("?" for _ in ids)
    objectives = defaultdict(list)
    rows = list(db.execute(
        "SELECT o.id AS objective_id, o.owner_id, o.title AS objective_title, "
        "o.weight AS objective_weight, o.progress AS kr_completion_rate, "
        "r.id, r.self_score, r.manager_score, COALESCE(r.status, 'pending') AS status "
        "FROM okr_objectives o LEFT JOIN okr_monthly_reviews r "
        "ON r.objective_id = o.id AND r.year_month = ? "
        f"WHERE o.type = 'personal' AND o.period = ? AND o.owner_id IN ({placeholders}) "
        "AND o.company_id = (SELECT company_id FROM users WHERE id = o.owner_id) "
        "ORDER BY o.id", [period, period, *ids],
    ))
    progress_objects = [{"id": row["objective_id"]} for row in rows]
    attach_key_results(db, progress_objects)
    progress_by_id = {item["id"]: item["progress"] for item in progress_objects}
    for row in rows:
        item = dict(row)
        item["kr_completion_rate"] = progress_by_id[item["objective_id"]]
        item["composite_score"] = objective_score(
            item["kr_completion_rate"], item["self_score"], item["manager_score"],
        )
        objectives[item["owner_id"]].append(item)
    evaluations = {
        row["user_id"]: dict(row) for row in db.execute(
            "SELECT e.*, u.name AS reviewer_name FROM okr_member_evaluations e "
            "LEFT JOIN users u ON u.id = e.reviewer_id "
            f"WHERE e.year_month = ? AND e.user_id IN ({placeholders})", [period, *ids],
        )
    }
    results = []
    for member in members:
        items = objectives[member["id"]]
        evaluation = evaluations.get(member["id"])
        results.append({
            "user_id": member["id"], "user_name": member["name"],
            "company_id": member["company_id"],
            "department_id": member["department_id"],
            "department_name": member["department_name"] or "未分配部门",
            "year_month": period, "objectives": items, "evaluation": evaluation,
            "can_evaluate": can_evaluate_member(user, member),
            **member_scores(items, evaluation),
        })
    return results


def read_member_evaluation_report(db, period, user, member_id):
    """Read one permitted member and all report details from the same snapshot."""
    members = read_member_evaluations(db, period, user, member_id=member_id)
    if not members:
        return None
    member = members[0]
    company = db.execute("SELECT name FROM companies WHERE id = ?", (member["company_id"],)).fetchone()
    member["company_name"] = company["name"] if company else "未分配公司"
    details = [dict(row) for row in db.execute(
        "SELECT o.id, o.description, r.self_comment, r.manager_comment "
        "FROM okr_objectives o LEFT JOIN okr_monthly_reviews r "
        "ON r.objective_id=o.id AND r.year_month=? "
        "WHERE o.owner_id=? AND o.type='personal' AND o.period=? AND o.company_id=?",
        (period, member_id, period, member["company_id"]),
    )]
    attach_key_results(db, details)
    by_id = {item["id"]: item for item in details}
    for objective in member["objectives"]:
        objective.update(by_id[objective["objective_id"]])
    return member
