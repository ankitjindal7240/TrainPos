from django.urls import path

from Orders import views


urlpatterns = [
    path("<int:pk>/bill/", views.bill_print, name="order_bill"),
    path("", views.order_list, name="order_list"),
]
