from django.urls import path
from bookings import views

urlpatterns = [path("", views.home, name="home"), path("health/", views.health)]
