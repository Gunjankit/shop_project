import json
import uuid
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone

import joblib
import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

from shop.ml import FEATURES, build_feature_row
from shop.models import HistoricalOrder


class Command(BaseCommand):
    help = "Train the return-risk model on HistoricalOrder and save it to models/."

    def handle(self, *args, **options):
        rows = list(
            HistoricalOrder.objects.values_list(
                "email", "sku", "extra_code", "channel", "created_at", "returned"
            ).iterator()
        )
        if len(rows) < 10:
            raise CommandError("Not enough history. Run import_history first.")

        # Har email ke saare order-times (sorted)
        times_by_email = defaultdict(list)
        for email, _sku, _extra, _channel, created_at, _returned in rows:
            times_by_email[email].append(created_at)
        for times in times_by_email.values():
            times.sort()

        # Features (X) aur jawab (y)
        X = []
        y = []
        for email, sku, extra_code, channel, created_at, returned in rows:
            prior_orders = bisect_left(times_by_email[email], created_at)
            X.append(build_feature_row(sku, extra_code, channel, created_at, prior_orders))
            y.append(1 if returned else 0)
        X = np.array(X)
        y = np.array(y)

        if min(np.bincount(y, minlength=2)) < 2:
            raise CommandError("Need at least 2 returned and 2 not-returned orders.")

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=0, stratify=y
        )

        model = LogisticRegression(max_iter=1000, random_state=0)
        model.fit(X_train, y_train)

        holdout_accuracy = float(model.score(X_test, y_test))
        majority_share = max(float(y_test.mean()), 1 - float(y_test.mean()))

        version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        metrics = {
            "version": version,
            "rows": int(len(y)),
            "trainRows": int(len(y_train)),
            "holdoutAccuracy": round(holdout_accuracy, 4),
            "baselineAccuracy": round(majority_share, 4),
            "features": FEATURES,
        }

        model_dir = settings.MODEL_DIR
        model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, model_dir / "return_model.joblib")
        (model_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

        self.stdout.write(json.dumps(metrics))