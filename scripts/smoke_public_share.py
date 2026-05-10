"""In-process smoke test for the public share-link feature.

Exercises the new endpoints against the local SQLite snapshot using
FastAPI's TestClient (no network socket, no separate process). Skipped
in CI — this is a developer-time check.

Run:  py scripts/smoke_public_share.py
"""
import os
import sys
import secrets

# Ensure the project root is importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.db import SessionLocal
from app.models import SavedFilter, User


def main() -> int:
    db = SessionLocal()
    try:
        # Find any admin user to seed an authenticated session against. The
        # smoke test mints a SavedFilter on their behalf, calls the share
        # endpoints directly, and tears the row down.
        admin = db.query(User).filter(User.role == "admin").first()
        seeded_admin = False
        if not admin:
            # Empty local DB. Seed a throwaway admin so the smoke test can
            # exercise ownership; cleaned up at end.
            from app.auth import hash_password
            admin = User(
                email=f"smoke-{secrets.token_hex(3)}@local",
                name="smoke admin",
                password_hash=hash_password("smoketest"),
                role="admin",
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
            seeded_admin = True
            print(f"  seeded throwaway admin id={admin.id}")
        else:
            print(f"  using admin user id={admin.id} email={admin.email!r}")

        # Seed a throwaway filter we own.
        seed = SavedFilter(
            owner_id=admin.id,
            name=f"smoke-test-{secrets.token_hex(3)}",
            spec={"signal_types": ["product"], "since_days": 30},
            visibility="private",
        )
        db.add(seed)
        db.commit()
        db.refresh(seed)
        filter_id = seed.id
        print(f"  seeded SavedFilter id={filter_id}")

        client = TestClient(app)

        # 1. Anonymous /p/{garbage} -> 404
        r = client.get("/p/this-token-does-not-exist", follow_redirects=False)
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
        print("  PASS: /p/{unknown} -> 404")

        # Reach into the DB to mint a token directly (we don't have a valid
        # session cookie wired up here — the share endpoints require auth).
        seed.public_token = secrets.token_urlsafe(32)
        from datetime import datetime as _dt
        seed.public_token_created_at = _dt.utcnow()
        db.commit()
        token = seed.public_token

        # 2. Anonymous /p/{token} -> 200 + noindex header + no signup CTA
        r = client.get(f"/p/{token}", follow_redirects=False)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
        assert r.headers.get("x-robots-tag", "").startswith("noindex"), \
            f"missing X-Robots-Tag: {r.headers!r}"
        body = r.text
        assert "noindex, nofollow" in body, "missing <meta robots>"
        assert "Shared from Aware" in body, "missing public-stream header"
        # The card actions and per-user chrome must NOT render in public mode.
        for forbidden in (
            'class="new-dot"',
            'class="flip-toggle"',
            'class="expand-toggle"',
            'state-new',
            "Snoozed until",
        ):
            assert forbidden not in body, f"public render leaked: {forbidden!r}"
        print("  PASS: /p/{token} renders with noindex + no per-user chrome")

        # 3. Anonymous /api/filters/{id}/share -> 401 (auth required)
        r = client.post(f"/api/filters/{filter_id}/share")
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        print("  PASS: /api/filters/.../share -> 401 unauthenticated")

        # 4. Pinned-only filter can't be minted (test the gate via direct call).
        from app.routes.filters import _check_mintable
        seed.spec = {"pinned_only": True}
        try:
            _check_mintable(seed)
            print("FAIL: pinned-only filter should have been rejected")
            return 1
        except Exception as e:
            assert "pinned" in str(e).lower(), f"wrong rejection reason: {e}"
            print("  PASS: pinned-only filter rejected at mint time")

        # 5. Revoke (set token to NULL directly to simulate the endpoint) ->
        #    /p/{token} now 404s.
        seed.spec = {"signal_types": ["product"]}
        seed.public_token = None
        seed.public_token_created_at = None
        db.commit()
        r = client.get(f"/p/{token}", follow_redirects=False)
        assert r.status_code == 404, f"expected 404 after revoke, got {r.status_code}"
        print("  PASS: /p/{token} -> 404 after revoke")

        # 6. Authenticated round-trip on the JSON share endpoints. Login,
        # mint, fetch GET state, then revoke. Confirms the API path the UI
        # would actually drive.
        if seeded_admin:
            login_r = client.post(
                "/login",
                data={"email": admin.email, "password": "smoketest", "next": "/"},
                follow_redirects=False,
            )
            assert login_r.status_code in (200, 303), \
                f"login failed: {login_r.status_code} {login_r.text[:200]}"
            mint_r = client.post(f"/api/filters/{filter_id}/share")
            assert mint_r.status_code == 200, \
                f"mint failed: {mint_r.status_code} {mint_r.text[:200]}"
            payload = mint_r.json()
            assert payload.get("public_token"), f"no token in payload: {payload}"
            assert payload.get("share_url", "").endswith(payload["public_token"]), \
                f"share_url shape wrong: {payload}"
            print(f"  PASS: POST /api/filters/{filter_id}/share -> {payload['public_token'][:8]}...")

            get_r = client.get(f"/api/filters/{filter_id}/share")
            assert get_r.status_code == 200 and get_r.json().get("public_token") == payload["public_token"], \
                f"GET share state mismatch: {get_r.status_code} {get_r.text[:200]}"
            print("  PASS: GET /api/filters/.../share returns current token")

            del_r = client.delete(f"/api/filters/{filter_id}/share")
            assert del_r.status_code == 200, f"revoke failed: {del_r.status_code}"
            get_r2 = client.get(f"/api/filters/{filter_id}/share")
            assert get_r2.json().get("public_token") is None, \
                f"token should be null after revoke: {get_r2.json()}"
            print("  PASS: DELETE /api/filters/.../share clears the token")

        return 0
    finally:
        # Cleanup: drop the seeded filter (and admin, if we made one).
        seed = db.get(SavedFilter, filter_id) if "filter_id" in dir() else None
        if seed:
            db.delete(seed)
            db.commit()
            print(f"  cleaned up SavedFilter id={filter_id}")
        if "seeded_admin" in dir() and seeded_admin and admin:
            db.delete(db.get(User, admin.id))
            db.commit()
            print(f"  cleaned up throwaway admin id={admin.id}")
        db.close()


if __name__ == "__main__":
    sys.exit(main())
