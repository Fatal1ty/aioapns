import time
import json
import asyncio
from ssl import SSLContext
from functools import partial

import OpenSSL
import jwt
from h2.connection import H2Connection
from h2.events import ResponseReceived, DataReceived, RemoteSettingsChanged,\
    StreamEnded, ConnectionTerminated, WindowUpdated
from h2.exceptions import NoAvailableStreamIDError, FlowControlError
from h2.settings import SettingCodes

from aioapns.common import NotificationResult, DynamicBoundedSemaphore, \
    APNS_RESPONSE_CODE, wrap_private_key
from aioapns.exceptions import ConnectionClosed
from aioapns.logging import logger


class ChannelPool(DynamicBoundedSemaphore):
    def __init__(self, *args, **kwargs):
        super(ChannelPool, self).__init__(*args, **kwargs)
        self._stream_id = -1

    async def acquire(self):
        await super(ChannelPool, self).acquire()
        self._stream_id += 2
        if self._stream_id > H2Connection.HIGHEST_ALLOWED_STREAM_ID:
            raise NoAvailableStreamIDError()
        return self._stream_id

    @property
    def is_busy(self):
        return self._value <= 0


class H2Protocol(asyncio.Protocol):
    def __init__(self):
        self.transport = None
        self.conn = H2Connection()
        self.free_channels = ChannelPool(1000)

    def connection_made(self, transport):
        self.transport = transport
        self.conn.initiate_connection()
        self.flush()

    def data_received(self, data):
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
            else:
                logger.warning('Unknown event: %s', event)
        self.flush()

    def flush(self):
        self.transport.write(self.conn.data_to_send())

    def on_response_received(self, headers):
        pass

    def on_data_received(self, data, stream_id):
        pass

    def on_remote_settings_changed(self, changed_settings):
        for setting in changed_settings.values():
            logger.debug('Remote setting changed: %s', setting)
            if setting.setting == SettingCodes.MAX_CONCURRENT_STREAMS:
                self.free_channels.bound = setting.new_value

    def on_stream_ended(self, stream_id):
        if stream_id % 2 == 0:
            logger.warning('End stream: %d', stream_id)
        self.free_channels.release()

    def on_connection_terminated(self, event):
        pass


class APNsBaseClientProtocol(H2Protocol):
    APNS_SERVER = 'api.push.apple.com'
    INACTIVITY_TIME = 10

    def __init__(self, apns_topic, auth_token=None, loop=None, on_connection_lost=None):
        super(APNsBaseClientProtocol, self).__init__()
        self.apns_topic = apns_topic
        self.auth_token = auth_token
        self.loop = loop or asyncio.get_event_loop()
        self.on_connection_lost = on_connection_lost

        self.requests = {}
        self.request_streams = {}
        self.request_statuses = {}
        self.inactivity_timer = None

    def connection_made(self, transport):
        super(APNsBaseClientProtocol, self).connection_made(transport)
        self.refresh_inactivity_timer()

    async def send_notification(self, request):
        stream_id = await self.free_channels.acquire()

        headers = [
            (':method', 'POST'),
            (':scheme', 'https'),
            (':path', '/3/device/%s' % request.device_token),
            ('host', self.APNS_SERVER),
            ('apns-id', request.notification_id),
            ('apns-topic', self.apns_topic)
        ]

        if self.auth_token:
            headers.append(('authorization', 'bearer {0}'.format(self.auth_token.decode('ascii'))))

        if request.time_to_live is not None:
            expiration = int(time.time()) + request.time_to_live
            headers.append(('apns-expiration', str(expiration)))
        if request.priority is not None:
            headers.append(('apns-priority', str(request.priority)))
        if request.collapse_key is not None:
            headers.append(('apns-collapse-id', request.collapse_key))

        self.conn.send_headers(
            stream_id=stream_id,
            headers=headers
        )
        try:
            data = json.dumps(request.message).encode()
            self.conn.send_data(
                stream_id=stream_id,
                data=data,
                end_stream=True,
            )
        except FlowControlError:
            raise

        self.flush()

        future_response = asyncio.Future()
        self.requests[request.notification_id] = future_response
        self.request_streams[stream_id] = request.notification_id

        response = await future_response
        return response

    def flush(self):
        self.refresh_inactivity_timer()
        self.transport.write(self.conn.data_to_send())

    def refresh_inactivity_timer(self):
        if self.inactivity_timer:
            self.inactivity_timer.cancel()
        self.inactivity_timer = self.loop.call_later(
            self.INACTIVITY_TIME, self.close)

    @property
    def is_busy(self):
        return self.free_channels.is_busy

    def close(self):
        raise NotImplementedError

    def connection_lost(self, exc):
        logger.debug('Connection %s lost!', self)

        if self.inactivity_timer:
            self.inactivity_timer.cancel()

        if self.on_connection_lost:
            self.on_connection_lost(self)

        closed_connection = ConnectionClosed()
        for request in self.requests.values():
            request.set_exception(closed_connection)
        self.free_channels.destroy(closed_connection)

    def on_response_received(self, headers):
        notification_id = headers.get(b'apns-id').decode('utf8')
        status = headers.get(b':status').decode('utf8')
        if status == APNS_RESPONSE_CODE.SUCCESS:
            request = self.requests.pop(notification_id, None)
            if request:
                result = NotificationResult(notification_id, status)
                request.set_result(result)
            else:
                logger.warning(
                    'Got response for unknown notification request %s',
                    notification_id)
        else:
            self.request_statuses[notification_id] = status

    def on_data_received(self, data, stream_id):
        data = json.loads(data.decode())
        reason = data.get('reason', '')
        if not reason:
            return

        notification_id = self.request_streams.pop(stream_id, None)
        if notification_id:
            request = self.requests.pop(notification_id, None)
            if request:
                # TODO: Теоретически здесь может быть ошибка, если нет ключа
                status = self.request_statuses.pop(notification_id)
                result = NotificationResult(notification_id, status,
                                            description=reason)
                request.set_result(result)
            else:
                logger.warning('Could not find request %s', notification_id)
        else:
            logger.warning('Could not find notification by stream %s',
                           stream_id)

    def on_connection_terminated(self, event):
        logger.warning(
            'Connection %s terminated: code=%s, additional_data=%s, '
            'last_stream_id=%s', self, event.error_code,
            event.additional_data, event.last_stream_id)
        self.close()


class APNsTLSClientProtocol(APNsBaseClientProtocol):
    APNS_PORT = 443

    def close(self):
        if self.inactivity_timer:
            self.inactivity_timer.cancel()
        logger.debug('Closing connection %s', self)
        self.transport._ssl_protocol._transport.close()


class APNsProductionClientProtocol(APNsTLSClientProtocol):
    APNS_SERVER = 'api.push.apple.com'


class APNsDevelopmentClientProtocol(APNsTLSClientProtocol):
    APNS_SERVER = 'api.development.push.apple.com'


class APNsBaseConnectionPool:
    MAX_ATTEMPTS = 10

    def __init__(self, max_connections=10, loop=None, use_sandbox=False):
        self.max_connections = max_connections

        if use_sandbox:
            self.protocol_class = APNsDevelopmentClientProtocol
        else:
            self.protocol_class = APNsProductionClientProtocol

        self.loop = loop or asyncio.get_event_loop()
        self.connections = []
        self._lock = asyncio.Lock(loop=self.loop)

    async def connect(self):
        raise NotImplementedError

    def close(self):
        for connection in self.connections:
            connection.close()

    def discard_connection(self, connection):
        logger.debug('Connection %s discarded', connection)
        self.connections.remove(connection)
        logger.info('Connection released (total: %d)',
                    len(self.connections))

    async def acquire(self):
        for connection in self.connections:
            if not connection.is_busy:
                return connection
        else:
            await self._lock.acquire()
            for connection in self.connections:
                if not connection.is_busy:
                    self._lock.release()
                    return connection
            if len(self.connections) < self.max_connections:
                try:
                    connection = await self.connect()
                except Exception as e:
                    logger.error('Could not connect to server: %s', str(e))
                    self._lock.release()
                    raise ConnectionError()
                self.connections.append(connection)
                self._lock.release()
                return connection
            else:
                self._lock.release()
                logger.warning('Pool is busy, wait...')
                while True:
                    await asyncio.sleep(0.01)
                    for connection in self.connections:
                        if not connection.is_busy:
                            return connection

    async def send_notification(self, request):
        attempt = 0
        while True:
            attempt += 1
            if attempt > self.MAX_ATTEMPTS:
                logger.warning('Trying to send notification %s: attempt #%s',
                               request.notification_id, attempt)
            logger.debug('Notification %s: waiting for connection',
                         request.notification_id)
            try:
                connection = await self.acquire()
            except ConnectionError:
                logger.warning('Could not send notification %s: '
                               'ConnectionError', request.notification_id)
                await asyncio.sleep(1)
                continue
            logger.debug('Notification %s: connection %s acquired',
                         request.notification_id, connection)
            try:
                response = await connection.send_notification(request)
                return response
            except NoAvailableStreamIDError:
                connection.close()
            except ConnectionClosed:
                logger.warning('Could not send notification %s: '
                               'ConnectionClosed', request.notification_id)
            except FlowControlError:
                logger.debug('Got FlowControlError for notification %s',
                             request.notification_id)
                await asyncio.sleep(1)


class APNsCertConnectionPool(APNsBaseConnectionPool):

    def __init__(self, cert_file, max_connections=10, loop=None,
                 use_sandbox=False):
        self.cert_file = cert_file
        self.ssl_context = SSLContext()
        self.ssl_context.load_cert_chain(cert_file)

        super(APNsCertConnectionPool, self).__init__(
            max_connections=max_connections,
            loop=loop,
            use_sandbox=use_sandbox
        )

        with open(self.cert_file, 'rb') as f:
            body = f.read()
            cert = OpenSSL.crypto.load_certificate(
                OpenSSL.crypto.FILETYPE_PEM, body
            )
            self.apns_topic = cert.get_subject().UID

    async def connect(self):
        _, protocol = await self.loop.create_connection(
            protocol_factory=partial(
                self.protocol_class,
                self.apns_topic,
                None,
                self.loop,
                self.discard_connection
            ),
            host=self.protocol_class.APNS_SERVER,
            port=self.protocol_class.APNS_PORT,
            ssl=self.ssl_context
        )
        logger.info('Connection established (total: %d)',
                    len(self.connections) + 1)
        return protocol


class APNsKeyConnectionPool(APNsBaseConnectionPool):

    def __init__(self, team_id, bundle_id=None, auth_key_id=None, auth_key=None, max_connections=10, loop=None,
                 use_sandbox=False):

        self.auth_token = jwt.encode({
                'iss': team_id,
                'iat': time.time()
            },
            auth_key,
            algorithm='ES256',
            headers={
                'alg': 'ES256',
                'kid': auth_key_id,
            }
        )

        self.apns_topic = bundle_id

        super(APNsKeyConnectionPool, self).__init__(max_connections=max_connections, loop=loop, use_sandbox=use_sandbox)

    async def connect(self):
        _, protocol = await self.loop.create_connection(
            protocol_factory=partial(
                self.protocol_class,
                self.apns_topic,
                self.auth_token,
                self.loop,
                self.discard_connection
            ),
            host=self.protocol_class.APNS_SERVER,
            port=self.protocol_class.APNS_PORT,
            ssl=True
        )
        logger.info('Connection established (total: %d)',
                    len(self.connections) + 1)
        return protocol