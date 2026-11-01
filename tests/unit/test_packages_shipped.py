"""The two packages the platform ships are valid on the contract as they sit in the repository: the console copies
`shell` for every new harness and `init` installs `harness-package-builder`, so a broken one breaks a fresh deploy."""
from pathlib import Path

import pytest

from harnessgateway.domain import package as pkg

PACKAGES = Path(__file__).resolve().parents[2] / "packages"


@pytest.mark.parametrize("name", ["shell", "harness-package-builder"])
def test_a_shipped_package_validates_and_is_named_after_its_folder(name):
    manifest = pkg.validate(PACKAGES / name)
    assert manifest.name == name
    assert pkg.digest(PACKAGES / name)
    assert (PACKAGES / name / "README.md").is_file() and (PACKAGES / name / "tests").is_dir()


def test_the_builder_reaches_everything_and_the_template_nothing():
    builder = pkg.validate(PACKAGES / "harness-package-builder")
    assert builder.run.egress == ["*"] and builder.provider and builder.provider.apis == ["anthropic"]
    assert (PACKAGES / "harness-package-builder/seeds/home/.claude/skills/harness-package-builder/SKILL.md").is_file()
    template = pkg.validate(PACKAGES / "shell")
    assert template.run.egress == [] and template.provider is None
