from aioapns.connection import APNsConnectionPool
from aioapns.logging import logger


class APNs:
    def __init__(self, client_cert, max_connections=10, loop=None):
        self.pool = APNsConnectionPool(client_cert, max_connections, loop)

    async def send_notification(self, request):
        response = await self.pool.send_notification(request)
        if not response.is_successful:
            logger.error(
                'Status of notification %s is %s (%s)',
                request.notification_id, response.status, response.description
            )
        return response
