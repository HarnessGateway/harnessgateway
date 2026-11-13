import harnessgateway


def test_the_package_states_its_version() -> None:
    assert harnessgateway.__version__ == "4.0.0"
