"""Отчёт о рассылке для автора."""

from __future__ import annotations

from app.services import BroadcastReport

#: Сколько строк показывать в каждом списке, чтобы отчёт не разросся.
_LIST_LIMIT = 20


def _listing(title: str, items: list[str]) -> list[str]:
    lines = [f"\n<b>{title}</b>"]
    lines.extend(items[:_LIST_LIMIT])
    if len(items) > _LIST_LIMIT:
        lines.append(f"…и ещё {len(items) - _LIST_LIMIT}")
    return lines


def format_report(title: str, report: BroadcastReport) -> str:
    """Собрать отчёт, разделив осознанный пропуск и настоящий сбой.

    Смешивать их нельзя: «вышел из группы» — это штатная работа системы, а
    «не доставлено» требует вмешательства автора.
    """
    if report.aborted:
        # Материал не ушёл никому, и это сделано намеренно: проверить допуск
        # было нечем, а раздать всем подряд — ровно то, чего гейт не допускает.
        return "\n".join(
            [
                f"<b>{title} — НЕ РАЗОСЛАН</b>",
                "",
                f"⚠️ {report.aborted}",
                "",
                "Материал не ушёл никому: без работающей проверки он достался бы "
                "всем подряд, включая тех, кто из курса вышел.",
                "",
                "Что проверить:",
                "• бот раздачи добавлен в чат курса и остаётся администратором;",
                "• id чата в /courses совпадает с настоящим (узнать: /groupid в чате).",
                "",
                "После исправления просто разошлите материал заново.",
            ]
        )

    lines = [f"<b>{title}</b>", f"Доставлено: {report.sent} из {report.total}"]

    if report.total > 1 and len(report.skipped) == report.total:
        # Массовый пропуск почти наверняка означает не «все разом вышли», а
        # поломку настройки. Без этого предупреждения автор увидел бы ровный
        # отчёт без единого слова об ошибке и узнал бы о беде от учеников.
        lines += [
            "",
            "⚠️ <b>Ни один ученик не прошёл проверку членства.</b>",
            "Похоже на сбой настройки, а не на массовый выход из группы. Проверьте:",
            "• бот раздачи всё ещё администратор группы курса;",
            "• VSA_GROUP_ID указывает на ту самую группу.",
            "Пока это не исправлено, материалы не получит никто.",
        ]

    if report.skipped:
        lines += _listing(
            "Пропущены — нет в группе курса:",
            [f"• {item.uid:04d} — {item.name}" for item in report.skipped],
        )

    if report.failed:
        lines += _listing(
            "Не доставлено:",
            [f"• {item.uid:04d} — {item.name}: {item.error}" for item in report.failed],
        )

    return "\n".join(lines)
