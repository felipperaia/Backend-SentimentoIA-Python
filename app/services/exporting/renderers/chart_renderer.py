from typing import Any


def _palette() -> list[str]:
    return [
        "#0F766E",
        "#2563EB",
        "#EA580C",
        "#475569",
        "#65A30D",
        "#BE123C",
        "#1D4ED8",
        "#92400E",
    ]


def _load_reportlab_chart_components() -> dict[str, Any]:
    try:
        from reportlab.graphics.charts.barcharts import VerticalBarChart
        from reportlab.graphics.charts.linecharts import HorizontalLineChart
        from reportlab.graphics.charts.piecharts import Pie
        from reportlab.graphics.shapes import Drawing, String
        from reportlab.lib import colors
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Exportacao PDF indisponivel no momento. Verifique a dependencia reportlab no backend."
        ) from exc

    return {
        "VerticalBarChart": VerticalBarChart,
        "HorizontalLineChart": HorizontalLineChart,
        "Pie": Pie,
        "Drawing": Drawing,
        "String": String,
        "colors": colors,
    }


def _is_non_empty_points(values: list[float]) -> bool:
    return any(float(value or 0.0) > 0.0 for value in values)


def render_chart(chart_spec: dict[str, Any], width: int = 480, height: int = 220):
    components = _load_reportlab_chart_components()
    Drawing = components["Drawing"]
    String = components["String"]
    colors = components["colors"]

    chart_type = str(chart_spec.get("type") or "").strip().lower()
    title = str(chart_spec.get("title") or "").strip()

    drawing = Drawing(width, height)
    if title:
        drawing.add(String(8, height - 18, title, fontSize=10, fillColor=colors.HexColor("#0B1220")))

    if chart_type == "pie":
        Pie = components["Pie"]
        data_points = chart_spec.get("data") or []
        labels = [str(item[0]) for item in data_points]
        values = [float(item[1] or 0.0) for item in data_points]
        if not _is_non_empty_points(values):
            return None
        pie = Pie()
        pie.x = 20
        pie.y = 10
        pie.width = width - 40
        pie.height = height - 50
        pie.data = values
        pie.labels = labels
        palette = _palette()
        for index, _ in enumerate(values):
            pie.slices[index].fillColor = colors.HexColor(palette[index % len(palette)])
        drawing.add(pie)
        return drawing

    if chart_type == "bar":
        VerticalBarChart = components["VerticalBarChart"]
        data_points = chart_spec.get("data") or []
        categories = [str(item[0]) for item in data_points]
        values = [float(item[1] or 0.0) for item in data_points]
        if not _is_non_empty_points(values):
            return None

        bar = VerticalBarChart()
        bar.x = 40
        bar.y = 20
        bar.width = width - 70
        bar.height = height - 70
        bar.data = [values]
        bar.categoryAxis.categoryNames = categories
        bar.valueAxis.valueMin = 0
        bar.groupSpacing = 12
        bar.barSpacing = 2
        bar.bars[0].fillColor = colors.HexColor("#2563EB")
        drawing.add(bar)
        return drawing

    if chart_type == "line":
        HorizontalLineChart = components["HorizontalLineChart"]
        labels = [str(label) for label in (chart_spec.get("labels") or [])]
        series = chart_spec.get("series") or []
        if not labels or not series:
            return None

        lines = []
        for entry in series:
            values = [float(value or 0.0) for value in (entry.get("values") or [])]
            if len(values) == len(labels):
                lines.append(values)

        if not lines:
            return None

        chart = HorizontalLineChart()
        chart.x = 40
        chart.y = 20
        chart.height = height - 70
        chart.width = width - 70
        chart.data = lines
        chart.categoryAxis.categoryNames = labels
        chart.valueAxis.valueMin = 0
        palette = _palette()
        for index in range(len(lines)):
            chart.lines[index].strokeColor = colors.HexColor(palette[index % len(palette)])
            chart.lines[index].strokeWidth = 2
        drawing.add(chart)
        return drawing

    return None

