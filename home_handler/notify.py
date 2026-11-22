import logging

log = logging.getLogger("home_handler.notify")


def push(ha, message, title=None, url=None, tag="home-handler-evening"):
    """Send a phone notification through HA's script.notify_phone (phone only). Returns True on success."""
    data = {"tag": tag}
    if url:
        data.update(url=url, clickAction=url)
    body = {"message": message, "data": data}
    if title:
        body["title"] = title
    try:
        ha.rest("POST", "/api/services/script/notify_phone", body)
        return True
    except Exception as e:
        log.warning("push failed: %s", e)
        return False
