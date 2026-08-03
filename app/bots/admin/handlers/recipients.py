"""Сводка по получателям: кто получит следующий материал и кто получил прошлые."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import DeliveryStatus, Student
from app.db.repo import (
    get_course,
    get_lesson,
    list_active_students,
    list_courses,
    list_lesson_deliveries,
    list_lesson_summaries,
)
from app.services import LessonBroadcaster
from app.services.membership import CourseUnreachable
from app.utils import format_dt

logger = logging.getLogger(__name__)

router = Router(name="recipients")

#: Сколько материалов показывать в сводке.
_RECENT = 10
#: Сколько имён показывать в списке, чтобы не упереться в лимит сообщения.
_NAMES = 40


@router.message(Command("recipients"))
async def show_recipients(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    broadcaster: LessonBroadcaster,
) -> None:
    """Кто что получит, в разбивке по курсам.

    Числа живые: членство спрашивается у Telegram, а не берётся из базы.
    Именно они определяют, кому уйдёт следующий материал каждого курса.
    """
    students = list(await list_active_students(session))
    if not students:
        await message.answer("Ни один ученик ещё не принял условия.")
        return

    courses = list(await list_courses(session))
    if not courses:
        await _without_courses(message, settings, broadcaster, students)
        return

    status = await message.answer(
        f"Проверяю {len(students)} учеников по {len(courses)} курсам…"
    )

    inside: dict[int, set[int]] = {}
    broken: dict[int, str] = {}
    for course in courses:
        try:
            members, _ = await broadcaster.split_by_membership(
                students, chat_ids=[course.chat_id]
            )
        except CourseUnreachable as exc:
            # Курс, который нельзя проверить, — это не «ноль получателей»: по
            # нему рассылка вообще не пойдёт, и сказать надо именно так.
            broken[course.id] = exc.reason
            inside[course.id] = set()
            continue
        inside[course.id] = {student.id for student in members}

    lines = ["<b>Получатели по курсам</b>", f"Принято условий: {len(students)}", ""]
    for course in courses:
        if course.id in broken:
            lines.append(f"<b>{course.title}</b> — ⚠️ чат недоступен, рассылки не будет")
            continue
        lines.append(f"<b>{course.title}</b> — получат {len(inside[course.id])}")

    # Сколько курсов приходится на человека: это и есть ответ на вопрос,
    # кто получит два потока, кто один, а кто уже ничего.
    counts = {
        student.id: sum(1 for course in courses if student.id in inside[course.id])
        for student in students
    }
    if len(courses) > 1:
        both = sum(1 for value in counts.values() if value > 1)
        lines.append(f"\nСразу в нескольких курсах: {both}")

    if broken:
        lines += [
            "",
            "⚠️ <b>Часть курсов не проверяется:</b>",
            *(
                f"• {course.title}: {broken[course.id]}"
                for course in courses
                if course.id in broken
            ),
            "Проверьте, что бот раздачи в чате и остаётся администратором, "
            "а id курса совпадает с настоящим (/groupid в чате).",
        ]

    orphans = [student for student in students if counts[student.id] == 0]
    if orphans:
        lines.append(f"\n<b>Ни в одном курсе ({len(orphans)}):</b>")
        lines.append("Зарегистрированы, но материалы им больше не уходят.")
        lines.extend(f"• {s.uid_str} — {s.display_name}" for s in orphans[:_NAMES])
        if len(orphans) > _NAMES:
            lines.append(f"…и ещё {len(orphans) - _NAMES}")

    if not any(inside.values()) and len(students) > 1 and not broken:
        lines.append(
            "\n⚠️ Не прошёл никто ни по одному курсу. Похоже на сбой настройки: "
            "проверьте, что бот рассылки — администратор во всех чатах курсов."
        )
    await status.edit_text("\n".join(lines))


async def _without_courses(
    message: Message,
    settings: Settings,
    broadcaster: LessonBroadcaster,
    students: list[Student],
) -> None:
    """Курсов не заведено — старое поведение по одной группе из настроек."""
    if settings.vsa_group_id is None:
        await message.answer(
            f"<b>Получателей: {len(students)}</b>\n\n"
            "Курсы не заведены и проверка членства выключена — материалы "
            "получат все, кто принял условия.\n\nЗавести курс: /courses"
        )
        return

    status = await message.answer(f"Проверяю {len(students)} учеников…")
    try:
        inside, outside = await broadcaster.split_by_membership(students)
    except CourseUnreachable as exc:
        await status.edit_text(
            f"⚠️ {exc.reason}\n\nПроверьте VSA_GROUP_ID и права бота раздачи в группе."
        )
        return
    lines = [f"<b>Получателей: {len(inside)}</b>", f"Принято условий: {len(students)}"]
    if outside:
        lines.append(f"\n<b>Не получат — нет в группе ({len(outside)}):</b>")
        lines.extend(f"• {s.uid_str} — {s.display_name}" for s in outside[:_NAMES])
    await status.edit_text("\n".join(lines))


@router.message(Command("course"))
async def show_course_audience(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    broadcaster: LessonBroadcaster,
) -> None:
    """Поимённо: кто из зарегистрированных состоит в этом курсе."""
    raw = (command.args or "").strip().lstrip("#")
    if not raw.isdigit():
        courses = list(await list_courses(session, only_active=False))
        hint = "\n".join(f"• <code>/course {c.id}</code> — {c.title}" for c in courses)
        await message.answer(f"Укажите номер курса:\n{hint}" if hint else "Курсов нет.")
        return

    course = await get_course(session, int(raw))
    if course is None:
        await message.answer(f"Курса #{raw} нет. Список: /courses")
        return

    students = list(await list_active_students(session))
    if not students:
        await message.answer("Ни один ученик ещё не принял условия.")
        return

    status = await message.answer(f"Проверяю {len(students)} учеников…")
    try:
        inside, outside = await broadcaster.split_by_membership(
            students, chat_ids=[course.chat_id]
        )
    except CourseUnreachable as exc:
        await status.edit_text(
            f"<b>{course.title}</b>\n\n⚠️ {exc.reason}\n\n"
            "Пока это не исправлено, материалы этого курса не уйдут никому. "
            "Проверьте, что бот раздачи в чате и остаётся администратором, "
            "а id курса совпадает с настоящим (/groupid в чате)."
        )
        return

    lines = [
        f"<b>{course.title}</b>",
        f"Получат материалы курса: <b>{len(inside)}</b> из {len(students)}",
        "",
    ]
    if inside:
        lines.append(f"<b>В курсе ({len(inside)}):</b>")
        lines.extend(f"• {s.uid_str} — {s.display_name}" for s in inside[:_NAMES])
        if len(inside) > _NAMES:
            lines.append(f"…и ещё {len(inside) - _NAMES}")
    if outside:
        lines.append(f"\n<b>Не в курсе ({len(outside)}):</b>")
        lines.extend(f"• {s.uid_str} — {s.display_name}" for s in outside[:_NAMES])
        if len(outside) > _NAMES:
            lines.append(f"…и ещё {len(outside) - _NAMES}")
    await status.edit_text("\n".join(lines))


@router.message(Command("lessons"))
async def show_lessons(message: Message, session: AsyncSession) -> None:
    """Сколько человек получило каждый из последних материалов."""
    summaries = await list_lesson_summaries(session, limit=_RECENT)
    if not summaries:
        await message.answer("Материалов пока нет.")
        return

    lines = [f"<b>Последние материалы ({len(summaries)})</b>", ""]
    for item in summaries:
        counts = [f"получили {item.sent}"]
        if item.skipped:
            counts.append(f"пропущено {item.skipped}")
        if item.failed:
            counts.append(f"ошибок {item.failed}")
        course = item.lesson.course.title if item.lesson.course else "всем"
        lines.append(f"<b>{item.lesson.title}</b> · {course}")
        lines.append(f"   {format_dt(item.lesson.created_at)} · " + " · ".join(counts))
    lines.append("\nПодробности: /lesson &lt;номер&gt;")
    await message.answer("\n".join(lines))


@router.message(Command("lesson"))
async def show_lesson(message: Message, command: CommandObject, session: AsyncSession) -> None:
    """Поимённо: кто получил материал, кого пропустили, у кого сорвалось."""
    raw = (command.args or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Укажите номер материала: <code>/lesson 9</code>")
        return

    lesson = await get_lesson(session, int(raw))
    if lesson is None:
        await message.answer(f"Материала #{raw} нет.")
        return

    rows = await list_lesson_deliveries(session, lesson.id)
    if not rows:
        await message.answer(
            f"<b>{lesson.title}</b> · {format_dt(lesson.created_at)}\n\nЕщё никому не выдавался."
        )
        return

    groups: dict[DeliveryStatus, list[str]] = {status: [] for status in DeliveryStatus}
    for delivery, student in rows:
        groups[delivery.status].append(f"• {student.uid_str} — {student.display_name}")

    lines = [
        f"<b>{lesson.title}</b> · {format_dt(lesson.created_at)}",
        f"Курс: {lesson.course.title if lesson.course else 'всем зарегистрированным'}",
        f"Получили: <b>{len(groups[DeliveryStatus.SENT])}</b>",
    ]
    titles = {
        DeliveryStatus.SENT: "Получили",
        DeliveryStatus.SKIPPED: "Пропущены — нет в группе курса",
        DeliveryStatus.FAILED: "Не доставлено",
    }
    for status, title in titles.items():
        names = groups[status]
        if not names:
            continue
        lines.append(f"\n<b>{title} ({len(names)}):</b>")
        lines.extend(names[:_NAMES])
        if len(names) > _NAMES:
            lines.append(f"…и ещё {len(names) - _NAMES}")
    await message.answer("\n".join(lines))
