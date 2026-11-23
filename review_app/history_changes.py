from . import history_models as model
from . import query_service as query
from .storage import connect_read


def resume_token(exercise_id: str, sequence: int) -> str:
    return f"h1.{exercise_id}.{sequence}"


def list_changes(request: model.ChangeRequest) -> model.ChangePage:
    query.check_scope(request.exercise_id)
    cursor = 0
    if request.after:
        _, exercise, position = request.after.split(".")
        cursor = int(position)
        if exercise != request.exercise_id or cursor > 2**63 - 1:
            raise query.invalid("The resume token does not belong to this exercise.")

    with connect_read() as conn:
        if not conn.execute("SELECT 1 FROM exercise WHERE id = %s", (request.exercise_id,)).fetchone():
            raise query.not_found("Exercise history not found.")
        clock = conn.execute("SELECT sequence FROM history_change_clock WHERE exercise_id = %s",
                             (request.exercise_id,)).fetchone()
        if cursor > (clock["sequence"] if clock else 0):
            raise query.invalid("The resume token is ahead of the available change feed.")
        rows = conn.execute(
            """
            SELECT sequence, event, ref, version, available_at FROM history_change
            WHERE exercise_id = %s AND sequence > %s ORDER BY sequence LIMIT %s
            """,
            (request.exercise_id, cursor, request.limit + 1),
        ).fetchall()
    selected = rows[:request.limit]
    return model.ChangePage(exercise_id=request.exercise_id, changes=selected,
                            resume_token=resume_token(request.exercise_id, selected[-1]["sequence"] if selected else cursor),
                            has_more=len(rows) > request.limit)
