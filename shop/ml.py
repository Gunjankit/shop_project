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