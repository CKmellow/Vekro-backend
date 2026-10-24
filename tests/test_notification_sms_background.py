import threading

from app.db import session as db_session
from app.models.notification import NotificationDeliveryStatus
from app.services import notification_delivery
from app.services.notification_delivery import install_notification_sms_hooks, run_in_background
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.test_notification_sms_delivery import _FakeSender, _notification, _reload, _user


def _factory_with(dispatch, sender) -> sessionmaker:
    from app.db.base import Base
    from app.models.notification import Notification
    from app.models.user import User

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    column = Notification.__table__.c.payload
    original = column.server_default
    column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(engine, tables=[User.__table__, Notification.__table__])
    finally:
        column.server_default = original
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    install_notification_sms_hooks(factory, sender_provider=lambda: sender, dispatch=dispatch)
    return factory


def test_commit_returns_before_any_sms_is_sent() -> None:
    sender = _FakeSender()
    jobs: list = []
    factory = _factory_with(jobs.append, sender)
    with factory() as db:
        notification = _notification(_user(db).id)
        db.add(notification)
        db.commit()

    assert sender.calls == []
    assert (
        _reload(factory, notification.id).delivery_status
        == NotificationDeliveryStatus.NOT_ATTEMPTED
    )

    jobs.pop()()

    assert len(sender.calls) == 1
    assert _reload(factory, notification.id).delivery_status == NotificationDeliveryStatus.SENT


def test_background_runner_runs_jobs_off_the_calling_thread() -> None:
    done = threading.Event()
    seen: list[str] = []

    def job() -> None:
        seen.append(threading.current_thread().name)
        done.set()

    run_in_background(job)

    assert done.wait(5)
    assert seen[0].startswith("notification-sms")


def test_the_app_installs_background_delivery(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        notification_delivery,
        "install_notification_sms_hooks",
        lambda factory, **kwargs: captured.update(kwargs),
    )
    db_session._get_session_factory.cache_clear()
    try:
        db_session._get_session_factory()
    finally:
        db_session._get_session_factory.cache_clear()

    assert captured["dispatch"] is run_in_background
