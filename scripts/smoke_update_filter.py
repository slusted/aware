"""In-process smoke test for the saved-filter update endpoints.

Exercises both the JSON `PUT /api/filters/{id}` and the HTMX
`POST /partials/stream_update_filter/{id}` paths against the local
SQLite snapshot. Skipped in CI — developer-time check.

Run:  py -X utf8 scripts/smoke_update_filter.py
"""
import os
import sys
import secrets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.db import SessionLocal
from app.models import SavedFilter, User


def main() -> int:
    db = SessionLocal()
    seeded_admin = False
    seed = None
    other = None
    admin = None
    try:
        admin = db.query(User).filter(User.role == "admin").first()
        if not admin:
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
            print(f"  using admin id={admin.id} email={admin.email!r}")

        seed = SavedFilter(
            owner_id=admin.id,
            name=f"smoke-update-{secrets.token_hex(3)}",
            spec={"signal_types": ["product_launch"], "since_days": 30},
            visibility="private",
        )
        db.add(seed)
        db.commit()
        db.refresh(seed)
        print(f"  seeded SavedFilter id={seed.id}")

        client = TestClient(app)

        # 1. Unauthenticated PUT -> 401
        r = client.put(f"/api/filters/{seed.id}", json={"name": "x", "spec": {}})
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        print("  PASS: PUT /api/filters/.. -> 401 unauthenticated")

        # 2. Login.
        if seeded_admin:
            login_r = client.post(
                "/login",
                data={"email": admin.email, "password": "smoketest", "next": "/"},
                follow_redirects=False,
            )
            assert login_r.status_code in (200, 303), f"login failed: {login_r.status_code}"
            print("  PASS: logged in")

            # 3. PUT happy path: spec overwritten, name preserved when unchanged.
            new_spec = {"signal_types": ["funding"], "min_materiality": 0.5}
            r = client.put(f"/api/filters/{seed.id}", json={"name": seed.name, "spec": new_spec})
            assert r.status_code == 200, f"PUT failed: {r.status_code} {r.text[:200]}"
            payload = r.json()
            assert payload["spec"]["signal_types"] == ["funding"], f"spec not updated: {payload}"
            assert payload["spec"]["min_materiality"] == 0.5
            assert payload["name"] == seed.name
            print(f"  PASS: PUT /api/filters/{seed.id} updated spec")

            # 4. PUT can rename too.
            r = client.put(f"/api/filters/{seed.id}", json={"name": "renamed-by-smoke", "spec": new_spec})
            assert r.status_code == 200 and r.json()["name"] == "renamed-by-smoke"
            print("  PASS: PUT also renames")

            # 5. PUT without name -> 400.
            r = client.put(f"/api/filters/{seed.id}", json={"name": "", "spec": new_spec})
            assert r.status_code == 400, f"empty name should 400, got {r.status_code}"
            print("  PASS: PUT empty name -> 400")

            # 6. PUT to nonexistent filter -> 404.
            r = client.put("/api/filters/999999", json={"name": "x", "spec": {}})
            assert r.status_code == 404, f"nonexistent should 404, got {r.status_code}"
            print("  PASS: PUT nonexistent -> 404")

            # 7. PUT against another user's filter -> 403.
            other = SavedFilter(owner_id=admin.id + 1, name="not-mine", spec={}, visibility="private")
            db.add(other)
            db.commit()
            db.refresh(other)
            r = client.put(f"/api/filters/{other.id}", json={"name": "stolen", "spec": {}})
            assert r.status_code == 403, f"other-owner should 403, got {r.status_code}"
            print("  PASS: PUT other user's filter -> 403")

            # 8. HTMX partial update returns the saved-filter list HTML.
            form = {
                "signal_types": "messaging_shift",
                "min_materiality": "0.7",
                "since_days": "7",
            }
            r = client.post(f"/partials/stream_update_filter/{seed.id}", data=form)
            assert r.status_code == 200, f"partial update failed: {r.status_code} {r.text[:200]}"
            assert "saved-filter" in r.text, "expected saved-filter list HTML"
            db.refresh(seed)
            assert seed.spec.get("signal_types") == ["messaging_shift"], \
                f"partial didn't persist: {seed.spec}"
            assert seed.spec.get("min_materiality") == 0.7
            print("  PASS: /partials/stream_update_filter/.. persists and returns list")

        return 0
    finally:
        if seed:
            db.delete(db.get(SavedFilter, seed.id))
        if other:
            db.delete(db.get(SavedFilter, other.id))
        if seeded_admin and admin:
            db.delete(db.get(User, admin.id))
        db.commit()
        db.close()
        print("  cleaned up")


if __name__ == "__main__":
    sys.exit(main())
