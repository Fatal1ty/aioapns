import asyncio
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

PRIORITY_NORMAL = "5"
PRIORITY_HIGH = "10"


class PushType(Enum):
    ALERT = "alert"
    BACKGROUND = "background"
    VOIP = "voip"
    COMPLICATION = "complication"
    FILEPROVIDER = "fileprovider"
    MDM = "mdm"
    LIVEACTIVITY = "liveactivity"


class NotificationRequest:
    __slots__ = (
        "device_token",
        "message",
        "notification_id",
        "time_to_live",
        "priority",
        "collapse_key",
        "push_type",
        "apns_topic",
    )

    def __init__(
        self,
        device_token: str,
        message: Dict[str, Any],
        notification_id: Optional[str] = None,
        time_to_live: Optional[int] = None,
        priority: Optional[int] = None,
        collapse_key: Optional[str] = None,
        push_type: Optional[PushType] = None,
        *,
        apns_topic: Optional[str] = None,
    ) -> None:
        self.device_token = device_token
        self.message = message
        self.notification_id = notification_id or str(uuid4())
        self.time_to_live = time_to_live
        self.priority = priority
        self.collapse_key = collapse_key
        self.push_type = push_type
        self.apns_topic = apns_topic


class NotificationResult:
    __slots__ = ("notification_id", "status", "description", "timestamp")

    def __init__(
        self,
        notification_id: str,
        status: str,
        description: Optional[str] = None,
        timestamp: Optional[int] = None,
    ):
        self.notification_id = notification_id
        self.status = status
        self.description = description
        self.timestamp = timestamp

    @property
    def is_successful(self) -> bool:
        return self.status == APNS_RESPONSE_CODE.SUCCESS


class DynamicBoundedSemaphore(asyncio.BoundedSemaphore):
    _bound_value: int

    @property
    def bound(self) -> int:
        return self._bound_value

    @bound.setter
    def bound(self, new_bound: int) -> None:
        if new_bound > self._bound_value:
            if self._value > 0:
                self._value += new_bound - self._bound_value
            if self._value <= 0:
                for _ in range(new_bound - self._bound_value):
                    self.release()
        elif new_bound < self._bound_value:
            self._value -= self._bound_value - new_bound
        self._bound_value = new_bound

    def release(self) -> None:
        self._value += 1
        if self._value > self._bound_value:
            self._value = self._bound_value
        self._wake_up_next()

    def destroy(self, exc: Exception) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(exc)


class APNS_RESPONSE_CODE:
    SUCCESS = "200"
    BAD_REQUEST = "400"
    FORBIDDEN = "403"
    METHOD_NOT_ALLOWED = "405"
    GONE = "410"
    PAYLOAD_TOO_LARGE = "413"
    TOO_MANY_REQUESTS = "429"
    INTERNAL_SERVER_ERROR = "500"
    SERVICE_UNAVAILABLE = "503"
