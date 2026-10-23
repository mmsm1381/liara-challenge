from unittest.mock import patch
from django.db import DatabaseError
from django.test import Client, TestCase
from bookings.models import Booking


class BookingTests(TestCase):
    def test_save_and_reload_booking(self):
        self.assertRedirects(self.client.post("/", {"name": "Sara"}), "/")
        self.assertEqual(Booking.objects.get().name, "Sara")
        self.assertContains(self.client.get("/"), "Sara")

    def test_invalid_name_preserves_database(self):
        self.client.post("/", {"name": ""})
        self.assertFalse(Booking.objects.exists())

    def test_booking_submission_requires_csrf(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post("/", {"name": "Sara"}).status_code, 403)

    def test_health_observes_database(self):
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok", "database": "reachable"})

    def test_health_failure_does_not_claim_availability(self):
        with patch("bookings.views.connection.cursor", side_effect=DatabaseError):
            self.assertEqual(self.client.get("/health/").status_code, 503)
