import asyncio
import logging

import uvloop

from aioapns import APNs, NotificationRequest


def setup_logger(log_level):
    log_level = getattr(logging, log_level)
    logging.basicConfig(
        format="[%(asctime)s] %(levelname)8s %(module)6s:%(lineno)03d %(message)s",
        level=log_level,
    )


if __name__ == "__main__":
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    setup_logger("INFO")

    client_cert = "apns-production-cert.pem"
    device_token = "<DEVICE_TOKEN>"
    message = {
        "aps": {
            "alert": "Hello from APNs Tester.",
            "badge": "1",
            "sound": "default",
        }
    }

    apns = APNs(client_cert)

    async def send_request():
        request = NotificationRequest(
            device_token=device_token, message=message
        )
        await apns.send_notification(request)

    async def main():
        send_requests = [send_request() for _ in range(1000)]
        import time

        t = time.time()
        await asyncio.wait(send_requests)
        print("Done: %s" % (time.time() - t))
        print()

    try:
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(main())
        loop.run_forever()
    except KeyboardInterrupt:
        pass
