"""Small HTTP request-body helpers shared by the REST entry points."""

CHUNK = 64 * 1024
DEFAULT_DRAIN_CAP = 16 * 1024 * 1024


def discard_body(reader, length, cap=DEFAULT_DRAIN_CAP):
    """Drop a rejected request body so the following error can reach the caller.

    Closing a connection while request bytes are still unread makes the kernel
    answer with RST, which can discard the 400 response the caller needs. Read at
    most `cap` bytes; beyond that we do not read for the attacker and the caller
    should just close. Returns True when the whole declared body was consumed.
    """
    length = max(int(length), 0)
    if length > cap:
        return False
    remaining = length
    while remaining > 0:
        chunk = reader.read(min(CHUNK, remaining))
        if not chunk:
            return True
        remaining -= len(chunk)
    return True
