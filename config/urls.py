from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def api_root(request):
    return JsonResponse(
        {
            "message": "Fuel Route API is running.",
            "usage": "Send POST requests to /api/route/ with JSON: {'start': 'New York, NY', 'finish': 'Chicago, IL'}",
        }
    )


urlpatterns = [
    path("", api_root),
    path("admin/", admin.site.urls),
    path("api/", include("routing.urls")),
]