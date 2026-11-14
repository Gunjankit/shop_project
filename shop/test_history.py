import json
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from . import ml
from .models import HistoricalOrder, Item

CSV_PATH = settings.BASE_DIR / "order_history.csv"
PREDICT_URL = "/api/predictions/return-risk"
PREDICT_BODY = {
    "email": "new@example.com",
    "sku": "lamp",
    "extraCode": None,
    "channel": "app",
    "createdAt": "2026-07-01T22:15:00Z",
}


def run_import():
    out = StringIO()
    call_command("import_history", str(CSV_PATH), stdout=out)
    return json.loads(out.getvalue().strip().splitlines()[-1])


class ImportHistoryTests(TestCase):
    def test_import_counts_and_reasons(self):
        result = run_import()

        self.assertEqual(result["created"], 112)
        self.assertEqual(result["skippedExisting"], 0)
        self.assertEqual(
            result["rejected"],
            {
                "duplicate_order_id": 2,
                "invalid_email": 2,
                "unknown_sku": 1,
                "extra_not_allowed": 1,
                "bad_total": 1,
                "bad_date": 2,
                "bad_returned": 1,
            },
        )
        self.assertEqual(HistoricalOrder.objects.count(), 112)

    def test_rows_are_cleaned(self):
        run_import()

        ada = HistoricalOrder.objects.get(order_id="H0003")
        self.assertEqual(ada.email, "ada@example.com")
        self.assertEqual(HistoricalOrder.objects.get(order_id="H0006").sku, "lamp")
        self.assertTrue(HistoricalOrder.objects.get(order_id="H0010").returned)
        self.assertFalse(HistoricalOrder.objects.get(order_id="H0015").returned)
        self.assertIsNone(HistoricalOrder.objects.get(order_id="H0002").extra_code)
        for bad in ("H0012", "H0024", "H0032", "H0041", "H0048", "H0056", "H0064", "H0071"):
            self.assertFalse(HistoricalOrder.objects.filter(order_id=bad).exists(), bad)

    def test_second_import_creates_nothing(self):
        run_import()
        snapshot = list(HistoricalOrder.objects.order_by("order_id").values())

        second = run_import()

        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skippedExisting"], 112)
        self.assertEqual(sum(second["rejected"].values()), 10)
        self.assertEqual(list(HistoricalOrder.objects.order_by("order_id").values()), snapshot)

    def test_import_does_not_touch_live_stock(self):
        run_import()

        self.assertEqual(Item.objects.get(sku="lamp").remaining, 2)
        self.assertEqual(Item.objects.get(sku="stool").remaining, 1)


class HistoryApiTests(TestCase):
    def setUp(self):
        run_import()

    def test_list_pagination_and_order(self):
        first = self.client.get("/api/history/").json()
        self.assertEqual(first["count"], 112)
        self.assertEqual(first["page"], 1)
        self.assertEqual(len(first["results"]), 10)
        self.assertEqual(first["results"][0]["orderId"], "H0046")
        self.assertEqual(first["results"][0]["createdAt"], "2026-05-01T10:31:00Z")

        last = self.client.get("/api/history/?page=12").json()
        self.assertEqual(len(last["results"]), 2)

        beyond = self.client.get("/api/history/?page=13").json()
        self.assertEqual(beyond["results"], [])
        self.assertEqual(beyond["count"], 112)

    def test_filters(self):
        data = self.client.get("/api/history/?sku=stool&channel=app").json()
        self.assertGreater(data["count"], 0)
        for r in data["results"]:
            self.assertEqual((r["sku"], r["channel"]), ("stool", "app"))

    def test_date_window_is_inclusive_on_both_ends(self):
        data = self.client.get("/api/history/?from=2026-05-01&to=2026-05-01").json()
        self.assertEqual([r["orderId"] for r in data["results"]], ["H0046", "H0013"])

    def test_bad_params(self):
        r = self.client.get("/api/history/?from=abc")
        self.assertEqual((r.status_code, r.json()), (400, {"error": "invalid_date"}))
        r = self.client.get("/api/history/?page=0")
        self.assertEqual((r.status_code, r.json()), (400, {"error": "invalid_page"}))

    def test_stats_for_may_2026(self):
        r = self.client.get("/api/history/stats?from=2026-05-01&to=2026-05-31")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json(),
            {
                "lamp": {"orders": 43, "revenueCents": 344000, "returnRate": 0.28},
                "stool": {"orders": 20, "revenueCents": 265000, "returnRate": 0.3},
            },
        )

    def test_stats_for_empty_window(self):
        r = self.client.get("/api/history/stats?from=2030-01-01&to=2030-01-31").json()
        empty = {"orders": 0, "revenueCents": 0, "returnRate": None}
        self.assertEqual(r, {"lamp": empty, "stool": empty})


class ReturnRiskTests(TestCase):
    def setUp(self):
        ml._cache.update({"model": None, "version": None, "mtime": None})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = self.settings(MODEL_DIR=Path(tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        run_import()

    def post(self, **changes):
        return self.client.post(
            PREDICT_URL, {**PREDICT_BODY, **changes}, content_type="application/json"
        )

    def test_503_before_training(self):
        r = self.post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "model_not_trained"})

    def test_prediction_after_training(self):
        call_command("train_return_model", stdout=StringIO())
        metrics = json.loads((settings.MODEL_DIR / "metrics.json").read_text())

        r = self.post()

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["probability"], 0)
        self.assertLessEqual(body["probability"], 1)
        self.assertEqual(body["probability"], round(body["probability"], 3))
        self.assertEqual(body["modelVersion"], metrics["version"])

    def test_metrics_file_contents(self):
        call_command("train_return_model", stdout=StringIO())
        metrics = json.loads((settings.MODEL_DIR / "metrics.json").read_text())

        self.assertEqual(metrics["rows"], 112)
        self.assertEqual(metrics["trainRows"], 89)
        self.assertEqual(metrics["features"], ml.FEATURES)
        self.assertTrue((settings.MODEL_DIR / "return_model.joblib").exists())

    def test_retraining_changes_version_and_api_sees_it(self):
        call_command("train_return_model", stdout=StringIO())
        v1 = self.post().json()["modelVersion"]
        call_command("train_return_model", stdout=StringIO())
        v2 = self.post().json()["modelVersion"]
        self.assertNotEqual(v1, v2)

    def test_unknown_sku_is_400_after_training(self):
        call_command("train_return_model", stdout=StringIO())
        r = self.post(sku="desk")
        self.assertEqual((r.status_code, r.json()), (400, {"error": "unknown_item"}))