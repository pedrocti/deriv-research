"""The self-hosted demo journal: storage, HTTP routes, screenshots, review numbers."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from dresearch import journal as J
from dresearch.dashboard import get_token, make_server

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture()
def srv(tmp_path):
    s = make_server(tmp_path, host="127.0.0.1", port=0)
    th = threading.Thread(target=s.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{s.server_address[1]}/{get_token(tmp_path)}/journal/"
    yield base, tmp_path
    s.shutdown()


def call(url, body=None, ctype="application/json"):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype} if data else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers, r.read()


def test_page_state_trade_close_shot_export(srv):
    base, data = srv
    st, h, page = call(base)
    assert st == 200 and b"Challenge Trade Log" in page and "connect-src 'self'" in h["Content-Security-Policy"]
    _, _, b = call(base + "api/shot", PNG, "image/png")
    name = json.loads(b)["name"]
    assert name.endswith(".png") and call(base + "shots/" + name)[2] == PNG
    t = json.loads(call(base + "api/trade", {"market": "XAUUSD", "dir": 1, "entry": 3870, "stop": 3867.5, "target": 3876,
                                             "lots": 0.1, "risk": 25, "reason": "", "shots": [name], "planBreaks": [],
                                             "created": "2026-11-02T08:00:00Z", "status": "open", "evil": 1})[2])
    assert "evil" not in t
    call(base + "api/update", {"id": t["id"], "patch": {"status": "closed", "exit": 3876, "pnl": 59.2,
                                                        "closedAt": "2026-11-02T10:00:00Z", "closeBreaks": [], "followed": True}})
    s = json.loads(call(base + "api/state")[2])
    assert s["trades"][0]["pnl"] == 59.2 and s["trades"][0]["shots"] == [name]
    _, h, csv = call(base + "export.csv")
    assert "attachment" in h["Content-Disposition"] and b"XAUUSD" in csv and name.encode() in csv
    assert (data / "journal" / "changes.jsonl").read_text().count("\n") == 2


def test_rejects_bad_input_and_wrong_token(srv):
    base, data = srv
    for url, body, ctype in [(base + "api/shot", b"GIF89a", "image/gif"),
                             (base + "api/update", {"id": "nope", "patch": {}}, "application/json"),
                             (base + "api/trade", {"shots": ["../../etc/passwd"]}, "application/json"),
                             (base + "api/settings", {"accountSize": 5}, "application/json")]:
        with pytest.raises(urllib.error.HTTPError) as e:
            call(url, body, ctype)
        assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "shots/..%2F..%2Fdashboard.json")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base.replace(get_token(data), "wrongtoken"))
    assert e.value.code == 404


def trade(day, pnl, risk=25, followed=True, created_h=8):
    return {"id": day + str(pnl), "market": "XAUUSD", "dir": 1, "risk": risk, "status": "closed", "pnl": pnl,
            "created": f"{day}T0{created_h}:00:00Z", "closedAt": f"{day}T10:00:00Z", "followed": followed,
            "planBreaks": [] if followed else ["Risk $40.00 is within $25.00 (0.5% of the account)"]}


def test_challenge_phases_fail_and_summary():
    days = [f"2026-11-0{i}" for i in range(2, 10)]
    st = {"settings": {"accountSize": 5000}, "trades": [trade(d, 110) for d in days]}
    c = J.challenge(st)
    assert c["result"] == "passed" and c["balance"] == 5000 + 110 * 8   # +440 after 4 days, then +5% over 4 more
    st2 = {"settings": {"accountSize": 5000}, "trades": [trade("2026-11-02", -130), trade("2026-11-02", -130)]}
    assert J.challenge(st2)["result"] == "failed"
    s = J.summary({"settings": {}, "trades": [trade("2026-11-02", 25), trade("2026-11-03", -40, followed=False)]})
    assert s["closed"] == 2 and s["rules_kept_pct"] == 50.0 and s["avg_R_rules_broken"] == pytest.approx(-1.6)
    assert s["common_breaks"][0][1] == 1


def test_early_exits_are_summarised_and_exported(tmp_path):
    t1 = {**trade("2026-11-02", 10), "exitType": "early", "earlyReason": "fear", "earlyText": "got scared", "followed": False,
          "closeBreaks": ["closed early out of fear"]}
    t2 = {**trade("2026-11-03", -25), "exitType": "stop"}
    s = J.summary({"settings": {}, "trades": [t1, t2]})
    assert s["exits"]["early"]["n"] == 1 and s["early_reasons"]["fear"]["avg_R"] == pytest.approx(0.4)
    J.apply(tmp_path, "trade", {k: v for k, v in t1.items() if k != "id"})
    assert "got scared" in J.export_csv(tmp_path) and "closed early (fear)" in J.report(tmp_path)


def test_volatility_practice_stays_out_of_challenge_and_specs_come_from_snapshot(tmp_path):
    v = {**trade("2026-11-02", 500), "market": "VOL75_1S"}
    g = {**trade("2026-11-02", -25), "followed": False}
    st = {"settings": {"accountSize": 5000}, "trades": [v, g]}
    assert J.challenge(st)["balance"] == 4975
    s = J.summary(st)
    assert s["practice_volatility"]["n"] == 1 and s["total_pnl"] == -25 and s["rules_kept_pct"] == 50.0
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "mt5_snapshot_20261029.json").write_text(json.dumps({"symbols": {
        "1HZ75V": {"specs": {"trade_contract_size": 1.0, "volume_min": 0.05, "volume_step": 0.01}}}}))
    assert J.state(tmp_path)["specs"] == {"VOL75_1S": {"contract": 1.0, "min": 0.05, "step": 0.01}}
