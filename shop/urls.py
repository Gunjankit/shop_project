from django.urls import path, re_path

from .views import CatalogView, OutboxView, PurchaseView

urlpatterns = [
    path("purchases/", PurchaseView.as_view()),
    re_path(r"^catalog/(?P<sku>[^/]+)/?$", CatalogView.as_view()),
    re_path(r"^outbox/?$", OutboxView.as_view()),
]