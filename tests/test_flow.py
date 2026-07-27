"""Сквозной сценарий: регистрация -> урок -> рассылка -> трассировка.

Telegram здесь подменён заглушкой, которая пережимает картинку в JPEG ровно так,
как это делает отправка фото. Дальше из «доставленного» файла собирается скрин
экрана — и по нему система должна назвать конкретного ученика.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.config import Settings
from app.db import Student, create_database
from app.db.repo import (
    collect_stats,
    get_delivery,
    list_active_students,
    register_student,
)
from app.services import LessonBroadcaster, LessonSpec, Storage, TraceHit, TraceMiss, TraceService
from app.services.lessons import save_lesson
from app.watermark import READABLE_SCALE, Payload, WatermarkEngine
from tests.conftest import (
    PW_IMG,
    PW_WM,
    desktop_screenshot,
    jpeg,
    make_chart,
    make_terminal_chart,
    screenshot,
)


@dataclass
class SentMessage:
    chat_id: int
    path: Path | None
    caption: str | None
    text: str | None = None
    protected: bool = False


class FakeBot:
    """Заглушка Telegram: сохраняет «отправленное», пережимая фото в JPEG."""

    def __init__(self, outbox: Path, quality: int = 80) -> None:
        self.outbox = outbox
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.quality = quality
        self.sent: list[SentMessage] = []

    def _store(self, chat_id: int, source: Path) -> Path:
        target = self.outbox / f"{chat_id}_{len(self.sent)}.jpg"
        with Image.open(source) as image:
            image.convert("RGB").save(target, format="JPEG", quality=self.quality)
        return target

    async def send_photo(
        self,
        chat_id: int,
        photo: Any,
        caption: str | None = None,
        protect_content: bool = False,
    ) -> _FakeResult:
        stored = self._store(chat_id, Path(photo.path))
        self.sent.append(
            SentMessage(
                chat_id=chat_id, path=stored, caption=caption, protected=protect_content
            )
        )
        return _FakeResult(len(self.sent))

    async def send_media_group(
        self, chat_id: int, media: list[Any], protect_content: bool = False
    ) -> list[_FakeResult]:
        results: list[_FakeResult] = []
        for item in media:
            stored = self._store(chat_id, Path(item.media.path))
            self.sent.append(
                SentMessage(
                    chat_id=chat_id,
                    path=stored,
                    caption=item.caption,
                    protected=protect_content,
                )
            )
            results.append(_FakeResult(len(self.sent)))
        return results

    async def send_message(
        self, chat_id: int, text: str, protect_content: bool = False
    ) -> Any:
        self.sent.append(
            SentMessage(
                chat_id=chat_id,
                path=None,
                caption=None,
                text=text,
                protected=protect_content,
            )
        )
        return _FakeResult(len(self.sent))

    def delivered_to(self, chat_id: int) -> list[Path]:
        return [item.path for item in self.sent if item.chat_id == chat_id and item.path]


@dataclass
class _FakeResult:
    message_id: int


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_bot_token="admin:test",
        student_bot_token="student:test",
        admin_ids="1,2",
        wm_pw_img=PW_IMG,
        wm_pw_wm=PW_WM,
        db_path=tmp_path / "db.sqlite3",
        storage_path=tmp_path / "storage",
        offer_path=Path("app/texts/offer.txt"),
        wm_workers=2,
        broadcast_rate=1000.0,
    )


@pytest.fixture
async def database(settings: Settings):
    db = create_database(settings)
    await db.create_all()
    yield db
    await db.dispose()


@pytest.fixture
def storage(settings: Settings) -> Storage:
    return Storage(root=settings.storage_path)


@pytest.fixture
def engine() -> WatermarkEngine:
    return WatermarkEngine(password_img=PW_IMG, password_wm=PW_WM)


@pytest.fixture
def lesson_source(tmp_path: Path) -> Path:
    path = tmp_path / "source.png"
    make_chart(width=800, height=520).save(path)
    return path


async def _register(database, tg_id: int, name: str) -> Student:
    async with database.session_factory() as session:
        return await register_student(
            session, tg_user_id=tg_id, username=name.lower(), full_name=name
        )


def _broadcaster(bot: FakeBot, engine: WatermarkEngine, storage: Storage, database, settings):
    return LessonBroadcaster(
        bot=bot,  # type: ignore[arg-type]  # утиная типизация: нужны только send_*
        engine=engine,
        storage=storage,
        session_factory=database.session_factory,
        rate_interval=settings.send_interval,
        workers=settings.wm_workers,
        protect_content=settings.protect_content,
    )


async def test_uid_is_assigned_sequentially(database) -> None:
    first = await _register(database, 1001, "Первый")
    second = await _register(database, 1002, "Второй")
    assert (first.uid, second.uid) == (1, 2)
    assert first.uid_str == "0001"


async def test_repeat_start_keeps_uid(database) -> None:
    first = await _register(database, 1001, "Первый")
    again = await _register(database, 1001, "Первый Изменённый")
    assert again.uid == first.uid, "uid вшит в уже разосланные картинки и меняться не может"


async def test_student_without_consent_is_not_a_recipient(database) -> None:
    """Без принятой оферты доступ не выдаётся — это условие MVP."""
    async with database.session_factory() as session:
        session.add(
            Student(tg_user_id=2001, uid=77, full_name="Без оферты", consent_at=None)
        )
        await session.commit()
        assert await list_active_students(session) == []

    await _register(database, 2002, "С офертой")
    async with database.session_factory() as session:
        recipients = await list_active_students(session)
    assert [student.tg_user_id for student in recipients] == [2002]


async def test_full_flow_delivery_and_trace(
    database, storage: Storage, engine: WatermarkEngine, settings: Settings, lesson_source: Path
) -> None:
    alice = await _register(database, 5001, "Алиса")
    bob = await _register(database, 5002, "Борис")

    async with database.session_factory() as session:
        lesson = await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption="Урок 1: объёмы и спред",
            staged=[lesson_source],
            max_side=settings.lesson_max_side,
        )
        students = list(await list_active_students(session))

    bot = FakeBot(outbox=settings.storage_path / "outbox")
    report = await _broadcaster(bot, engine, storage, database, settings).run(
        LessonSpec.of(lesson), students
    )
    assert report.sent == 2 and not report.failed

    # Каждому ушла своя копия, и она отличается от копии соседа.
    alice_file = bot.delivered_to(alice.tg_user_id)[0]
    bob_file = bot.delivered_to(bob.tg_user_id)[0]
    assert alice_file.read_bytes() != bob_file.read_bytes()

    async with database.session_factory() as session:
        delivery = await get_delivery(session, lesson_id=lesson.id, student_id=bob.id)
    assert delivery is not None
    assert delivery.wm_payload == Payload(uid=bob.uid, lesson_id=lesson.id).encode()

    # Борис делает скриншот экрана и выкладывает его.
    with Image.open(bob_file) as delivered:
        leak = screenshot(delivered, canvas=(1500, 950), scale=0.7, offset=(180, 160))
    leak_path = settings.storage_path / "leak.png"
    jpeg(leak, 85).save(leak_path)

    tracer = TraceService(
        engine=engine,
        session_factory=database.session_factory,
        min_confidence=settings.trace_min_confidence,
    )
    result = await tracer.trace(leak_path, admin_tg_id=1)

    assert isinstance(result, TraceHit), getattr(result, "reason", result)
    assert result.payload == Payload(uid=bob.uid, lesson_id=lesson.id)
    assert result.student is not None and result.student.tg_user_id == bob.tg_user_id
    assert result.delivery_confirmed, "доставка должна подтверждаться журналом"

    async with database.session_factory() as session:
        stats = await collect_stats(session)
    assert (stats.students_active, stats.lessons, stats.deliveries_sent) == (2, 1, 2)
    assert stats.traces_success == 1


async def test_tiny_preview_reports_size_not_false_match(
    database, storage: Storage, engine: WatermarkEngine, settings: Settings, lesson_source: Path
) -> None:
    """Скрин всего экрана: ответ обязан назвать причину — «слишком мелко»."""
    student = await _register(database, 8001, "Алиса")
    async with database.session_factory() as session:
        lesson = await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption=None,
            staged=[lesson_source],
            max_side=settings.lesson_max_side,
        )
        students = list(await list_active_students(session))

    bot = FakeBot(outbox=settings.storage_path / "outbox")
    await _broadcaster(bot, engine, storage, database, settings).run(
        LessonSpec.of(lesson), students
    )

    with Image.open(bot.delivered_to(student.tg_user_id)[0]) as delivered:
        shot = jpeg(desktop_screenshot(delivered, 0.18), 90)
    leak = settings.storage_path / "desktop_leak.png"
    shot.save(leak)

    tracer = TraceService(
        engine=engine,
        session_factory=database.session_factory,
        min_confidence=settings.trace_min_confidence,
    )
    result = await tracer.trace(leak, admin_tg_id=1)

    assert isinstance(result, TraceMiss)
    assert result.best_lesson_id == lesson.id, "урок должен быть опознан, пусть метка и не читается"
    assert result.scale is not None and result.scale < READABLE_SCALE
    assert "занимает" in result.reason and "%" in result.reason


async def test_delivered_material_is_protected(
    database, storage: Storage, engine: WatermarkEngine, settings: Settings, lesson_source: Path
) -> None:
    """Пересылку и сохранение закрываем на стороне Telegram.

    Это снимает самый лёгкий путь утечки — переслать файл в один клик.
    Скриншот остаётся возможен на iOS и десктопе, его и ловит метка.
    """
    long_caption = "Разбор урока. " * 120  # длиннее лимита подписи — уйдёт отдельно
    student = await _register(database, 9500, "Алиса")
    async with database.session_factory() as session:
        lesson = await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption=long_caption,
            staged=[lesson_source],
            max_side=settings.lesson_max_side,
        )
        students = list(await list_active_students(session))

    bot = FakeBot(outbox=settings.storage_path / "outbox_protect")
    await _broadcaster(bot, engine, storage, database, settings).run(
        LessonSpec.of(lesson), students
    )

    sent = [item for item in bot.sent if item.chat_id == student.tg_user_id]
    assert sent, "ученику ничего не ушло"
    # И картинка, и вынесенный отдельно длинный текст должны быть защищены.
    assert all(item.protected for item in sent)
    assert any(item.path for item in sent) and any(item.text for item in sent)


async def test_lesson_image_is_capped(
    database, storage: Storage, settings: Settings, tmp_path: Path
) -> None:
    """Длинная сторона урока ограничивается — иначе метка не переживёт ленту чата."""
    tall = tmp_path / "tall.png"
    make_terminal_chart(589, 1280, seed=5).save(tall)

    async with database.session_factory() as session:
        lesson = await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption=None,
            staged=[tall],
            max_side=settings.lesson_max_side,
        )

    image = lesson.images[0]
    assert max(image.width, image.height) == settings.lesson_max_side
    assert image.width / image.height == pytest.approx(589 / 1280, abs=0.01)


async def test_small_image_is_not_upscaled(database, storage: Storage, tmp_path: Path) -> None:
    small = tmp_path / "small.png"
    make_terminal_chart(300, 420, seed=2).save(small)

    async with database.session_factory() as session:
        lesson = await save_lesson(
            session, storage, admin_tg_id=1, caption=None, staged=[small], max_side=800
        )
    assert (lesson.images[0].width, lesson.images[0].height) == (300, 420)


async def test_chat_preview_screenshot_is_traced(
    database, storage: Storage, engine: WatermarkEngine, settings: Settings, tmp_path: Path
) -> None:
    """Скриншот ленты чата — самый обычный способ слить материал.

    Telegram показывает вертикальное фото уменьшенным независимо от его размера,
    поэтому урок и ограничивается по длинной стороне: иначе на скриншоте видно
    всего ~38% оригинала, и метки там уже нет.
    """
    tall = tmp_path / "tall.png"
    make_terminal_chart(589, 1280, seed=5).save(tall)

    student = await _register(database, 8500, "Борис")
    async with database.session_factory() as session:
        lesson = await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption=None,
            staged=[tall],
            max_side=settings.lesson_max_side,
        )
        students = list(await list_active_students(session))

    bot = FakeBot(outbox=settings.storage_path / "outbox_chat")
    await _broadcaster(bot, engine, storage, database, settings).run(
        LessonSpec.of(lesson), students
    )

    # Так Telegram Desktop показывает вертикальное фото в ленте (замерено).
    with Image.open(bot.delivered_to(student.tg_user_id)[0]) as delivered:
        shown = delivered.resize((225, 489), Image.Resampling.LANCZOS)
    leak = settings.storage_path / "chat_preview.png"
    jpeg(shown, 90).save(leak)

    tracer = TraceService(
        engine=engine,
        session_factory=database.session_factory,
        min_confidence=settings.trace_min_confidence,
    )
    result = await tracer.trace(leak, admin_tg_id=1)

    assert isinstance(result, TraceHit), getattr(result, "reason", result)
    assert result.payload == Payload(uid=student.uid, lesson_id=lesson.id)


@pytest.mark.parametrize("keep", [0.8, 0.5])
async def test_cropped_leak_is_still_traced(
    database,
    storage: Storage,
    engine: WatermarkEngine,
    settings: Settings,
    lesson_source: Path,
    keep: float,
) -> None:
    """От урока отрезали часть — виновник всё равно должен определяться.

    Без восстановления геометрии обрезка ломает трассировку полностью, даже
    когда срезали десятую долю: сетка блоков уезжает целиком.
    """
    student = await _register(database, 9001 + int(keep * 100), "Борис")
    async with database.session_factory() as session:
        lesson = await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption=None,
            staged=[lesson_source],
            max_side=settings.lesson_max_side,
        )
        students = list(await list_active_students(session))

    bot = FakeBot(outbox=settings.storage_path / f"outbox_{keep}")
    await _broadcaster(bot, engine, storage, database, settings).run(
        LessonSpec.of(lesson), students
    )

    with Image.open(bot.delivered_to(student.tg_user_id)[0]) as delivered:
        side = keep**0.5
        width, height = int(delivered.width * side), int(delivered.height * side)
        left, top = (delivered.width - width) // 2, (delivered.height - height) // 2
        piece = delivered.crop((left, top, left + width, top + height))

    leak = settings.storage_path / f"cropped_{keep}.png"
    jpeg(piece, 90).save(leak)

    tracer = TraceService(
        engine=engine,
        session_factory=database.session_factory,
        min_confidence=settings.trace_min_confidence,
    )
    result = await tracer.trace(leak, admin_tg_id=1)

    assert isinstance(result, TraceHit), getattr(result, "reason", result)
    assert result.payload == Payload(uid=student.uid, lesson_id=lesson.id)
    assert result.student is not None and result.student.tg_user_id == student.tg_user_id


async def test_unrelated_image_is_not_attributed(
    database, storage: Storage, engine: WatermarkEngine, settings: Settings, lesson_source: Path
) -> None:
    """Чужая картинка не должна назначать виноватого."""
    await _register(database, 6001, "Алиса")
    async with database.session_factory() as session:
        await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption=None,
            staged=[lesson_source],
            max_side=settings.lesson_max_side,
        )

    stranger = settings.storage_path / "stranger.png"
    make_chart(width=800, height=520, seed=1234).save(stranger)

    tracer = TraceService(
        engine=engine,
        session_factory=database.session_factory,
        min_confidence=settings.trace_min_confidence,
    )
    result = await tracer.trace(stranger, admin_tg_id=1)
    assert isinstance(result, TraceMiss)


async def test_partial_extraction_is_rejected(
    database,
    storage: Storage,
    engine: WatermarkEngine,
    settings: Settings,
    lesson_source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Метка, назвавшая чужой урок, отбрасывается, а не выдаётся за истину.

    Именно так выглядит частично восстановленная строка при неточном кропе:
    формат верный, uid правдоподобный, а lesson_id — от балды.
    """
    student = await _register(database, 7001, "Алиса")
    async with database.session_factory() as session:
        lesson = await save_lesson(
            session,
            storage,
            admin_tg_id=1,
            caption=None,
            staged=[lesson_source],
            max_side=settings.lesson_max_side,
        )

    wrong = Payload(uid=student.uid, lesson_id=lesson.id + 500)
    monkeypatch.setattr(engine, "extract", lambda *args, **kwargs: wrong)

    tracer = TraceService(
        engine=engine,
        session_factory=database.session_factory,
        min_confidence=settings.trace_min_confidence,
    )
    result = await tracer.trace(Path(lesson.original_image_path), admin_tg_id=1)

    assert isinstance(result, TraceMiss)
    assert "не читается" in result.reason
