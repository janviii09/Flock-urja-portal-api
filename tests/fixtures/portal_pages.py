"""Recorded portal mock fixtures for deterministic testing.

Contains standard login HTML fixtures (needed for testing form-based operator auth)
and verified JSON payload samples for portal endpoints.
"""

# ── Login page fixtures (for PortalClient auth tests) ─────────────────

LOGIN_PAGE_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Urja Meter Ops</title></head>
<body>
  <form method="POST" action="/login">
    <label>Email <input type="email" name="email" value="" /></label>
    <label>Password <input type="password" name="password" value="" /></label>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
"""

LOGIN_FAILED_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Urja Meter Ops</title></head>
<body>
  <div class="error">Invalid email or password</div>
  <form method="POST" action="/login">
    <label>Email <input type="email" name="email" value="" /></label>
    <label>Password <input type="password" name="password" value="" /></label>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
"""

# ── Verified JSON Payload Fixtures ────────────────────────────────────

EXPORT_PAYLOAD_SAMPLE = {
    "data": [
        {
            "meterId": "J100000",
            "serialNo": "SE33962",
            "make": "HPL",
            "phaseType": "single",
            "installStatus": "Installed",
            "installType": "Whole Current",
            "build": "legacy",
            "dtCode": "DT-001",
            "hierarchy": {
                "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
                "circle": {"name": "Circle 1", "code": "C-01"},
                "division": {"name": "Division 1", "code": "D-01"},
                "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
                "substation": {"name": "Substation 1", "code": "S-01"},
                "feeder": {"name": "Feeder 1", "code": "F-01"},
                "dt": {"name": "DT 1", "code": "DT-001"},
            },
            "geo": {"lat": 26.94, "lng": 75.81},
        },
        {
            "meterId": "J100001",
            "serialNo": "GE84132",
            "make": "L&T",
            "phaseType": "single",
            "installStatus": "Installed",
            "installType": "Whole Current",
            "build": "legacy",
            "dtCode": "DT-002",
            "hierarchy": {
                "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
                "circle": {"name": "Circle 1", "code": "C-01"},
                "division": {"name": "Division 1", "code": "D-01"},
                "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
                "substation": {"name": "Substation 1", "code": "S-01"},
                "feeder": {"name": "Feeder 1", "code": "F-01"},
                "dt": {"name": "DT 2", "code": "DT-002"},
            },
            "geo": {"lat": 26.95, "lng": 75.82},
        },
    ],
    "total": 2,
}

SEARCH_PAGE_1_SAMPLE = {
    "data": [
        {
            "meterId": "J100000",
            "serialNo": "SE33962",
            "make": "HPL",
            "phaseType": "single",
            "installStatus": "Installed",
            "dtCode": "DT-001",
        },
        {
            "meterId": "J100001",
            "serialNo": "GE84132",
            "make": "L&T",
            "phaseType": "single",
            "installStatus": "Installed",
            "dtCode": "DT-002",
        },
    ],
    "total": 4,
    "page": 1,
    "pageSize": 2,
}

SEARCH_PAGE_2_SAMPLE = {
    "data": [
        {
            "meterId": "J100002",
            "serialNo": "AL28136",
            "make": "L&T",
            "phaseType": "single",
            "installStatus": "Installed",
            "dtCode": "DT-003",
        },
        {
            "meterId": "J100020",
            "serialNo": "SE99999",
            "make": "Secure",
            "phaseType": "three",
            "installStatus": "Installed",
            "dtCode": "DT-004",
        },
    ],
    "total": 4,
    "page": 2,
    "pageSize": 2,
}

DTS_PAYLOAD_SAMPLE = {
    "data": [
        {
            "code": "DT-001",
            "name": "Malviya Nagar DT 1",
            "feederCode": "F-001",
            "capacityKva": 100,
        },
        {
            "code": "DT-002",
            "name": "Mansarovar DT 2",
            "feederCode": "F-002",
            "capacityKva": 63,
        },
        {
            "code": "DT-003",
            "name": "Vaishali DT 3",
            "feederCode": "F-003",
            "capacityKva": 250,
        },
    ],
    "total": 3,
    "page": 1,
    "pageSize": 20,
}

ENERGY_PAYLOAD_SAMPLE = {
    "data": [
        {"timestamp": "23/06/2026 23:30", "kwh": 48439.58, "kvah": 51230.12, "voltR": 238.4},
        {"timestamp": "23/06/2026 23:00", "kwh": 48438.12, "kvah": 51228.45, "voltR": 239.1},
    ]
}
