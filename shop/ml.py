import json
from datetime import timezone

import joblib
from django.conf import settings
from datetime import timezone

FEATURES = [
    "is_stool",
    "has_extra",
    "channel_app",
    "channel_web",
    "hour",
    "prior_orders",
]


def build_feature_row(sku, extra_code, channel, created_at, prior_orders):
    """Ek order ke 6 numbers banao. Order FEATURES list jaisa hi hona chahiye."""
    created_at = created_at.astimezone(timezone.utc)
    return [
        1 if sku == "stool" else 0,
        1 if extra_code else 0,
        1 if channel == "app" else 0,
        1 if channel == "web" else 0,
        created_at.hour,
        prior_orders,
    ]

_cache = {"model": None, "version": None, "mtime": None}


def get_model():
 
    path = settings.MODEL_DIR / "return_model.joblib"
    meta = settings.MODEL_DIR / "metrics.json"
    if not path.exists() or not meta.exists():
        return None, None

    mtime = (path.stat().st_mtime_ns, meta.stat().st_mtime_ns)
    if _cache["model"] is None or _cache["mtime"] != mtime:
        _cache["model"] = joblib.load(path)
        _cache["version"] = json.loads(meta.read_text())["version"]
        _cache["mtime"] = mtime
    return _cache["model"], _cache["version"]