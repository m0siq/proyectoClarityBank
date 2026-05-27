"""Shared prompt templates for the cold-path Batch API calls (SPEC §6.5)."""

from __future__ import annotations

SYSTEM_PROMPT = (
    "Eres un asistente financiero que escribe resúmenes mensuales personalizados. "
    "Tono cercano pero profesional. Máximo 5 frases. Sin moralizar el gasto."
)


def build_user_prompt(
    user_hash: str,
    year_month: str,
    breakdown: dict[str, float],
    prev_breakdown: dict[str, float] | None = None,
) -> str:
    """Build the user message for a monthly summary request.

    Args:
        user_hash: SHA-256 truncated to 16 chars (anonymized user ID).
        year_month: e.g. "2026-05"
        breakdown: dict of category → total amount in EUR
        prev_breakdown: previous month breakdown for comparison (optional)
    """
    lines = [f"Resumen mensual del usuario {user_hash}, mes {year_month}, EUR:"]
    for category, amount in sorted(breakdown.items()):
        lines.append(f"- {category.capitalize()}: {amount:.2f}€")

    if prev_breakdown:
        lines.append("\nMes anterior:")
        for category, amount in sorted(prev_breakdown.items()):
            lines.append(f"- {category.capitalize()}: {amount:.2f}€")

    lines.append(
        "\nEscribe un párrafo breve destacando la categoría con más gasto "
        "y comparándolo con el mes anterior si está disponible."
    )
    return "\n".join(lines)
