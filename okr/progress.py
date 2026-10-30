"""Canonical KR and objective progress projection, shared by OKR views and evaluations."""

from .db import dicts_from_rows


def calculate_kr_progress(current_value, target_value) -> float:
    target = float(target_value or 0)
    current = float(current_value or 0)
    if target <= 0:
        return 0.0
    return round(max(0.0, min(100.0, current / target * 100)), 1)


def attach_key_results(db, objectives: list):
    """批量附加 KR，并以 current/target 作为展示进度的最终事实来源。"""
    obj_ids = [o["id"] for o in objectives]
    if not obj_ids:
        return
    ph = ",".join("?" * len(obj_ids))
    krs = dicts_from_rows(
        db.execute(
            f"SELECT * FROM okr_key_results WHERE objective_id IN ({ph}) ORDER BY id",
            obj_ids,
        ).fetchall()
    )
    kr_map: dict = {}
    for kr in krs:
        kr["progress"] = calculate_kr_progress(
            kr.get("current_value"), kr.get("target_value")
        )
        kr_map.setdefault(kr["objective_id"], []).append(kr)
    for o in objectives:
        attached = kr_map.get(o["id"], [])
        o["key_results"] = attached
        total_weight = sum(float(kr.get("weight") or 0) for kr in attached)
        o["progress"] = round(
            sum(float(kr["progress"]) * float(kr.get("weight") or 0) for kr in attached)
            / total_weight,
            1,
        ) if total_weight > 0 else 0.0
