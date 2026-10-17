"""App boots against fake R2 and core public pages respond."""


def test_app_imports_against_fake_bucket(main_module):
    assert main_module.R2_BUCKET == "test-bucket"


def test_homepage(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_days_endpoint(client):
    r = client.get("/api/days")
    assert r.status_code == 200
    assert "days" in r.json()


def test_admin_requires_login(client):
    r = client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_uptime_monitor_head_request(client):
    r = client.head("/")
    assert r.status_code == 200 and r.content == b""
