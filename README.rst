aioapns - An efficient APNs Client Library for Python/asyncio
=================================================================================

.. image:: https://travis-ci.org/Fatal1ty/aioapns.svg?branch=master
    :target: https://travis-ci.org/Fatal1ty/aioapns

.. image:: https://requires.io/github/Fatal1ty/aioapns/requirements.svg?branch=master
     :target: https://requires.io/github/Fatal1ty/aioapns/requirements/?branch=master
     :alt: Requirements Status

.. image:: https://img.shields.io/pypi/v/aioapns.svg
    :target: https://pypi.python.org/pypi/aioapns

.. image:: https://img.shields.io/pypi/pyversions/aioapns.svg
    :target: https://pypi.python.org/pypi/aioapns/

.. image:: https://img.shields.io/badge/License-Apache%202.0-blue.svg
    :target: https://opensource.org/licenses/Apache-2.0

**aioapns** is a library designed specifically for sending push-notifications to iOS devices
via Apple Push Notification Service. aioapns provides an efficient client through
asynchronous HTTP2 protocol for use with Python's ``asyncio``
framework.

aioapns requires Python 3.5 or later.


Performance
-----------

In my testing aioapns allows you to send on average 1.3k notifications per second on a single core.


Features
--------

* Internal connection pool which adapts to the current load
* Ability to set TTL (time to live) for notifications
* Ability to set priority for notifications
* Ability to set collapse-key for notifications
* Ability to use production or development APNs server


Installation
------------

Use pip to install::

    $ pip install aioapns


Basic Usage
-----------

.. code-block:: python

    from uuid import uuid4
    from aioapns import APNs, NotificationRequest


    async def run():
        apns = APNs('/path/to/apns-production-cert.pem', use_sandbox=False)
        request = NotificationRequest(
            device_token='<DEVICE_TOKEN>',
            message = {
                "aps": {
                    "alert": "Hello from APNs",
                    "badge": "1",
                }
            }
            notification_id=str(uuid4())  # optional
            time_to_live=3,               # optional
        )
        await apns.send_notification(request)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run())


License
-------

aioapns is developed and distributed under the Apache 2.0 license.
