import asyncio
from uuid import uuid4


PRIORITY_NORMAL = '5'
PRIORITY_HIGH = '10'


class NotificationRequest:
    __slots__ = ('device_token', 'message', 'notification_id',
                 'time_to_live', 'priority', 'collapse_key')

    def __init__(self, device_token, message, notification_id=None,
                 time_to_live=None, priority=None, collapse_key=None):
        self.device_token = device_token
        self.message = message
        self.notification_id = notification_id or str(uuid4())
        self.time_to_live = time_to_live
        self.priority = priority
        self.collapse_key = collapse_key


class NotificationResult:
    __slots__ = ('notification_id', 'status', 'description')

    def __init__(self, notification_id, status, description=None):
        self.notification_id = notification_id
        self.status = status
        self.description = description

    @property
    def is_successful(self):
        return self.status == APNS_RESPONSE_CODE.SUCCESS


class DynamicBoundedSemaphore(asyncio.BoundedSemaphore):
    @property
    def bound(self):
        return self._bound_value

    @bound.setter
    def bound(self, new_bound):
        if new_bound > self._bound_value:
            if self._value > 0:
                self._value += (new_bound - self._bound_value)
            if self._value <= 0:
                for _ in range(new_bound - self._bound_value):
                    self.release()
        elif new_bound < self._bound_value:
            self._value -= (self._bound_value - new_bound)
        self._bound_value = new_bound

    def release(self):
        self._value += 1
        if self._value > self._bound_value:
            self._value = self._bound_value
        self._wake_up_next()

    def destroy(self, exc):
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(exc)


class APNS_RESPONSE_CODE:
    SUCCESS = '200'
