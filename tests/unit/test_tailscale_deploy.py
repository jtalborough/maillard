"""Deployment configuration parity and entirely offline OAuth/NAS execution."""

import io
import json
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
STEPS = {step["name"]: step for step in WORKFLOW["jobs"]["deploy"]["steps"]}
NAS_SCRIPT = STEPS["Deploy to NAS"]["with"]["script"]
MINT_SCRIPT = STEPS["Mint Maillard tailnet key"]["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


@pytest.mark.parametrize(
    "filename,delimiter",
    [
        ("docker-compose.tailscale.yml", "MAILLARD_TS_COMPOSE"),
        ("tailscale/serve.json", "MAILLARD_TS_SERVE"),
    ],
)
def test_embedded_sidecar_files_are_byte_identical(filename, delimiter):
    embedded = (
        NAS_SCRIPT.split(f"<<'{delimiter}' || return 1\n", 1)[1].split(f"\n{delimiter}\n", 1)[0]
        + "\n"
    )
    assert embedded.encode() == (ROOT / filename).read_bytes()


def test_embedded_shell_syntax():
    result = subprocess.run(["bash", "-n"], input=NAS_SCRIPT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tag", ["tag:maillard", "tag:coffee"])
def test_mint_key_capabilities_and_masking(tmp_path, monkeypatch, capsys, tag):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("TS_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("TS_OAUTH_SECRET", "test-secret")
    monkeypatch.setenv("MAILLARD_TS_TAG", tag)
    replies = [io.BytesIO(b'{"access_token":"test-token"}'), io.BytesIO(b'{"key":"test-key"}')]
    with patch("urllib.request.urlopen", side_effect=replies) as request:
        exec(compile(MINT_SCRIPT, "mint-key", "exec"), {})
    token_request, key_request = [call.args[0] for call in request.call_args_list]
    assert token_request.full_url == "https://api.tailscale.com/api/v2/oauth/token"
    assert b"client_id=test-client" in token_request.data
    assert b"client_secret=test-secret" in token_request.data
    assert key_request.full_url == "https://api.tailscale.com/api/v2/tailnet/-/keys"
    assert key_request.get_method() == token_request.get_method() == "POST"
    assert key_request.get_header("Authorization") == "Bearer test-token"
    assert json.loads(key_request.data) == {
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    "ephemeral": False,
                    "preauthorized": True,
                    "tags": [tag],
                }
            }
        },
        "expirySeconds": 3600,
        "description": "maillard sidecar",
    }
    assert output.read_text() == "key=test-key\n"
    assert capsys.readouterr().out.splitlines() == [
        "::add-mask::test-token",
        "::add-mask::test-key",
    ]


@pytest.mark.parametrize("stage", ["token", "key", "invalid-response"])
def test_mint_failure_warns_with_acl_and_empty_output(tmp_path, monkeypatch, capsys, stage):
    output = tmp_path / "outputs"
    for name, value in {
        "GITHUB_OUTPUT": str(output),
        "TS_OAUTH_CLIENT_ID": "client",
        "TS_OAUTH_SECRET": "secret",
        "MAILLARD_TS_TAG": "tag:maillard",
    }.items():
        monkeypatch.setenv(name, value)
    error = urllib.error.HTTPError("https://example.invalid", 400, "secret-response-body", {}, None)
    replies = [error] if stage == "token" else [io.BytesIO(b'{"access_token":"test-token"}'), error]
    if stage == "invalid-response":
        replies[-1] = io.BytesIO(b'{"key":"invalid\\nkey"}')
    with patch("urllib.request.urlopen", side_effect=replies):
        exec(compile(MINT_SCRIPT, "mint-key", "exec"), {})
    assert output.read_text() == "key=\n"
    log = capsys.readouterr().out
    assert "::warning::" in log
    assert '{"tag:maillard": ["autogroup:admin", "tag:ci"]} into tagOwners' in log
    assert "secret-response-body" not in log
    assert "invalid\nkey" not in log


# All external commands used by the NAS script are replaced in PATH. The Docker
# mock checks the files sudo/Compose would read, and never contacts a daemon.
MOCK_COMMAND = r"""
import json
import os
import stat
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "curl":
    if args[-1].endswith("/api/status"):
        print(json.dumps({"roast": {"active": os.environ.get("TEST_ACTIVE") == "true"}}))
    elif args[-1].endswith("/health"):
        print(json.dumps({"status": "unhealthy" if os.environ.get("TEST_FAIL") == "health" else "healthy"}))
    else:
        print('{"version":"test"}')
    sys.exit(0)
if name == "sleep":
    sys.exit(0)
assert name == "sudo"
assert args.pop(0) == "/usr/local/bin/docker"
record = {"args": args}
auth = Path("tailscale/run/authkey")
if auth.is_file():
    record["auth_mode"] = stat.S_IMODE(auth.stat().st_mode)
    record["auth_matches"] = auth.read_text() == os.environ.get("MAILLARD_TS_AUTHKEY", "")
with Path("docker-calls.jsonl").open("a") as log:
    log.write(json.dumps(record) + "\n")
if args[0] == "volume":
    sys.exit(0 if os.environ.get("TEST_STATE") == "volume" else 1)
if args[0] != "compose":
    sys.exit(0)
if "--env-file" not in args:
    sys.exit(0)
assert args[:9] == ["compose", "--env-file", ".env", "--env-file", "tailscale/ts.env",
                    "-f", "docker-compose.yml", "-f", "docker-compose.tailscale.yml"]
assert Path("tailscale/ts.env").read_text() == "MAILLARD_TS_TAG=tag:coffee\n"
assert os.environ["MAILLARD_TS_AUTHKEY"] not in Path(".env").read_text() or not os.environ["MAILLARD_TS_AUTHKEY"]
op = args[9]
failure = os.environ.get("TEST_FAIL", "")
if op == "ps":
    if os.environ.get("TEST_STATE") == "container":
        print("saved-container")
elif op in ("pull", "up"):
    if failure == op:
        sys.exit(1)
elif op == "logs":
    print("mock sidecar log " + os.environ.get("MAILLARD_TS_AUTHKEY", ""))
elif op == "exec":
    if "maillard-ts" in args:
        print(json.dumps({"BackendState": "Starting" if failure == "status" else "Running",
                          "Self": {"HostName": "maillard", "DNSName": "maillard.test.ts.net.",
                                   "TailscaleIPs": ["100.64.0.1"]},
                          "Peer": {"private-peer": {"DNSName": "do-not-print"}}}))
    else:
        os.execv(sys.executable, [sys.executable, "-c", args[-1]])
"""


def run_nas(tmp_path, *, key="", state="", failure="", active=False):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    for name in ("sudo", "curl", "sleep"):
        executable = mock_bin / name
        executable.write_text(f"#!{sys.executable}\n" + MOCK_COMMAND)
        executable.chmod(0o755)
    (tmp_path / "docker-compose.yml").write_bytes((ROOT / "docker-compose.yml").read_bytes())
    (tmp_path / ".env").write_text("EXISTING=value\n")
    if failure == "files":
        (tmp_path / "tailscale").write_text("cannot create directory here")
    script = NAS_SCRIPT.replace("cd /volume1/docker/maillard", 'cd "$TEST_NAS_DIR"')
    result = subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        timeout=20,
        env={
            **os.environ,
            "PATH": f"{mock_bin}:{os.environ['PATH']}",
            "TEST_NAS_DIR": str(tmp_path),
            "TEST_ACTIVE": str(active).lower(),
            "TEST_STATE": state,
            "TEST_FAIL": failure,
            "MAILLARD_TS_AUTHKEY": key,
            "MAILLARD_TS_TAG": "tag:coffee",
            "MAILLARD_INGEST_SECRET": "test-ingest",
            "GHCR_TOKEN": "test-ghcr",
            "GHCR_USER": "tester",
            "DEPLOY_SHA": "test-sha",
        },
    )
    calls_file = tmp_path / "docker-calls.jsonl"
    calls = (
        [json.loads(line) for line in calls_file.read_text().splitlines()]
        if calls_file.exists()
        else []
    )
    return result, calls


@pytest.mark.parametrize("key,state", [("test-auth-key", ""), ("", "volume"), ("", "container")])
def test_sidecar_enrollment_and_saved_state(tmp_path, key, state):
    result, calls = run_nas(tmp_path, key=key, state=state)
    assert result.returncode == 0, result.stderr
    assert "::warning::" not in result.stdout
    assert "maillard.test.ts.net." in result.stdout and "100.64.0.1" in result.stdout
    assert "do-not-print" not in result.stdout
    assert not (tmp_path / "tailscale/run/authkey").exists()
    assert (tmp_path / "deploy.log").read_text().endswith("deployed test-sha\n")
    up = next(
        call for call in calls if call["args"][-4:] == ["up", "-d", "--no-deps", "maillard-ts"]
    )
    if key:
        assert key not in result.stdout + result.stderr
        assert up["auth_mode"] == 0o600 and up["auth_matches"]
    logout = next(i for i, call in enumerate(calls) if call["args"][0] == "logout")
    pull = next(i for i, call in enumerate(calls) if call["args"][-2:] == ["pull", "maillard-ts"])
    assert logout < pull


def test_no_key_or_saved_state_skips_sidecar(tmp_path):
    result, calls = run_nas(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "sidecar skipped" in result.stdout
    assert not any(call["args"][-2:] == ["pull", "maillard-ts"] for call in calls)


@pytest.mark.parametrize("failure", ["pull", "up", "status", "files"])
def test_sidecar_failures_warn_and_remove_key_without_failing_deploy(tmp_path, failure):
    result, calls = run_nas(tmp_path, key="test-auth-key", failure=failure)
    assert result.returncode == 0, result.stderr
    assert "::warning::Maillard sidecar failed" in result.stdout
    assert "test-auth-key" not in result.stdout + result.stderr
    assert not (tmp_path / "tailscale/run/authkey").exists()
    assert any(call["args"][-4:] == ["logs", "--tail", "30", "maillard-ts"] for call in calls)
    if failure == "status":
        assert sum(call["args"][-3:] == ["tailscale", "status", "--json"] for call in calls) == 12
    assert (tmp_path / "deploy.log").exists()


@pytest.mark.parametrize("active,failure", [(True, ""), (False, "health")])
def test_roast_guard_and_app_health_precede_sidecar(tmp_path, active, failure):
    result, calls = run_nas(tmp_path, key="test-auth-key", active=active, failure=failure)
    assert result.returncode == 1
    assert not (tmp_path / "tailscale").exists()
    assert not (tmp_path / "docker-compose.tailscale.yml").exists()
    assert not (tmp_path / "deploy.log").exists()
    if active:
        assert "Roast in progress; not restarting" in result.stdout
        assert calls == []
        assert (tmp_path / ".env").read_text() == "EXISTING=value\n"
        assert not list(tmp_path.glob(".env.bak.*"))


def test_documented_container_scripts_are_bundled():
    dockerfile = (ROOT / "Dockerfile").read_text()
    for path in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        for script in re.findall(r"/app/scripts/(\w+\.py)", path.read_text()):
            assert (ROOT / "scripts" / script).is_file()
            assert f"COPY scripts/{script} /app/scripts/{script}" in dockerfile
