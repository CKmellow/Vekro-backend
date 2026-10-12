from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import get_settings
from app.models.notification import Notification, NotificationDeliveryStatus
from app.models.user import User
from app.services.sms import AfricasTalkingSmsSender, SmsSendResult

_PENDING_KEY = "pending_sms_notification_ids"
_HOOKS_INSTALLED_KEY = "_vekro_sms_hooks_installed"
SMS_MAX_LENGTH = 459

delivery_logger = logging.getLogger("app.notifications.sms")


class SmsSender(Protocol):
    def send(self, message: str, recipients: Sequence[str]) -> SmsSendResult: ...


SenderProvider = Callable[[], SmsSender | None]


@dataclass(frozen=True)
class NotificationSmsDeliveryResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0


def default_sender_provider() -> SmsSender | None:
    settings = get_settings()
    if not settings.africastalking_api_key.strip():
        return None
    return AfricasTalkingSmsSender.from_settings(settings)


def build_sms_text(notification: Notification) -> str:
    text = f"Vekro: {notification.title}. {notification.message}".strip()
    if len(text) > SMS_MAX_LENGTH:
        text = f"{text[: SMS_MAX_LENGTH - 3]}..."
    return text


def deliver_notification_sms(
    db: Session,
    notification_ids: Iterable[uuid.UUID],
    *,
    sender: SmsSender | None,
    now: datetime | None = None,
) -> NotificationSmsDeliveryResult:
    ids = list(dict.fromkeys(notification_ids))
    if not ids:
        return NotificationSmsDeliveryResult()
    if sender is None:
        return NotificationSmsDeliveryResult(skipped=len(ids))

    notifications = list(
        db.execute(
            select(Notification).where(
                Notification.id.in_(ids),
                Notification.delivery_status == NotificationDeliveryStatus.NOT_ATTEMPTED,
            )
        )
        .scalars()
        .all()
    )

    sent = failed = 0
    current_time = now or datetime.now(UTC)
    for notification in notifications:
        user = db.get(User, notification.user_id)
        phone = (user.phone or "").strip() if user is not None else ""
        delivered = False
        if phone:
            try:
                result = sender.send(build_sms_text(notification), [phone])
                delivered = result.all_sent
            except Exception as exc:
                delivery_logger.warning(
                    "notification_sms_send_error notification_id=%s error_type=%s",
                    notification.id,
                    type(exc).__name__,
                )

        if delivered:
            notification.delivery_status = NotificationDeliveryStatus.SENT
            notification.delivered_at = current_time
            sent += 1
        else:
            notification.delivery_status = NotificationDeliveryStatus.FAILED
            failed += 1
        db.add(notification)

    db.commit()
    delivery_logger.info("notification_sms_delivery sent=%s failed=%s", sent, failed)
    return NotificationSmsDeliveryResult(sent=sent, failed=failed)


def install_notification_sms_hooks(
    session_factory: sessionmaker,
    *,
    sender_provider: SenderProvider = default_sender_provider,
) -> None:
    if getattr(session_factory, _HOOKS_INSTALLED_KEY, False):
        return
    setattr(session_factory, _HOOKS_INSTALLED_KEY, True)

    @event.listens_for(session_factory, "after_flush")
    def _collect(session: Session, _flush_context) -> None:
        for obj in session.new:
            if isinstance(obj, Notification):
                session.info.setdefault(_PENDING_KEY, []).append(obj.id)

    @event.listens_for(session_factory, "after_rollback")
    def _discard(session: Session) -> None:
        session.info.pop(_PENDING_KEY, None)

    @event.listens_for(session_factory, "after_commit")
    def _dispatch(session: Session) -> None:
        ids = session.info.pop(_PENDING_KEY, None)
        if not ids:
            return
        # The committing session cannot run SQL inside after_commit.
        delivery_db = session_factory()
        try:
            deliver_notification_sms(delivery_db, ids, sender=sender_provider())
        except Exception as exc:
            delivery_db.rollback()
            delivery_logger.warning(
                "notification_sms_delivery_failed count=%s error_type=%s",
                len(ids),
                type(exc).__name__,
            )
        finally:
            delivery_db.close()
