import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)
PROCUREMENT = (
    "Przedmiotem jest dostawa i montaż instalacji PV wraz z magazynem energii "
    "o pojemności 250–270 kWh dla zakładu produkcyjnego."
)


def detail(
    source_id: int = 101,
    *,
    version: int = 501,
    status: str = "PUBLISHED",
    deadline: str | None = "2026-01-10 23:59:59",
    description: str = PROCUREMENT,
    title: str = "Instalacja fotowoltaiczna",
    nip: str = "1234563218",
) -> dict[str, Any]:
    return {
        "advertisement": {
            "id": version,
            "title": title,
            "publication_date": "2026-01-04",
            "submission_deadline": deadline,
            "advertisement": {
                "id": source_id,
                "publication_date": "2026-01-03",
                "status": {"label": status},
            },
            "advertiser_address_details": {
                "name": "Przykładowa spółka",
                "identification_number_type": "nip",
                "identification_number": nip,
            },
            "contact_persons": [{"email": "private@example.test"}],
            "orders": [
                {
                    "title": title,
                    "order_items": [
                        {
                            "description": description + "\nKontakt: private@example.test",
                            "category": {"name": "Dostawa"},
                        }
                    ],
                }
            ],
        }
    }


class MockBK:
    def __init__(self) -> None:
        self.details: dict[int, dict[str, Any] | int] = {101: detail()}
        self.search: Callable[[httpx.QueryParams], tuple[list[int], int]] = lambda _: (
            list(self.details),
            len(self.details),
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "bazakonkurencyjnosci.funduszeeuropejskie.gov.pl"
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "search":
            ids, total = self.search(request.url.params)
            payload = {"advertisements": [{"id": item} for item in ids], "meta": {"total": total}}
        else:
            payload = self.details[int(endpoint)]
            if isinstance(payload, int):
                return httpx.Response(payload, text="upstream failure")
        return httpx.Response(200, content=json.dumps({"status": "OK", "data": payload}))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


@pytest.fixture
def bk() -> MockBK:
    return MockBK()
