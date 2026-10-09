"""Every route must authenticate, unless it is on the short public list.

The API reaches Supabase with the service_role key, so a route that forgets the
auth dependency is not caught by RLS: it simply serves everyone. This test walks
the real route table so a new endpoint cannot ship without a decision.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api.deps import get_current_user, get_current_user_with_token
from app.main import app

AUTH_DEPENDENCIES: set[Callable[..., Any]] = {get_current_user, get_current_user_with_token}

# Reachable without a token, on purpose. Anything added here is public to the internet.
PUBLIC_ROUTES: set[tuple[str, str]] = {
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/plans/"),
}

# Protected by a shared secret instead of a user JWT.
ADMIN_PREFIX = "/admin/"
ADMIN_HEADER = "X-Admin-Token"


def _walk(routes: list[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        # FastAPI >= 0.140 keeps `include_router` targets as a wrapper around the
        # original router instead of copying its routes into `app.routes`.
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _walk(included.routes)


def _api_routes() -> list[APIRoute]:
    return list(_walk(app.routes))


def _endpoints(route: APIRoute) -> Iterator[tuple[str, str]]:
    for method in sorted(route.methods or ()):
        if method in {"HEAD", "OPTIONS"}:
            continue
        yield method, route.path


def _calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield dep.call
        yield from _calls(dep)


def _header_aliases(dependant: Dependant) -> set[str]:
    aliases = {p.alias for p in dependant.header_params}
    for dep in dependant.dependencies:
        aliases |= _header_aliases(dep)
    return aliases


def _is_public(method: str, path: str) -> bool:
    return (method, path) in PUBLIC_ROUTES or (method == "GET" and path.endswith("/health"))


def _route_ids() -> list[str]:
    return [f"{m} {p}" for r in _api_routes() for m, p in _endpoints(r)]


def test_route_table_is_not_empty() -> None:
    # Guards the parametrised test below from passing vacuously.
    assert len(_route_ids()) > 20


@pytest.mark.parametrize(
    ("route", "method", "path"),
    [(r, m, p) for r in _api_routes() for m, p in _endpoints(r)],
    ids=_route_ids(),
)
def test_route_requires_authentication(route: APIRoute, method: str, path: str) -> None:
    if _is_public(method, path):
        return
    if path.startswith(ADMIN_PREFIX):
        assert ADMIN_HEADER in _header_aliases(route.dependant), (
            f"{method} {path} is under {ADMIN_PREFIX} but does not read {ADMIN_HEADER}."
        )
        return
    assert AUTH_DEPENDENCIES & set(_calls(route.dependant)), (
        f"{method} {path} does not depend on get_current_user*. Add the dependency, "
        "or add the route to PUBLIC_ROUTES if it really is public."
    )


def test_public_list_has_no_stale_entries() -> None:
    # A renamed route would otherwise leave a dead exception that a future route
    # with the old path inherits silently.
    existing = {(m, p) for r in _api_routes() for m, p in _endpoints(r)}
    assert PUBLIC_ROUTES <= existing
