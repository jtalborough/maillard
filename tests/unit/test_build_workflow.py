import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_main_workflow_builds_and_reports_digest():
    source = (ROOT / ".github/workflows/deploy.yml").read_text()
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert set(workflow["jobs"]) == {"docker"}
    steps = workflow["jobs"]["docker"]["steps"]
    build = next(step for step in steps if step.get("id") == "build")
    assert build["uses"].startswith("docker/build-push-action@")
    assert build["with"]["push"] == "true"
    assert steps[-1]["env"]["DIGEST"] == "${{ steps.build.outputs.digest }}"
    assert "GITHUB_STEP_SUMMARY" in steps[-1]["run"]
    assert set(re.findall(r"secrets\.([A-Z_]+)", source)) == {"GITHUB_TOKEN"}
    assert all(step.get("uses", "").startswith(("docker/", "actions/")) for step in steps if "uses" in step)
    assert "tailscale" not in source.lower()
    assert "ssh-action" not in source.lower()


def test_documented_container_scripts_are_bundled():
    dockerfile = (ROOT / "Dockerfile").read_text()
    for path in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        for script in re.findall(r"/app/scripts/(\w+\.py)", path.read_text()):
            assert (ROOT / "scripts" / script).is_file()
            assert f"COPY scripts/{script} /app/scripts/{script}" in dockerfile
