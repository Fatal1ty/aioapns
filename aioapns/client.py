from ssl import SSLContext
from typing import Awaitable, Callable, Optional

from aioapns.common import NotificationRequest, NotificationResult
from aioapns.connection import (
    APNsBaseConnectionPool,
    APNsCertConnectionPool,
    APNsKeyConnectionPool,
)
from aioapns.logging import logger


class APNs:
    def __init__(
        self,
        client_cert: Optional[str] = None,
        key: Optional[str] = None,
        key_id: Optional[str] = None,
        team_id: Optional[str] = None,
        topic: Optional[str] = None,
        max_connections: int = 10,
        max_connection_attempts: int = 5,
        use_sandbox: bool = False,
        no_cert_validation: bool = False,
        ssl_context: Optional[SSLContext] = None,
        proxy_host: Optional[str] = None,
        proxy_port: Optional[int] = None,
        err_func: Optional[
            Callable[
                [NotificationRequest, NotificationResult], Awaitable[None]
            ]
        ] = None,
    ) -> None:
        self.pool: APNsBaseConnectionPool
        self.err_func = err_func
        if client_cert is not None and key is not None:
            raise ValueError("cannot specify both client_cert and key")
        elif client_cert:
            self.pool = APNsCertConnectionPool(
                cert_file=client_cert,
                topic=topic,
                max_connections=max_connections,
                max_connection_attempts=max_connection_attempts,
                use_sandbox=use_sandbox,
                no_cert_validation=no_cert_validation,
                ssl_context=ssl_context,
                proxy_host=proxy_host,
                proxy_port=proxy_port,
            )
        elif key and key_id and team_id and topic:
            self.pool = APNsKeyConnectionPool(
                key_file=key,
                key_id=key_id,
                team_id=team_id,
                topic=topic,
                max_connections=max_connections,
                max_connection_attempts=max_connection_attempts,
                use_sandbox=use_sandbox,
                ssl_context=ssl_context,
                proxy_host=proxy_host,
                proxy_port=proxy_port,
            )
        else:
            raise ValueError(
                "You must provide either APNs cert file path or "
                "the key credentials"
            )

    async def send_notification(
        self, request: NotificationRequest
    ) -> NotificationResult:
        response = await self.pool.send_notification(request)
        if not response.is_successful:
            if self.err_func is not None:
                await self.err_func(request, response)
            else:
                logger.error(
                    "Status of notification %s is %s (%s)",
                    request.notification_id,
                    response.status,
                    response.description,
                )
        return response
