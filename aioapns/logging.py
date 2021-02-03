import logging

logging.getLogger("hpack").setLevel(logging.CRITICAL)

logger = logging.getLogger("aioapns")


def set_hpack_debugging(value):
    if value:
        logging.getLogger("hpack").setLevel(logging.DEBUG)
