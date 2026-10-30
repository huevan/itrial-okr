"""Pure monthly member scoring rules; no transport or persistence dependencies."""

TASK_WEIGHT = 0.7
BEHAVIOR_WEIGHT = 0.3


def objective_score(progress, self_score, manager_score):
    """Self review 20% + manager review 80%; progress remains display-only.

    Keep the progress argument for existing callers and keyword compatibility.
    """
    if self_score is None or manager_score is None:
        return None
    return round(self_score * 0.2 + manager_score * 0.8, 1)


def member_scores(objectives, evaluation=None):
    """Only publish a total when every personal O and the monthly review are ready.

    Keep full precision between weighting stages. A missing review is different
    from a real zero score, including a confirmed red-line factor of zero.
    """
    completed = [
        item for item in objectives
        if item.get("status") == "completed" and item.get("composite_score") is not None
    ]
    total_weight = sum(float(item.get("objective_weight", 1)) for item in objectives)
    task_ready = bool(objectives) and len(completed) == len(objectives) and total_weight > 0
    task = None
    if task_ready:
        task = sum(
            item["composite_score"] * float(item.get("objective_weight", 1))
            for item in objectives
        ) / total_weight
    behavior = None
    factor = None
    if evaluation:
        behavior = (evaluation["satisfaction_score"] + evaluation["initiative_score"]) / 2
        factor = evaluation["redline_factor"]
    total = None
    if task is not None and behavior is not None:
        total = round((task * TASK_WEIGHT + behavior * BEHAVIOR_WEIGHT) * factor, 1)
    return {
        "task_score": round(task, 1) if task is not None else None,
        "task_contribution": round(task * TASK_WEIGHT, 1) if task is not None else None,
        "behavior_score": round(behavior, 1) if behavior is not None else None,
        "behavior_contribution": round(behavior * BEHAVIOR_WEIGHT, 1) if behavior is not None else None,
        "redline_factor": factor,
        "total_score": total,
        "objective_count": len(objectives),
        "completed_objective_count": len(completed),
        "total_objective_weight": total_weight,
        "status": "completed" if total is not None else (
            "no_objectives" if not objectives else "pending_tasks" if not task_ready else "pending_behavior"
        ),
    }
