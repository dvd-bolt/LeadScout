"""
LeadScout AI — Модуль генерации PDF-отчетов аудита резюме (ReportLab).
Создает стилизованные PDF-документы с оценками, визуальными баллами, матрицей рекомендаций и анализом ATS.
"""

import os
import logging
from datetime import datetime
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
)
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logger = logging.getLogger(__name__)

# Регистрация кириллических шрифтов (Windows Arial / Linux DejaVu / Liberation)
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"

regular_candidates = [
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
bold_candidates = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]

found_reg = next((p for p in regular_candidates if os.path.exists(p)), None)
found_bold = next((p for p in bold_candidates if os.path.exists(p)), None)

if found_reg and found_bold:
    try:
        pdfmetrics.registerFont(TTFont("ArialCyr", found_reg))
        pdfmetrics.registerFont(TTFont("ArialCyr-Bold", found_bold))
        FONT_REGULAR = "ArialCyr"
        FONT_BOLD = "ArialCyr-Bold"
        logger.info("Успешно зарегистрированы шрифты %s / %s для ReportLab PDF.", found_reg, found_bold)
    except Exception as e:
        logger.warning("Не удалось зарегистрировать кириллические шрифты (%s). Используются стандартные.", e)



def generate_resume_audit_pdf(audit_data: dict, output_path: str) -> str:
    """
    Генерирует стильный PDF-отчет результатов аудита IT-резюме.
    
    audit_data должен содержать:
    - profession_name: str
    - overall_score: int (0-100)
    - category_scores: dict (hard_skills, impact_metrics, parseability, timeline, style)
    - penalties: list[str]
    - top_recommendations: list[str]
    - insights: list[dict] (tier, title, description, score_impact)
    - summary_text: str
    """
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    # Стили текста
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName=FONT_BOLD,
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#1E293B"),
        alignment=0,
        spaceAfter=4
    )

    subtitle_style = ParagraphStyle(
        "DocSubTitle",
        parent=styles["Normal"],
        fontName=FONT_REGULAR,
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#64748B"),
        spaceAfter=15
    )

    heading_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Normal"],
        fontName=FONT_BOLD,
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#0F172A"),
        spaceBefore=12,
        spaceAfter=8
    )

    body_style = ParagraphStyle(
        "BodyTextCustom",
        parent=styles["Normal"],
        fontName=FONT_REGULAR,
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#334155")
    )

    bullet_style = ParagraphStyle(
        "BulletCustom",
        parent=body_style,
        leftIndent=12,
        spaceAfter=4
    )

    story = []

    # 1. Шапка документа
    story.append(Paragraph("<b>LeadScout AI</b> — Отчет аудита IT-резюме", title_style))
    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    profession = audit_data.get("profession_name", "IT-Специалист")
    story.append(Paragraph(f"Профессия: <b>{profession}</b> | Дата проверки: {date_str}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#CBD5E1"), spaceAfter=15))

    # 2. Карточка итогового балла
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

    score_card_data = [
        [
            Paragraph(f"<font size=28 fontName='{FONT_BOLD}'><b>{score} / 100</b></font><br/><font color='{badge_text_color.hexval()}'><b>{status_label}</b></font>", body_style),
            Paragraph(f"<b>ИИ-Резюме вывода:</b><br/>{audit_data.get('summary_text', 'Анализ завершен успешно.')}", body_style)
        ]
    ]

    score_table = Table(score_card_data, colWidths=[200, 320])
    score_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), badge_bg),
        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor("#F8FAFC")),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 10),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
        ('INNERGRID', (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
    ]))
    story.append(score_table)
    story.append(Spacer(1, 15))

    # 3. Детализация по 5 категориям
    story.append(Paragraph("📊 Детализация оценок по 5 ключевым категориям", heading_style))
    
    cats = audit_data.get("category_scores", {})
    cat_rows = [
        ["Категория оценки", "Вес", "Балл", "Визуальная шкала"],
        ["Hard Skills & Стек технологий", "30%", f"{cats.get('hard_skills', 0)} / 100", _get_progress_bar(cats.get('hard_skills', 0))],
        ["Impact & Метрики (Google XYZ / STAR)", "25%", f"{cats.get('impact_metrics', 0)} / 100", _get_progress_bar(cats.get('impact_metrics', 0))],
        ["Техническая читаемость & ATS Формат", "15%", f"{cats.get('parseability', 0)} / 100", _get_progress_bar(cats.get('parseability', 0))],
        ["Хронология & Карьерный трек", "15%", f"{cats.get('timeline', 0)} / 100", _get_progress_bar(cats.get('timeline', 0))],
        ["Стиль, лаконичность & Soft Skills", "15%", f"{cats.get('style', 0)} / 100", _get_progress_bar(cats.get('style', 0))],
    ]

    cat_table_data = []
    for i, row in enumerate(cat_rows):
        if i == 0:
            cat_table_data.append([
                Paragraph(f"<b>{row[0]}</b>", body_style),
                Paragraph(f"<b>{row[1]}</b>", body_style),
                Paragraph(f"<b>{row[2]}</b>", body_style),
                Paragraph(f"<b>{row[3]}</b>", body_style),
            ])
        else:
            cat_table_data.append([
                Paragraph(row[0], body_style),
                Paragraph(row[1], body_style),
                Paragraph(f"<b>{row[2]}</b>", body_style),
                Paragraph(row[3], body_style),
            ])

    cat_table = Table(cat_table_data, colWidths=[200, 50, 80, 190])
    cat_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor("#0F172A")),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
    ]))
    story.append(cat_table)
    story.append(Spacer(1, 15))

    # 4. Штрафы и выявленные риски ATS
    penalties = audit_data.get("penalties", [])
    if penalties:
        story.append(Paragraph("⚠️ Выявленные барьеры ATS и риски", heading_style))
        for pen in penalties:
            story.append(Paragraph(f"• {pen}", bullet_style))
        story.append(Spacer(1, 10))

    # 5. Топ-3 Главные рекомендации
    top_recs = audit_data.get("top_recommendations", [])
    if top_recs:
        story.append(Paragraph("💡 Топ-3 приоритетных шагов к улучшению", heading_style))
        for idx, rec in enumerate(top_recs, 1):
            story.append(Paragraph(f"<b>{idx}.</b> {rec}", bullet_style))
        story.append(Spacer(1, 10))

    # 6. Матрица пошаговых рекомендаций Actionable Insights
    insights = audit_data.get("insights", [])
    if insights:
        story.append(Paragraph("🚀 Полная матрица оптимизации резюме (Actionable Insights)", heading_style))
        
        # Группировка по Tier
        tier_1 = [ins for ins in insights if ins.get("tier") == 1 or ins.get("tier") == "1"]
        tier_2 = [ins for ins in insights if ins.get("tier") == 2 or ins.get("tier") == "2"]
        tier_3 = [ins for ins in insights if ins.get("tier") == 3 or ins.get("tier") == "3"]

        if tier_1:
            story.append(Paragraph("<b>🔴 Tier 1: Критические блокеры (Исправить незамедлительно)</b>", body_style))
            for item in tier_1:
                title = item.get("title", "")
                desc = item.get("description", "")
                impact = item.get("score_impact", "")
                story.append(Paragraph(f"• <b>{title}</b> ({impact}): {desc}", bullet_style))
            story.append(Spacer(1, 6))

        if tier_2:
            story.append(Paragraph("<b>🟡 Tier 2: Оптимизация контента & Метрики XYZ</b>", body_style))
            for item in tier_2:
                title = item.get("title", "")
                desc = item.get("description", "")
                impact = item.get("score_impact", "")
                story.append(Paragraph(f"• <b>{title}</b> ({impact}): {desc}", bullet_style))
            story.append(Spacer(1, 6))

        if tier_3:
            story.append(Paragraph("<b>🟢 Tier 3: Стилистическая полировка</b>", body_style))
            for item in tier_3:
                title = item.get("title", "")
                desc = item.get("description", "")
                impact = item.get("score_impact", "")
                story.append(Paragraph(f"• <b>{title}</b> ({impact}): {desc}", bullet_style))

    # Подвал
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CBD5E1"), spaceAfter=10))
    story.append(Paragraph("Сгенерировано автоматически сервисом <b>LeadScout AI</b>. Спецификация скоринга резюме v2.0.", subtitle_style))

    # Сборка PDF
    doc.build(story)
    logger.info("PDF-отчет аудита успешно сформирован по адресу: %s", output_path)
    return output_path


def _get_progress_bar(val: int) -> str:
    """Формирует текстовый прогресс-бар для таблицы ReportLab."""
    val = max(0, min(100, val))
    total_blocks = 10
    filled = round(val / 10)
    empty = total_blocks - filled
    bar_str = "█" * filled + "░" * empty
    if val >= 80:
        color_hex = "#166534"
    elif val >= 60:
        color_hex = "#854D0E"
    else:
        color_hex = "#991B1B"
    return f"<font color='{color_hex}'>{bar_str}</font> {val}%"
