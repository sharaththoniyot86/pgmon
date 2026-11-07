"""Pins the invariant the whole frontend rests on: an /api/* request always
gets a JSON body back, whatever went wrong.

Run with:  python tests/test_api_json_contract.py

Why this file exists: the browser parses every API reply with resp.json(),
so the moment one of them answers with HTML instead -- a 303 to the login
page, an unhandled exception's error page -- the UI can only report
"Unexpected token '<', "<!DOCTYPE "... is not valid JSON", which names
neither the status nor the cause. Each test below is one way that used to
be reachable (or nearly was).
"""
import asyncio, sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app import metrics

FAILS = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        FAILS.append(name)


class FakeRequest:
    """Only the attribute the handler reads."""
    def __init__(self, path):
        self.url = type("U", (), {"path": path})()


async def test_unauth_handler():
    import app.main as main

    print("\n401 HANDLING (API gets JSON, pages get a login redirect)")
    exc = HTTPException(status_code=401, detail="Not authenticated")

    resp = await main.unauth_redirect(FakeRequest("/api/queries/tuning-advice"), exc)
    check("/api/* 401 -> JSON", isinstance(resp, JSONResponse) and resp.status_code == 401)

    resp = await main.unauth_redirect(FakeRequest("/"), exc)
    check("page 401 -> 303 redirect", resp.status_code == 303)

    # A path that still carries BASE_PATH must not be mistaken for a page.
    orig = main.BASE_PATH
    main.BASE_PATH = "/pgmonitor"
    try:
        resp = await main.unauth_redirect(FakeRequest("/pgmonitor/api/overview"), exc)
        check("prefixed /api/* 401 -> JSON", isinstance(resp, JSONResponse) and resp.status_code == 401)
        resp = await main.unauth_redirect(FakeRequest("/pgmonitor/"), exc)
        check("prefixed page 401 -> 303 redirect", resp.status_code == 303)
    finally:
        main.BASE_PATH = orig

    # Non-401 errors are JSON regardless of path.
    resp = await main.unauth_redirect(FakeRequest("/"), HTTPException(status_code=403, detail="nope"))
    check("page 403 -> JSON (no redirect)", isinstance(resp, JSONResponse) and resp.status_code == 403)


async def test_tuning_advice_without_a_plan():
    print("\nTUNING ADVICE WITH NO PLAN TREE (clean 400, not an unhandled 500)")
    for fake in ([], [{}], [{"Plan": "not-a-dict"}], None):
        async def fake_plan(pool, query, analyze=False, _f=fake):
            return {"plan": _f, "original_query": query, "query": query, "substituted": False}

        real, metrics.get_query_plan = metrics.get_query_plan, fake_plan
        try:
            await metrics.get_tuning_advice(None, "SELECT 1")
            check(f"plan={fake!r} raises", False)
        except metrics.UnsafeQueryError:
            check(f"plan={fake!r} -> UnsafeQueryError (HTTP 400 + JSON detail)", True)
        except Exception as e:  # IndexError/KeyError here would be a 500 with a non-JSON body
            check(f"plan={fake!r} -> {type(e).__name__}", False)
        finally:
            metrics.get_query_plan = real


async def main():
    await test_unauth_handler()
    await test_tuning_advice_without_a_plan()
    print("\n" + ("FAILURES: " + ", ".join(FAILS) if FAILS else "ALL API JSON-CONTRACT TESTS PASSED"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
