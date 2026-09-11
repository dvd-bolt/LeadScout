"""Generate styled LeadScout resume-audit PDF reports."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

logger = logging.getLogger(__name__)


def _safe(value: object) -> str:
    return escape(str(value or ""))


FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"

_REGULAR_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_BOLD_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]

_regular_font_path = next((path for path in _REGULAR_FONT_CANDIDATES if os.path.exists(path)), None)
_bold_font_path = next((path for path in _BOLD_FONT_CANDIDATES if os.path.exists(path)), None)

if _regular_font_path and _bold_font_path:
    try:
        pdfmetrics.registerFont(TTFont("ArialCyr", _regular_font_path))
        pdfmetrics.registerFont(TTFont("ArialCyr-Bold", _bold_font_path))
        FONT_REGULAR = "ArialCyr"
        FONT_BOLD = "ArialCyr-Bold"
        logger.info(
            "Успешно зарегистрированы шрифты %s / %s для ReportLab PDF.",
            _regular_font_path,
            _bold_font_path,
        )
    except Exception as exc:
        logger.warning(
            "Не удалось зарегистрировать кириллические шрифты (%s). Используются стандартные.",
            type(exc).__name__,
        )


def generate_resume_audit_pdf(audit_data: dict, output_path: str) -> str:
    """Generate a styled PDF report from validated resume-audit data."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName=FONT_BOLD,
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#1E293B"),
        alignment=0,
        spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "DocSubTitle",
        parent=styles["Normal"],
        fontName=FONT_REGULAR,
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#64748B"),
        spaceAfter=15,
    )
    heading_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Normal"],
        fontName=FONT_BOLD,
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#0F172A"),
        spaceBefore=12,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "BodyTextCustom",
        parent=styles["Normal"],
        fontName=FONT_REGULAR,
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#334155"),
    )
    bullet_style = ParagraphStyle("BulletCustom", parent=body_style, leftIndent=12, spaceAfter=4)

    story = [
        Paragraph("<b>LeadScout AI</b> — Отчет аудита IT-резюме", title_style),
        Paragraph(
            f"Профессия: <b>{_safe(audit_data.get('profession_name', 'IT-Специалист'))}</b> | "
            f"Дата проверки: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            subtitle_style,
        ),
        HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#CBD5E1"), spaceAfter=15),
    ]

    score = audit_data.get("overall_score", 0)
    if score >= 80:
        badge_bg = colors.HexColor("#DCFCE7")
        badge_text_color = colors.HexColor("#166534")
        status_label = "ОТЛИЧНЫЙ РЕЗУЛЬТАТ (Топ-10% ATS)"
    elif score >= 60:
        badge_bg = colors.HexColor("#FEF9C3")
        badge_text_color = colors.HexColor("#854D0E")
        status_label = "ХОРОШИЙ ПОТЕНЦИАЛ (Требуются доработки)"
    else:
        badge_bg = colors.HexColor("#FEE2E2")
        badge_text_color = colors.HexColor("#991B1B")
        status_label = "ТРЕБУЮТСЯ СРОЧНЫЕ ИСПРАВЛЕНИЯ"

    score_table = Table(
        [
            [
                Paragraph(
                    f"<font size=28 fontName='{FONT_BOLD}'><b>{score} / 100</b></font><br/>"
                    f"<font color='{badge_text_color.hexval()}'><b>{status_label}</b></font>",
                    body_style,
                ),
                Paragraph(
                    f"<b>Вывод:</b><br/>{_safe(audit_data.get('summary_text', 'Анализ завершен успешно.'))}",
                    body_style,
                ),
            ]
        ],
        colWidths=[200, 320],
    )
    score_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), badge_bg),
                ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#F8FAFC")),
                ("ALIGN", (0, 0), (0, 0), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("PADDING", (0, 0), (-1, -1), 10),
                ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
                ("INNERGRID", (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
            ]
        )
    )
    story.extend([score_table, Spacer(1, 15)])

    story.append(Paragraph("Детализация оценок по 5 ключевым категориям", heading_style))
    category_scores = audit_data.get("category_scores", {})
    category_rows = [
        ["Категория оценки", "Вес", "Балл", "Визуальная шкала"],
        _category_row("Hard Skills & Стек технологий", "30%", "hard_skills", category_scores),
        _category_row("Impact & Метрики (Google XYZ / STAR)", "25%", "impact_metrics", category_scores),
        _category_row("Техническая читаемость & ATS Формат", "15%", "parseability", category_scores),
        _category_row("Хронология & Карьерный трек", "15%", "timeline", category_scores),
        _category_row("Стиль, лаконичность & Soft Skills", "15%", "style", category_scores),
    ]
    table_rows = []
    for index, row in enumerate(category_rows):
        if index == 0:
            table_rows.append([Paragraph(f"<b>{value}</b>", body_style) for value in row])
        else:
            table_rows.append(
                [
                    Paragraph(row[0], body_style),
                    Paragraph(row[1], body_style),
                    Paragraph(f"<b>{row[2]}</b>", body_style),
                    Paragraph(row[3], body_style),
                ]
            )
    category_table = Table(table_rows, colWidths=[200, 50, 80, 190])
    category_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("PADDING", (0, 0), (-1, -1), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ]
        )
    )
    story.extend([category_table, Spacer(1, 15)])

    penalties = audit_data.get("penalties", [])
    if penalties:
        story.append(Paragraph("Выявленные барьеры ATS и риски", heading_style))
        story.extend(Paragraph(f"- {_safe(penalty)}", bullet_style) for penalty in penalties)
        story.append(Spacer(1, 10))

    recommendations = audit_data.get("top_recommendations", [])
    if recommendations:
        story.append(Paragraph("Топ-3 приоритетных шага к улучшению", heading_style))
        story.extend(
            Paragraph(f"<b>{index}.</b> {_safe(recommendation)}", bullet_style)
            for index, recommendation in enumerate(recommendations, 1)
        )
        story.append(Spacer(1, 10))

    insights = audit_data.get("insights", [])
    if insights:
        story.append(Paragraph("Полная матрица оптимизации резюме", heading_style))
        tier_labels = {
            1: "Tier 1: Критические блокеры",
            2: "Tier 2: Оптимизация контента и метрики XYZ",
            3: "Tier 3: Стилистическая полировка",
        }
        for tier, label in tier_labels.items():
            tier_items = [item for item in insights if str(item.get("tier")) == str(tier)]
            if not tier_items:
                continue
            story.append(Paragraph(f"<b>{label}</b>", body_style))
            for item in tier_items:
                story.append(
                    Paragraph(
                        f"- <b>{_safe(item.get('title', ''))}</b> "
                        f"({_safe(item.get('score_impact', ''))}): {_safe(item.get('description', ''))}",
                        bullet_style,
                    )
                )
            if tier != 3:
                story.append(Spacer(1, 6))

    story.extend(
        [
            Spacer(1, 20),
            HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CBD5E1"), spaceAfter=10),
            Paragraph(
                "Сгенерировано автоматически сервисом <b>LeadScout AI</b>. Спецификация скоринга резюме v2.0.",
                subtitle_style,
            ),
        ]
    )
    doc.build(story)
    logger.info("PDF-отчет аудита успешно сформирован")
    return output_path


def _category_row(label: str, weight: str, key: str, scores: dict) -> list[str]:
    value = scores.get(key, 0)
    return [label, weight, f"{value} / 100", _get_progress_bar(value)]


def _get_progress_bar(value: int) -> str:
    """Return a ReportLab-safe textual progress bar."""
    value = max(0, min(100, value))
    filled = round(value / 10)
    bar = "█" * filled + "░" * (10 - filled)
    if value >= 80:
        color_hex = "#166534"
    elif value >= 60:
        color_hex = "#854D0E"
    else:
        color_hex = "#991B1B"
    return f"<font color='{color_hex}'>{bar}</font> {value}%"
