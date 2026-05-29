import csv
import io
from typing import Any

from fastapi.responses import StreamingResponse

from app.services.exporting.contracts import ExportContext


def render_csv_document(document_model: dict[str, Any], _: ExportContext) -> StreamingResponse:
    columns = document_model.get("columns") or []
    rows = document_model.get("rows") or []
    filename = str(document_model.get("filename") or "export.csv")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([str(column.get("label") or column.get("key") or "") for column in columns])

    for row in rows:
        writer.writerow([row.get(str(column.get("key") or ""), "") for column in columns])

    payload = buffer.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

