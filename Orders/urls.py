from django.urls import path

from Orders import views


urlpatterns = [
    path("dashboard-version/", views.order_dashboard_version, name="order_dashboard_version"),
    path("<int:pk>/live-status/refresh/", views.refresh_order_train_status, name="order_train_status_refresh"),
    path("<int:pk>/bill/", views.bill_print, name="order_bill"),
    path("", views.order_list, name="order_list"),
]
