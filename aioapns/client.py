from aioapns.connection import APNsCertConnectionPool, APNsKeyConnectionPool
from aioapns.exceptions import ImproperlyConfigured
from aioapns.logging import logger


class APNs:
    def __init__(self, client_cert=None, team_id=None, bundle_id=None, auth_key_id=None, auth_key=None,
                 max_connections=10, loop=None, use_sandbox=False):
        if client_cert:
            self.pool = APNsCertConnectionPool(client_cert, max_connections, loop, use_sandbox)
        elif all([team_id, bundle_id, auth_key_id, auth_key]):
            self.pool = APNsKeyConnectionPool(team_id=team_id, bundle_id=bundle_id, auth_key_id=auth_key_id,
                                              auth_key=auth_key, max_connections=max_connections, loop=loop,
                                              use_sandbox=use_sandbox)
        else:
            raise ImproperlyConfigured('You must provide either APNs cert file path or an auth key credentials')

    async def send_notification(self, request):
        response = await self.pool.send_notification(request)
        if not response.is_successful:
            logger.error(
                'Status of notification %s is %s (%s)',
                request.notification_id, response.status, response.description
            )
        return response
