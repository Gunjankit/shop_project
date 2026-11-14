from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Item, OutboxMessage
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