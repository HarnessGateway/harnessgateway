"""The two packages the platform ships are valid on the contract as they sit in the repository: the console copies
`shell` for every new harness and `init` installs `harness-package-builder`, so a broken one breaks a fresh deploy.
The builder's skill carries the template and the packages guide: `scripts/sync-builder-seeds` keeps them current."""
import filecmp
from pathlib import Path

import pytest

from harnessgateway.domain import package as pkg

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
BUILDER = PACKAGES / "harness-package-builder"
SKILL = BUILDER / "seeds/home/.claude/skills/harness-package-builder"


@pytest.mark.parametrize("name", ["shell", "harness-package-builder"])
def test_a_shipped_package_validates_and_is_named_after_its_folder(name):
    manifest = pkg.validate(PACKAGES / name)
    assert manifest.name == name
    assert pkg.digest(PACKAGES / name)
    for script in ("install", "run"):                    # the three-file layout, executables with a shebang
        p = PACKAGES / name / script
        assert p.stat().st_mode & 0o111 and p.read_bytes().startswith(b"#!")
    assert not (PACKAGES / name / "run.sh").exists() and not (PACKAGES / name / "map.py").exists()
    assert (PACKAGES / name / "README.md").is_file() and (PACKAGES / name / "tests").is_dir()


def test_the_builder_reaches_everything_and_the_template_nothing():
    builder = pkg.validate(BUILDER)
    assert builder.run.egress == ["*"] and builder.provider and builder.provider.apis == ["anthropic"]
    assert (SKILL / "SKILL.md").is_file() and (BUILDER / "seeds/home/.claude/CLAUDE.md").is_file()
    assert not (BUILDER / "seeds/workspace").exists()          # the workspace is for the packages it delivers
    template = pkg.validate(PACKAGES / "shell")
    assert template.run.egress == [] and template.provider is None


def same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__"])
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(same_tree(a / d, b / d) for d in cmp.common_dirs)


def test_the_builder_ships_the_current_template_and_guide():
    """Stale: run scripts/sync-builder-seeds."""
    assert same_tree(PACKAGES / "shell", SKILL / "assets/template")
    assert filecmp.cmp(ROOT / "docs/packages.md", SKILL / "reference/packages.md", shallow=False)
