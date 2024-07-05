import asyncio
import json
import ssl
import time
from functools import partial
from typing import Any, Callable, Dict, List, NoReturn, Optional, Type

import jwt
import OpenSSL
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RemoteSettingsChanged,
    ResponseReceived,
    SettingsAcknowledged,
    StreamEnded,
    WindowUpdated,
)
from h2.exceptions import FlowControlError, NoAvailableStreamIDError
from h2.settings import ChangedSetting, SettingCodes

from aioapns.common import (
    APNS_RESPONSE_CODE,
    DynamicBoundedSemaphore,
    NotificationRequest,
    NotificationResult,
)
from aioapns.exceptions import (
    ConnectionClosed,
    ConnectionError,
    MaxAttemptsExceeded,
)
from aioapns.logging import logger


class ChannelPool(DynamicBoundedSemaphore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(ChannelPool, self).__init__(*args, **kwargs)
        self._stream_id = -1

    async def acquire(self) -> int:  # type: ignore
        await super(ChannelPool, self).acquire()
        self._stream_id += 2
        if self._stream_id > H2Connection.HIGHEST_ALLOWED_STREAM_ID:
            raise NoAvailableStreamIDError()
        return self._stream_id

    @property
    def is_busy(self) -> bool:
        return self._value <= 0


class AuthorizationHeaderProvider:
    def get_header(self) -> str:
        raise NotImplementedError


class JWTAuthorizationHeaderProvider(AuthorizationHeaderProvider):
    TOKEN_TTL = 30 * 60

    def __init__(self, key, key_id, team_id) -> None:
        self.key = key
        self.key_id = key_id
        self.team_id = team_id

        self.__issued_at = None
        self.__header = None

    def get_header(self):
        now = time.time()
        if not self.__header or self.__issued_at < now - self.TOKEN_TTL:
            self.__issued_at = int(now)
            token = jwt.encode(
                payload={"iss": self.team_id, "iat": self.__issued_at},
                key=self.key,
                algorithm="ES256",
                headers={"kid": self.key_id},
            )
            self.__header = f"bearer {token}"
        return self.__header


class H2Protocol(asyncio.Protocol):
    def __init__(self) -> None:
        self.transport: Optional[asyncio.Transport] = None
        self.conn = H2Connection()
        self.free_channels = ChannelPool(1000)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore
        self.conn.initiate_connection()
        self.flush()

    def data_received(self, data: bytes) -> None:
        for event in self.conn.receive_data(data):
            if isinstance(event, ResponseReceived):
                headers = dict(event.headers)
                self.on_response_received(headers)
            elif isinstance(event, DataReceived):
                self.on_data_received(event.data, event.stream_id)
            elif isinstance(event, RemoteSettingsChanged):
                self.on_remote_settings_changed(event.changed_settings)
            elif isinstance(event, StreamEnded):
                self.on_stream_ended(event.stream_id)
            elif isinstance(event, ConnectionTerminated):
                self.on_connection_terminated(event)
            elif isinstance(event, WindowUpdated):
                pass
            elif isinstance(event, SettingsAcknowledged):
                pass
            else:
                logger.warning("Unknown event: %s", event)
        self.flush()

    def flush(self) -> None:
        assert self.transport is not None
        self.transport.write(self.conn.data_to_send())

    def on_response_received(self, headers: Dict[bytes, bytes]) -> None:
        pass

    def on_data_received(self, data: bytes, stream_id: int) -> None:
        pass

    def on_remote_settings_changed(
        self, changed_settings: Dict[SettingCodes, ChangedSetting]
    ) -> None:
        for setting in changed_settings.values():
            logger.debug("Remote setting changed: %s", setting)
            if setting.setting == SettingCodes.MAX_CONCURRENT_STREAMS:
                self.free_channels.bound = setting.new_value

    def on_stream_ended(self, stream_id: int) -> None:
        if stream_id % 2 == 0:
            logger.warning("End stream: %d", stream_id)
        self.free_channels.release()

    def on_connection_terminated(self, event: ConnectionTerminated) -> None:
        pass


class APNsBaseClientProtocol(H2Protocol):
    APNS_SERVER = "api.push.apple.com"
    INACTIVITY_TIME = 10

    def __init__(
        self,
        apns_topic: str,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        on_connection_lost: Optional[
            Callable[["APNsBaseClientProtocol"], NoReturn]
        ] = None,
        auth_provider: Optional[AuthorizationHeaderProvider] = None,
    ) -> None:
        super(APNsBaseClientProtocol, self).__init__()
        self.apns_topic = apns_topic
        self.loop = loop or asyncio.get_event_loop()
        self.on_connection_lost = on_connection_lost
        self.auth_provider = auth_provider

        self.requests: Dict[str, asyncio.Future[NotificationResult]] = {}
        self.request_streams: Dict[int, str] = {}
        self.request_statuses: Dict[str, str] = {}
        self.inactivity_timer: Optional[asyncio.TimerHandle] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super(APNsBaseClientProtocol, self).connection_made(transport)
        self.refresh_inactivity_timer()

    async def send_notification(
        self, request: NotificationRequest
    ) -> NotificationResult:
        stream_id = await self.free_channels.acquire()

        headers = [
            (":method", "POST"),
            (":scheme", "https"),
            (":path", "/3/device/%s" % request.device_token),
            ("host", self.APNS_SERVER),
            ("apns-id", request.notification_id),
        ]
        apns_topic = request.apns_topic or self.apns_topic
        headers.append(("apns-topic", apns_topic))
        if request.time_to_live is not None:
            expiration = int(time.time()) + request.time_to_live
            headers.append(("apns-expiration", str(expiration)))
        if request.priority is not None:
            headers.append(("apns-priority", str(request.priority)))
        if request.collapse_key is not None:
            headers.append(("apns-collapse-id", request.collapse_key))
        if request.push_type is not None:
            headers.append(("apns-push-type", request.push_type.value))
        if self.auth_provider:
            headers.append(("authorization", self.auth_provider.get_header()))

        self.conn.send_headers(stream_id=stream_id, headers=headers)
        try:
            data = json.dumps(request.message, ensure_ascii=False).encode()
            self.conn.send_data(
                stream_id=stream_id,
                data=data,
                end_stream=True,
            )
        except FlowControlError:
            raise

        self.flush()

        future_response: asyncio.Future[NotificationResult] = asyncio.Future()
        self.requests[request.notification_id] = future_response
        self.request_streams[stream_id] = request.notification_id

        response = await future_response
        return response

    def flush(self) -> None:
        assert self.transport is not None
        self.refresh_inactivity_timer()
        self.transport.write(self.conn.data_to_send())

    def refresh_inactivity_timer(self) -> None:
        if self.inactivity_timer:
            self.inactivity_timer.cancel()
        self.inactivity_timer = self.loop.call_later(
            self.INACTIVITY_TIME, self.close
        )

    @property
    def is_busy(self) -> bool:
        return self.free_channels.is_busy

    def close(self) -> None:
        raise NotImplementedError

    def connection_lost(self, exc: Optional[Exception]) -> None:
        logger.debug("Connection %s lost! Error: %s", self, exc)

        if self.inactivity_timer:
            self.inactivity_timer.cancel()

        if self.on_connection_lost:
            self.on_connection_lost(self)

        closed_connection = ConnectionClosed(str(exc))
        for request in self.requests.values():
            request.set_exception(closed_connection)
        self.free_channels.destroy(closed_connection)

    def on_response_received(self, headers: Dict[bytes, bytes]) -> None:
        notification_id = headers.get(b"apns-id", b"").decode("utf8")
        status = headers.get(b":status", b"").decode("utf8")
        if status == APNS_RESPONSE_CODE.SUCCESS:
            request = self.requests.pop(notification_id, None)
            if request:
                result = NotificationResult(notification_id, status)
                request.set_result(result)
            else:
                logger.warning(
                    "Got response for unknown notification request %s",
                    notification_id,
                )
        else:
            self.request_statuses[notification_id] = status

    def on_data_received(self, raw_data: bytes, stream_id: int) -> None:
        data = json.loads(raw_data.decode())
        reason = data.get("reason", "")
        timestamp = data.get("timestamp")

        if not reason:
            return

        notification_id = self.request_streams.pop(stream_id, None)
        if notification_id:
            request = self.requests.pop(notification_id, None)
            if request:
                # TODO: Теоретически здесь может быть ошибка, если нет ключа
                status = self.request_statuses.pop(notification_id)
                result = NotificationResult(
                    notification_id,
                    status,
                    description=reason,
                    timestamp=timestamp,
                )
                request.set_result(result)
            else:
                logger.warning("Could not find request %s", notification_id)
        else:
            logger.warning(
                "Could not find notification by stream %s", stream_id
            )

    def on_connection_terminated(self, event: ConnectionTerminated):
        logger.warning(
            "Connection %s terminated: code=%s, additional_data=%s, "
            "last_stream_id=%s",
            self,
            event.error_code,
            event.additional_data,
            event.last_stream_id,
        )
        self.close()


class APNsTLSClientProtocol(APNsBaseClientProtocol):
    APNS_PORT = 443

    def close(self) -> None:
        if self.inactivity_timer:
            self.inactivity_timer.cancel()
        logger.debug("Closing connection %s", self)
        if self.transport is not None:
            self.transport.close()


class APNsProductionClientProtocol(APNsTLSClientProtocol):
    APNS_SERVER = "api.push.apple.com"


class APNsDevelopmentClientProtocol(APNsTLSClientProtocol):
    APNS_SERVER = "api.development.push.apple.com"


class APNsBaseConnectionPool:
    def __init__(
        self,
        topic: Optional[str] = None,
        max_connections: int = 10,
        max_connection_attempts: int = 5,
        use_sandbox: bool = False,
        proxy_host: Optional[str] = None,
        proxy_port: Optional[int] = None,
    ) -> None:
        self.apns_topic = topic
        self.max_connections = max_connections
        self.protocol_class: Type[APNsTLSClientProtocol]
        if use_sandbox:
            self.protocol_class = APNsDevelopmentClientProtocol
        else:
            self.protocol_class = APNsProductionClientProtocol

        self.loop = asyncio.get_event_loop()
        self.connections: List[APNsBaseClientProtocol] = []
        self._lock = asyncio.Lock()
        self.max_connection_attempts = max_connection_attempts
        self.ssl_context: Optional[ssl.SSLContext] = None

        self.proxy_host = proxy_host
        self.proxy_port = proxy_port

    async def create_connection(self) -> APNsBaseClientProtocol:
        raise NotImplementedError

    def close(self) -> None:
        for connection in self.connections:
            connection.close()

    def discard_connection(self, connection: APNsBaseClientProtocol) -> None:
        logger.debug("Connection %s discarded", connection)
        self.connections.remove(connection)
        logger.info("Connection released (total: %d)", len(self.connections))

    async def acquire(self) -> APNsBaseClientProtocol:
        for connection in self.connections:
            if not connection.is_busy:
                return connection

        async with self._lock:
            for connection in self.connections:
                if not connection.is_busy:
                    return connection
            if len(self.connections) < self.max_connections:
                try:
                    connection = await self.create_connection()
                except Exception as e:
                    logger.error("Could not connect to server: %s", str(e))
                    raise ConnectionError()
                self.connections.append(connection)
                logger.info(
                    "Connection established (total: %d)", len(self.connections)
                )
                return connection

        logger.warning(
            "Pool is completely busy and has hit max connections, retrying..."
        )
        while True:
            await asyncio.sleep(0.01)
            for connection in self.connections:
                if not connection.is_busy:
                    return connection

    async def send_notification(
        self, request: NotificationRequest
    ) -> NotificationResult:
        attempts = 0
        while attempts < self.max_connection_attempts:
            attempts += 1
            logger.debug(
                "Notification %s: waiting for connection",
                request.notification_id,
            )
            try:
                connection = await self.acquire()
            except ConnectionError:
                logger.warning(
                    "Could not send notification %s: " "ConnectionError",
                    request.notification_id,
                )
                await asyncio.sleep(1)
                continue
            logger.debug(
                "Notification %s: connection %s acquired",
                request.notification_id,
                connection,
            )
            try:
                response = await connection.send_notification(request)
                return response
            except NoAvailableStreamIDError:
                connection.close()
            except ConnectionClosed:
                logger.warning(
                    "Could not send notification %s: " "ConnectionClosed",
                    request.notification_id,
                )
            except FlowControlError:
                logger.debug(
                    "Got FlowControlError for notification %s",
                    request.notification_id,
                )
                await asyncio.sleep(1)
        logger.error("Failed to send after %d attempts.", attempts)
        raise MaxAttemptsExceeded

    async def _create_proxy_connection(
        self, apns_protocol_factory
    ) -> APNsBaseClientProtocol:
        assert self.proxy_host is not None, "proxy_host must be set"
        assert self.proxy_port is not None, "proxy_port must be set"

        _, protocol = await self.loop.create_connection(
            protocol_factory=partial(
                HttpProxyProtocol,
                self.protocol_class.APNS_SERVER,
                self.protocol_class.APNS_PORT,
                self.loop,
                self.ssl_context,
                apns_protocol_factory,
            ),
            host=self.proxy_host,
            port=self.proxy_port,
        )
        await protocol.apns_connection_ready.wait()

        assert (
            protocol.apns_protocol is not None
        ), "protocol.apns_protocol could not be set"
        return protocol.apns_protocol


class APNsCertConnectionPool(APNsBaseConnectionPool):
    def __init__(
        self,
        cert_file: str,
        topic: Optional[str] = None,
        max_connections: int = 10,
        max_connection_attempts: int = 5,
        use_sandbox: bool = False,
        no_cert_validation: bool = False,
        ssl_context: Optional[ssl.SSLContext] = None,
        proxy_host: Optional[str] = None,
        proxy_port: Optional[int] = None,
    ) -> None:
        super(APNsCertConnectionPool, self).__init__(
            topic=topic,
            max_connections=max_connections,
            max_connection_attempts=max_connection_attempts,
            use_sandbox=use_sandbox,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
        )

        self.cert_file = cert_file
        self.ssl_context = ssl_context or ssl.create_default_context()
        if no_cert_validation:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        self.ssl_context.load_cert_chain(cert_file)

        if not self.apns_topic:
            with open(self.cert_file, "rb") as f:
                body = f.read()
                cert = OpenSSL.crypto.load_certificate(  # type: ignore
                    OpenSSL.crypto.FILETYPE_PEM, body  # type: ignore
                )
                self.apns_topic = cert.get_subject().UID

    async def create_connection(self) -> APNsBaseClientProtocol:
        apns_protocol_factory = partial(
            self.protocol_class,
            self.apns_topic,
            self.loop,
            self.discard_connection,
        )

        if self.proxy_host and self.proxy_port:
            return await self._create_proxy_connection(apns_protocol_factory)
        else:
            return await self._create_connection(apns_protocol_factory)

    async def _create_connection(
        self, apns_protocol_factory
    ) -> APNsBaseClientProtocol:
        _, protocol = await self.loop.create_connection(
            protocol_factory=apns_protocol_factory,
            host=self.protocol_class.APNS_SERVER,
            port=self.protocol_class.APNS_PORT,
            ssl=self.ssl_context,
        )
        return protocol


class APNsKeyConnectionPool(APNsBaseConnectionPool):
    def __init__(
        self,
        key_file: str,
        key_id: str,
        team_id: str,
        topic: str,
        max_connections: int = 10,
        max_connection_attempts: int = 5,
        use_sandbox: bool = False,
        ssl_context: Optional[ssl.SSLContext] = None,
        proxy_host: Optional[str] = None,
        proxy_port: Optional[int] = None,
    ) -> None:
        super(APNsKeyConnectionPool, self).__init__(
            topic=topic,
            max_connections=max_connections,
            max_connection_attempts=max_connection_attempts,
            use_sandbox=use_sandbox,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
        )

        self.ssl_context = ssl_context or ssl.create_default_context()

        self.key_id = key_id
        self.team_id = team_id

        with open(key_file) as f:
            self.key = f.read()

    async def create_connection(self) -> APNsBaseClientProtocol:
        auth_provider = JWTAuthorizationHeaderProvider(
            key=self.key, key_id=self.key_id, team_id=self.team_id
        )
        apns_protocol_factory = partial(
            self.protocol_class,
            self.apns_topic,
            self.loop,
            self.discard_connection,
            auth_provider,
        )

        if self.proxy_host and self.proxy_port:
            return await self._create_proxy_connection(apns_protocol_factory)
        else:
            return await self._create_connection(apns_protocol_factory)

    async def _create_connection(
        self, apns_protocol_factory
    ) -> APNsBaseClientProtocol:
        _, protocol = await self.loop.create_connection(
            protocol_factory=apns_protocol_factory,
            host=self.protocol_class.APNS_SERVER,
            port=self.protocol_class.APNS_PORT,
            ssl=self.ssl_context,
        )
        return protocol


class HttpProxyProtocol(asyncio.Protocol):
    def __init__(
        self,
        apns_host: str,
        apns_port: int,
        loop: asyncio.AbstractEventLoop,
        ssl_context: ssl.SSLContext,
        protocol_factory,
    ):
        self.apns_host = apns_host
        self.apns_port = apns_port
        self.buffer = bytearray()
        self.loop = loop
        self.ssl_context = ssl_context
        self.apns_protocol_factory = protocol_factory
        self.apns_protocol: Optional[APNsBaseClientProtocol] = None
        self.transport = None
        self.apns_connection_ready = (
            asyncio.Event()
        )  # Event to signal APNs readiness

    def connection_made(self, transport):
        logger.debug(
            "Proxy connection made.",
        )
        self.transport = transport
        connect_request = (
            f"CONNECT {self.apns_host}:{self.apns_port} "
            f"HTTP/1.1\r\nHost: "
            f"{self.apns_host}\r\nConnection: close\r\n\r\n"
        )
        self.transport.write(connect_request.encode("utf-8"))

    def data_received(self, data):
        # Data is usually received in bytes,
        # so you might want to decode or process it
        logger.debug("Raw data received: %s", data)
        self.buffer.extend(data)
        # some proxies send "HTTP/1.1 200 Connection established"
        # others "HTTP/1.1 200 Connected"
        if b"HTTP/1.1 200 Connect" in data:
            logger.debug(
                "Proxy tunnel established.",
            )
            asyncio.create_task(self.create_apns_connection())
        else:
            logger.debug(
                "Data received (before APNs connection establishment): %s",
                data.decode(),
            )

    async def create_apns_connection(self):
        # Use the existing transport to create a new APNs connection
        logger.debug(
            "Initiating APNs connection.",
        )
        sock = self.transport.get_extra_info("socket")
        _, self.apns_protocol = await self.loop.create_connection(
            self.apns_protocol_factory,
            server_hostname=self.apns_host,
            ssl=self.ssl_context,
            sock=sock,
        )
        # Signal that APNs connection is ready
        self.apns_connection_ready.set()

    def connection_lost(self, exc):
        logger.debug(
            "Proxy connection lost.",
        )
        self.transport.close()
