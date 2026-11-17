"""Log session ids and session start failures (the bidi_session fixture swallows the latter)."""
import os
import traceback

import webdriver

_orig = webdriver.Session.start


def start(self, *args, **kwargs):
    try:
        result = _orig(self, *args, **kwargs)
    except Exception:
        with open(os.environ["SESSION_ERR_LOG"], "a") as f:
            f.write(os.environ.get("PYTEST_CURRENT_TEST", "?") + "\n" + traceback.format_exc(limit=-2) + "\n")
        raise
    if self.session_id and os.environ.get("SESSION_ID_LOG"):
        with open(os.environ["SESSION_ID_LOG"], "a") as f:
            f.write(self.session_id + "\n")
    return result


webdriver.Session.start = start
