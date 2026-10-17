import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import install_skill as ins  # noqa: E402


def test_install_copies_skill_without_caches(tmp_path):
    dest = ins.install(str(tmp_path / "skills"))
    assert os.path.isfile(os.path.join(dest, "SKILL.md"))
    assert os.path.isfile(os.path.join(dest, "scripts", "place_services.py"))
    assert os.path.isfile(os.path.join(dest, "references", "placement-rules.json"))
    for root, dirs, files in os.walk(dest):
        assert "__pycache__" not in dirs
    assert not os.path.exists(os.path.join(dest, "README.md"))


def test_second_install_refused_unless_forced(tmp_path):
    target = str(tmp_path / "skills")
    assert ins.install(target) is not None
    marker = os.path.join(target, ins.SKILL, "marker.txt")
    open(marker, "w").write("x")
    assert ins.install(target) is None
    assert os.path.exists(marker)
    assert ins.install(target, force=True) is not None
    assert not os.path.exists(marker)


def test_target_resolution(tmp_path):
    assert ins.resolve_target("claude", str(tmp_path)) == os.path.join(str(tmp_path), ".claude", "skills")
    assert ins.resolve_target("agents", str(tmp_path)) == os.path.join(str(tmp_path), ".agents", "skills")
    assert ins.resolve_target(str(tmp_path / "x")) == str(tmp_path / "x")


def test_cli_exit_codes(tmp_path):
    t = str(tmp_path / "s")
    assert ins.main(["--target", t]) == 0
    assert ins.main(["--target", t]) == 1
    assert ins.main(["--target", t, "--force"]) == 0
