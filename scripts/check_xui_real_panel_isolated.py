"""Run only in a network-none container with a synthetic standalone 3x-ui DB."""

import os, subprocess, tempfile, time, json, uuid, sys, copy
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shop_bot.modules import xui_api as x


def main():
    assert (
        os.environ.get("ISOLATED_PANEL_TEST") == "1"
    ), "Explicit isolated container required"
    interfaces = {
        line.split(":", 1)[0].strip()
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]
    }
    assert interfaces == {"lo"}, "Run this check only with Docker --network none"
    folder = tempfile.mkdtemp(prefix="xui-fixture-")
    env = dict(
        os.environ,
        XUI_DB_FOLDER=folder,
        XUI_BIN_FOLDER="/tmp/no-production-xray",
        XUI_LOG_FOLDER=folder,
    )
    log = open(folder + "/panel.log", "w+")
    proc = subprocess.Popen(["/app/project/test-x-ui"], env=env, stdout=log, stderr=log)
    base = "http://127.0.0.1:2053"
    s = requests.Session()

    def call(method, path, body=None):
        r = s.request(method, base + "/" + path, json=body, timeout=10)
        r.raise_for_status()
        obj = r.json()
        assert obj.get("success"), (path, str(obj.get("msg"))[:180])
        return obj.get("obj")

    try:
        for _ in range(60):
            try:
                r = s.get(base + "/csrf-token", timeout=1)
                if r.status_code == 200:
                    break
            except requests.RequestException:
                pass
            if proc.poll() is not None:
                raise RuntimeError("Isolated panel exited")
            time.sleep(0.25)
        csrf = r.json().get("obj")
        s.headers["X-CSRF-Token"] = csrf
        call("POST", "login", {"username": "admin", "password": "admin"})
        expiry = int(time.time() * 1000) + 3600000
        identity = str(uuid.uuid4())
        email = "isolated-fixture"
        payload = {
            "up": 0,
            "down": 0,
            "total": 0,
            "remark": "isolated-test",
            "enable": False,
            "expiryTime": 0,
            "listen": "127.0.0.1",
            "port": 19999,
            "protocol": "vless",
            "settings": json.dumps({"clients": [], "decryption": "none"}),
            "streamSettings": json.dumps({"network": "tcp", "security": "none"}),
            "sniffing": json.dumps({"enabled": False}),
        }
        call("POST", "panel/api/inbounds/add", payload)
        inbound_id = call("GET", "panel/api/inbounds/list")[0]["id"]
        client = {
            "id": identity,
            "email": email,
            "subId": "fixture-sub-123456",
            "enable": True,
            "expiryTime": expiry,
            "totalGB": 0,
            "limitIp": 2,
            "limitHwid": 3,
            "group": "fixture-group",
            "comment": "preserve fixture",
            "tgId": 12345,
            "reset": 0,
            "resetDay": 0,
            "resetWeekday": 0,
            "resetMax": 7,
            "trafficReset": "never",
            "trafficResetDay": 1,
            "reverse": None,
        }
        call(
            "POST",
            "panel/api/clients/add",
            {"client": client, "inboundIds": [inbound_id]},
        )
        before = call("GET", "panel/api/clients/get/" + email)["client"]
        api, inbound = x.login_to_host(base, "admin", "admin", inbound_id)
        assert api and inbound
        # Real controller/database round-trip, not a mocked implementation.
        ident, got = x.update_or_create_client_on_panel(
            api, inbound_id, email, absolute_expiry_ms=expiry + 3600000
        )
        assert ident == identity and got == expiry + 3600000, "Renewal was not verified"
        after = call("GET", "panel/api/clients/get/" + email)["client"]
        for k, v in before.items():
            if k not in {
                "expiryTime",
                "updatedAt",
                "limitIp",
                "totalGB",
                "reset",
                "tgId",
                "enable",
            }:
                assert after.get(k) == v, ("Preservation", k)
        assert after["expiryTime"] == got and after["enable"] is True
        # Disable and recover the same existing identity; never recreate it.
        ident, past = x.update_or_create_client_on_panel(
            api,
            inbound_id,
            email,
            absolute_expiry_ms=int(time.time() * 1000) - 60000,
            preserve_longer_expiry=False,
        )
        assert ident == identity and past < int(time.time() * 1000)
        assert (
            call("GET", "panel/api/clients/get/" + email)["client"]["enable"] is False
        )
        ident, got = x.update_or_create_client_on_panel(
            api, inbound_id, email, absolute_expiry_ms=expiry + 7200000
        )
        assert ident == identity and got == expiry + 7200000
        final = call("GET", "panel/api/clients/get/" + email)["client"]
        assert (
            final["uuid"] == identity
            and final["subId"] == before["subId"]
            and final["enable"] is True
        )
        assert (
            final["group"] == before["group"]
            and final["limitHwid"] == before["limitHwid"]
        )
        # New/trial issuance and repeated absolute target retain one identity/term.
        trial_start = int(time.time() * 1000)
        new_id, new_expiry = x.update_or_create_client_on_panel(
            api, inbound_id, "isolated-trial", seconds_to_add=86400
        )
        assert (
            new_id
            and trial_start + 86400000
            <= new_expiry
            <= int(time.time() * 1000) + 86400000
        )
        retry_id, retry_expiry = x.update_or_create_client_on_panel(
            api, inbound_id, "isolated-trial", absolute_expiry_ms=new_expiry
        )
        assert retry_id == new_id and retry_expiry == new_expiry
        extended_id, extended = x.update_or_create_client_on_panel(
            api, inbound_id, "isolated-trial", days_to_add=1
        )
        assert extended_id == new_id and extended == new_expiry + 86400000
        print(
            "REAL 3x-ui isolated API: PASS (new/trial, retry, additive renewal, expired disable, recovery, UUID/subId/group/HWID preservation)"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        s.close()
        log.close()


if __name__ == "__main__":
    main()
