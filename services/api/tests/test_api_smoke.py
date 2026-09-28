from api_support import TENANT_A, VIN_A


def test_health_and_me(h):
    assert h.client.get("/healthz").json() == {"status": "alive"}
    r = h.client.get("/v1/me", headers=h.auth())
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == TENANT_A


def test_vehicle_list(h):
    h.data.add_vehicle(TENANT_A, VIN_A)
    r = h.client.get("/v1/vehicles", headers=h.auth())
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["vin"] == VIN_A
