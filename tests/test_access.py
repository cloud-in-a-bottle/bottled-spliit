"""Who can do what, through the real router: our packaging layer's access rules.

Spliit has no accounts: a group is reached by its unguessable URL. Our layer (public_paths in cloudinabottle.toml
plus openhost/auth_proxy.py) keeps that link-sharing model for anonymous visitors, while creating groups and the
recent-groups list stay owner-only. An upstream merge that moves a route or renames a tRPC procedure could silently
open or close one of these, so each is checked here.
"""

import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from openhost_test_harness import OpenhostStack

HTML = {"Accept": "text/html"}


def _retry(call: Callable[[], requests.Response], ok: Callable[[requests.Response], bool]) -> requests.Response:
    """Repeat `call` until `ok` (the app finishes booting after the harness reports it running), up to a deadline."""
    deadline = time.monotonic() + 90
    while True:
        try:
            response = call()
            if ok(response) or time.monotonic() > deadline:
                return response
        except requests.ConnectionError:
            if time.monotonic() > deadline:
                raise
        time.sleep(2)


def _context(stack: OpenhostStack, response: requests.Response) -> str:
    return (
        f"{response.request.method} {response.url} -> {response.status_code} "
        f"(Location: {response.headers.get('Location')}): {response.text[:500]}\n\n"
        f"app logs:\n{stack.app_logs()[-3000:]}"
    )


def _trpc_mutation(session: requests.Session, stack: OpenhostStack, path: str, payload: Any) -> requests.Response:
    # The client uses superjson, so inputs are wrapped as {"json": ...}.
    return session.post(f"{stack.url}/api/trpc/{path}", json={"json": payload}, timeout=30, allow_redirects=False)


def _trpc_query(session: requests.Session, stack: OpenhostStack, procedure: str, payload: Any) -> requests.Response:
    return session.get(
        f"{stack.url}/api/trpc/{procedure}",
        params={"input": json.dumps({"json": payload})},
        timeout=30,
        allow_redirects=False,
    )


def _data(response: requests.Response) -> Any:
    return response.json()["result"]["data"]["json"]


@pytest.fixture(scope="module")
def group(stack: OpenhostStack) -> dict[str, str]:
    """A group created by the owner (the only one allowed to), to be shared by link."""
    name = f"Trip {uuid.uuid4().hex[:6]}"
    values = {
        "name": name,
        "currency": "$",
        "currencyCode": "USD",
        "participants": [{"name": "Alice"}, {"name": "Bob"}],
    }
    response = _retry(
        lambda: _trpc_mutation(stack.owner_session, stack, "groups.create", {"groupFormValues": values}),
        lambda r: r.status_code == 200,
    )
    assert response.status_code == 200, f"owner couldn't create a group: {_context(stack, response)}"
    return {"id": _data(response)["groupId"], "name": name}


def _assert_login_redirect(stack: OpenhostStack, response: requests.Response) -> None:
    """A browser navigation to an owner-only page is sent to the router's login on the same scheme/host."""
    assert response.status_code in (302, 303, 307), f"expected a redirect to login: {_context(stack, response)}"
    location = urlsplit(response.headers["Location"])
    router = urlsplit(stack.router_url)
    assert (location.scheme, location.netloc, location.path) == (router.scheme, router.netloc, "/login"), (
        f"redirect should go to {stack.router_url}/login: {_context(stack, response)}"
    )


@pytest.mark.parametrize(("path", "title"), [("/groups/create", "Create group"), ("/groups", "Recent groups")])
def test_owner_can_open_create_and_recent_groups(
    stack: OpenhostStack, group: dict[str, str], path: str, title: str
) -> None:
    # `group` already showed the owner can create a group through the API.
    response = _retry(
        lambda: stack.owner_session.get(f"{stack.url}{path}", headers=HTML, timeout=30),
        lambda r: r.status_code == 200,
    )
    assert response.status_code == 200 and title in response.text, (
        f"owner should see {title!r}: {_context(stack, response)}"
    )


def test_anonymous_can_use_shared_group_link(stack: OpenhostStack, group: dict[str, str]) -> None:
    anon = requests.Session()
    group_url = f"{stack.url}/groups/{group['id']}"

    # The shared link renders the group, along with the assets it needs.
    page = _retry(lambda: anon.get(f"{group_url}/expenses", headers=HTML, timeout=30), lambda r: r.status_code == 200)
    assert page.status_code == 200 and group["name"] in page.text, (
        f"shared link should show the group: {_context(stack, page)}"
    )
    scripts = re.findall(r'src="(/_next/static/[^"]+\.js)"', page.text)
    assert scripts, f"no /_next/ scripts in the group page: {page.text[:1000]}"
    asset = anon.get(f"{stack.url}{scripts[0]}", timeout=30, allow_redirects=False)
    assert asset.status_code == 200, f"group page's script should load anonymously: {_context(stack, asset)}"

    # And the API behind it: read the group, add an expense, see it listed.
    got = _trpc_query(anon, stack, "groups.get", {"groupId": group["id"]})
    assert got.status_code == 200, f"anonymous groups.get: {_context(stack, got)}"
    participants = {p["name"]: p["id"] for p in _data(got)["group"]["participants"]}
    expense = {
        "expenseDate": "2026-01-02",
        "title": "Dinner",
        "category": 0,
        "amount": 4200,
        "originalCurrency": "",
        "paidBy": participants["Alice"],
        "paidFor": [{"participant": pid, "shares": 1} for pid in participants.values()],
        "splitMode": "EVENLY",
        "saveDefaultSplittingOptions": False,
        "isReimbursement": False,
        "documents": [],
        "recurrenceRule": "NONE",
    }
    created = _trpc_mutation(
        anon, stack, "groups.expenses.create", {"groupId": group["id"], "expenseFormValues": expense}
    )
    assert created.status_code == 200, f"anonymous should be able to add an expense: {_context(stack, created)}"
    listed = _trpc_query(anon, stack, "groups.expenses.list", {"groupId": group["id"]})
    assert listed.status_code == 200, f"anonymous groups.expenses.list: {_context(stack, listed)}"
    assert "Dinner" in [e["title"] for e in _data(listed)["expenses"]], f"expense not listed: {listed.text[:500]}"


@pytest.mark.parametrize("path", ["/", "/groups", "/groups/create", "/groups/create/"])
def test_anonymous_is_sent_to_login_from_owner_pages(stack: OpenhostStack, group: dict[str, str], path: str) -> None:
    # `group` makes sure the app is up. "/" is gated by the router (it's not in public_paths); the others sit under
    # the public "/groups/" prefix and are re-gated by the auth proxy.
    response = requests.get(f"{stack.url}{path}", headers=HTML, timeout=30, allow_redirects=False)
    _assert_login_redirect(stack, response)


def test_anonymous_cannot_create_group(stack: OpenhostStack, group: dict[str, str]) -> None:
    anon = requests.Session()
    values = {"name": "Spam", "currency": "$", "currencyCode": "USD", "participants": [{"name": "Eve"}]}
    batch = {"0": {"json": {"groupFormValues": values}}, "1": {"json": {"groupFormValues": values}}}
    attempts = {
        "direct": lambda: _trpc_mutation(anon, stack, "groups.create", {"groupFormValues": values}),
        # httpBatchLink comma-joins procedures in the path, %-encoded or not.
        "batched": lambda: anon.post(
            f"{stack.url}/api/trpc/groups.create,groups.create?batch=1", json=batch, timeout=30, allow_redirects=False
        ),
        "batched, encoded comma": lambda: anon.post(
            f"{stack.url}/api/trpc/groups.create%2Cgroups.create?batch=1", json=batch, timeout=30, allow_redirects=False
        ),
        # The router strips client-sent X-OpenHost-* headers, so this can't pass for the owner.
        "spoofed owner header": lambda: anon.post(
            f"{stack.url}/api/trpc/groups.create",
            json={"json": {"groupFormValues": values}},
            headers={"X-OpenHost-Is-Owner": "true"},
            timeout=30,
            allow_redirects=False,
        ),
    }
    for label, attempt in attempts.items():
        response = attempt()
        assert response.status_code in (401, 403) and "groupId" not in response.text, (
            f"anonymous group creation ({label}) should be refused: {_context(stack, response)}"
        )
