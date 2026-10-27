"""Every route, tRPC procedure and server action, each reviewed for who may reach it.

public_paths exposes all of /groups/ and /api/ to anonymous visitors, and openhost/auth_proxy.py re-gates the few
owner-only ones (OWNER_ONLY_* there). So anything upstream adds under those prefixes is public by default. When this
test fails after an upstream merge, decide whether the new entry is something anyone with a group link may use (add
it to PUBLIC) or owner-only (add it to OWNER_ONLY, gate it in auth_proxy.py, and test it in test_access.py).
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# Reached only by the owner: "/" by the router (not in public_paths), the rest by auth_proxy.py.
OWNER_ONLY = {
    "app/page.tsx",
    "app/groups/page.tsx",
    "app/groups/create/page.tsx",
    "trpc/routers/groups/create.procedure.ts",
}

# Anyone with a group link: the group pages and everything they call, plus static metadata and health checks.
PUBLIC = {
    "app/manifest.ts",
    "app/robots.ts",
    "app/sitemap.ts",
    "app/api/health/route.ts",
    "app/api/health/liveness/route.ts",
    "app/api/health/readiness/route.ts",
    # Needs CRON_SECRET (404 without it, which is our default).
    "app/api/cron/purge-deleted-groups/route.ts",
    # Presigns uploads when S3 is configured (off by default); see bottled-spliit#5.
    "app/api/s3-upload/route.ts",
    "app/api/trpc/[trpc]/route.ts",
    "app/groups/[groupId]/page.tsx",
    "app/groups/[groupId]/activity/page.tsx",
    "app/groups/[groupId]/balances/page.tsx",
    "app/groups/[groupId]/edit/page.tsx",
    "app/groups/[groupId]/information/page.tsx",
    "app/groups/[groupId]/stats/page.tsx",
    "app/groups/[groupId]/expenses/page.tsx",
    "app/groups/[groupId]/expenses/create/page.tsx",
    "app/groups/[groupId]/expenses/[expenseId]/edit/page.tsx",
    "app/groups/[groupId]/expenses/export/csv/route.ts",
    "app/groups/[groupId]/expenses/export/json/route.ts",
    "trpc/routers/categories/list.procedure.ts",
    "trpc/routers/groups/get.procedure.ts",
    "trpc/routers/groups/getDetails.procedure.ts",
    # Takes explicit group ids (from the visitor's own recent list), so it only reveals groups they already know.
    "trpc/routers/groups/list.procedure.ts",
    "trpc/routers/groups/update.procedure.ts",
    # Off unless ENABLE_GROUP_DELETION is set; anyone with the link can schedule deletion, as upstream intends.
    "trpc/routers/groups/delete.procedure.ts",
    "trpc/routers/groups/restore.procedure.ts",
    "trpc/routers/groups/activities/list.procedure.ts",
    "trpc/routers/groups/balances/list.procedure.ts",
    "trpc/routers/groups/balances/forUser.procedure.ts",
    "trpc/routers/groups/expenses/create.procedure.ts",
    "trpc/routers/groups/expenses/delete.procedure.ts",
    "trpc/routers/groups/expenses/get.procedure.ts",
    "trpc/routers/groups/expenses/list.procedure.ts",
    "trpc/routers/groups/expenses/update.procedure.ts",
    "trpc/routers/groups/stats/category-expenses.procedure.ts",
    "trpc/routers/groups/stats/month-expenses.procedure.ts",
    "trpc/routers/groups/stats/overview.procedure.ts",
    # Server actions: callable by POSTing to any page, including the public group pages.
    "app/groups/[groupId]/expenses/create-from-receipt-button-actions.ts",
    "components/expense-form-actions.tsx",
    "lib/analytics/config.ts",
    "lib/featureFlags.ts",
    "lib/locale.ts",
}

ROUTE_FILES = {"page.tsx", "route.ts", "manifest.ts", "robots.ts", "sitemap.ts"}


def _endpoints() -> set[str]:
    found = set()
    for path in SRC.rglob("*"):
        if not path.is_file() or path.suffix not in (".ts", ".tsx"):
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("app/") and path.name in ROUTE_FILES:
            found.add(rel)
        elif rel.startswith("trpc/") and path.name.endswith(".procedure.ts"):
            found.add(rel)
        elif path.read_text(encoding="utf-8").lstrip().startswith(("'use server'", '"use server"')):
            found.add(rel)
    return found


def test_every_endpoint_is_reviewed() -> None:
    found = _endpoints()
    reviewed = OWNER_ONLY | PUBLIC
    assert not OWNER_ONLY & PUBLIC
    new, gone = sorted(found - reviewed), sorted(reviewed - found)
    assert not new and not gone, (
        f"new endpoints (public by default, decide who may reach them): {new}\n"
        f"reviewed endpoints that no longer exist (was an owner-only one moved?): {gone}"
    )
