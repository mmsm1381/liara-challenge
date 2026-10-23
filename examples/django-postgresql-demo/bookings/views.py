from django import forms
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from bookings.models import Booking


class BookingForm(forms.Form):
    name = forms.CharField(max_length=80, label="Your name")


@require_http_methods(["GET", "POST"])
def home(request):
    form = BookingForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        Booking.objects.create(name=form.cleaned_data["name"])
        return redirect("home")
    return render(request, "bookings/home.html", {"form": form, "bookings": Booking.objects.order_by("-created_at")[:20]})


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "reachable"})
