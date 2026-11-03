"""Live Mercadona API smoke test. Deselected unless --run-network is passed."""

import pytest
import requests

from supermarket import mercadona


@pytest.mark.network
def test_categories_endpoint_returns_level_one_with_children():
    payload = mercadona.fetch_json(requests.Session(), mercadona.CATEGORIES_URL, timeout=30)

    results = payload.get("results")
    assert isinstance(results, list) and results
    assert any(entry.get("categories") for entry in results)
