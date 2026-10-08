from django.urls import path
from .views import route_view

urlpatterns = [
    path("route/", route_view),
]   