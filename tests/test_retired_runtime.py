import asyncio
import json

from app.retired_main import RETIREMENT_VERSION, app, health, retired


def test_retired_health_is_database_free() -> None:
    assert asyncio.run(health()) == {
        "status": "retired",
        "version": RETIREMENT_VERSION,
        "database": "disabled",
        "archive": "canonical",
    }


def test_operational_routes_are_gone() -> None:
    for path in ("", "api/jobs", "api/candidates/example/robustness"):
        response = asyncio.run(retired(path))
        assert response.status_code == 410
        assert json.loads(response.body)["database"] == "disabled"

    catch_all = next(route for route in app.routes if getattr(route, "path", None) == "/{path:path}")
    assert {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"} <= catch_all.methods
