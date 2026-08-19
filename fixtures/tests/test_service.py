"""Tests for the service layer."""


def test_service_handle():
    from app.service import Service

    service = Service()
    assert service.handle("abc") == "ABC"
