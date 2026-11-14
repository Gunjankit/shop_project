from . import ml
import re
from datetime import date, datetime, time, timedelta, timezone

from django.db.models import Count, Q, Sum

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

# from .models import Item, OutboxMessage
from .models import HistoricalOrder, Item, OutboxMessage
from .services import PurchaseError, record_purchase

CONFLICT_CODES = {"sold_out", "duplicate_purchase"}


def order_to_dict(order):
    return {
        "id": order.id,
        "email": order.customer.email,
        "sku": order.item.sku,
        "extraCode": order.extra.code if order.extra else None,
        "externalChargeId": order.external_charge_id,
        "totalCents": order.total_cents,
    }


class PurchaseView(APIView):
    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"error": "missing_fields"}, status=status.HTTP_400_BAD_REQUEST)

        email = data.get("email")
        sku = data.get("sku")
        charge_id = data.get("externalChargeId")
        extra_code = data.get("extraCode")

        required_ok = all(isinstance(v, str) and v.strip() for v in (email, sku, charge_id))
        extra_ok = extra_code is None or isinstance(extra_code, str)
        if not required_ok or not extra_ok:
            return Response({"error": "missing_fields"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            order = record_purchase(email, sku, charge_id, extra_code)
        except PurchaseError as exc:
            http_status = (
                status.HTTP_409_CONFLICT if exc.code in CONFLICT_CODES
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"error": exc.code}, status=http_status)

        http_status = status.HTTP_200_OK if order.replayed else status.HTTP_201_CREATED
        return Response(
            {"replayed": order.replayed, "order": order_to_dict(order)},
            status=http_status,
        )


class CatalogView(APIView):
    def get(self, request, sku):
        try:
            item = Item.objects.get(sku=sku)
        except Item.DoesNotExist:
            return Response({"error": "unknown_item"}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "sku": item.sku,
            "name": item.name,
            "priceCents": item.price_cents,
            "remaining": item.remaining,
        })


class OutboxView(APIView):
    def get(self, request):
        messages = OutboxMessage.objects.order_by("id")
        return Response([
            {
                "toEmail": m.to_email,
                "kind": m.kind,
                "externalChargeId": m.external_charge_id,
            }
            for m in messages
        ])


PAGE_SIZE = 10
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BadParam(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_day(value):
   
    if not DATE_RE.match(value):
        raise BadParam("invalid_date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BadParam("invalid_date")


def utc_midnight(day):
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def apply_date_window(qs, params):
    
    start = params.get("from")
    end = params.get("to")
    try:
        if start:
            qs = qs.filter(created_at__gte=utc_midnight(parse_day(start)))
        if end:
            next_day = parse_day(end) + timedelta(days=1)
            qs = qs.filter(created_at__lt=utc_midnight(next_day))
    except OverflowError:
        raise BadParam("invalid_date")
    return qs


def parse_page(value):
    if value is None or value == "":
        return 1
    if not value.isdigit() or int(value) < 1:
        raise BadParam("invalid_page")
    return int(value)


def history_to_dict(h):
    return {
        "orderId": h.order_id,
        "email": h.email,
        "sku": h.sku,
        "extraCode": h.extra_code,
        "channel": h.channel,
        "createdAt": h.created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalCents": h.total_cents,
        "returned": h.returned,
    }


class HistoryListView(APIView):
    def get(self, request):
        params = request.query_params
        try:
            qs = apply_date_window(HistoricalOrder.objects.all(), params)
            page = parse_page(params.get("page"))
        except BadParam as exc:
            return Response({"error": exc.code}, status=status.HTTP_400_BAD_REQUEST)

        if params.get("sku"):
            qs = qs.filter(sku=params["sku"])
        if params.get("channel"):
            qs = qs.filter(channel=params["channel"])

        qs = qs.order_by("created_at", "order_id")
        count = qs.count()
        start = (page - 1) * PAGE_SIZE
        rows = qs[start:start + PAGE_SIZE]

        return Response({
            "count": count,
            "page": page,
            "results": [history_to_dict(h) for h in rows],
        })


class HistoryStatsView(APIView):
    def get(self, request):
        try:
            qs = apply_date_window(HistoricalOrder.objects.all(), request.query_params)
        except BadParam as exc:
            return Response({"error": exc.code}, status=status.HTTP_400_BAD_REQUEST)

        grouped = (
            qs.order_by()
            .values("sku")
            .annotate(
                orders=Count("id"),
                revenue=Sum("total_cents"),
                returned_count=Count("id", filter=Q(returned=True)),
            )
        )
        by_sku = {row["sku"]: row for row in grouped}

        result = {}
        for sku in ("lamp", "stool"):
            row = by_sku.get(sku)
            if row is None:
                result[sku] = {"orders": 0, "revenueCents": 0, "returnRate": None}
            else:
                result[sku] = {
                    "orders": row["orders"],
                    "revenueCents": row["revenue"],
                    "returnRate": round(row["returned_count"] / row["orders"], 2),
                }
        return Response(result)    


VALID_CHANNELS = {"web", "app", "store"}


def parse_aware_datetime(value):
    """ISO datetime (timezone ke saath) ko aware datetime banao, warna None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$", value):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


class ReturnRiskView(APIView):
    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"error": "missing_fields"}, status=status.HTTP_400_BAD_REQUEST)

        # Model hai?
        model, version = ml.get_model()
        if model is None:
            return Response({"error": "model_not_trained"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        email = data.get("email")
        sku = data.get("sku")
        channel = data.get("channel")
        extra_code = data.get("extraCode")
        created_at = parse_aware_datetime(data.get("createdAt"))

        if not all(isinstance(v, str) and v.strip() for v in (email, sku, channel)):
            return Response({"error": "missing_fields"}, status=status.HTTP_400_BAD_REQUEST)
        if extra_code is not None and not isinstance(extra_code, str):
            return Response({"error": "missing_fields"}, status=status.HTTP_400_BAD_REQUEST)
        if created_at is None:
            return Response({"error": "invalid_created_at"}, status=status.HTTP_400_BAD_REQUEST)

        email = email.strip().lower()
        sku = sku.strip().lower()
        channel = channel.strip().lower()
        extra_code = (extra_code or "").strip().lower() or None

        if sku not in ("lamp", "stool"):
            return Response({"error": "unknown_item"}, status=status.HTTP_400_BAD_REQUEST)
        if channel not in VALID_CHANNELS:
            return Response({"error": "invalid_channel"}, status=status.HTTP_400_BAD_REQUEST)
        if extra_code is not None and not (extra_code == "cushion" and sku == "stool"):
            return Response({"error": "extra_not_allowed"}, status=status.HTTP_400_BAD_REQUEST)

        prior_orders = HistoricalOrder.objects.filter(
            email=email, created_at__lt=created_at
        ).count()

        row = ml.build_feature_row(sku, extra_code, channel, created_at, prior_orders)
        probability = float(model.predict_proba([row])[0][1])

        return Response({
            "probability": round(probability, 3),
            "modelVersion": version,
        })    