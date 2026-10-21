import hecklr_mine


def test_package_imports_with_version() -> None:
    assert hecklr_mine.__version__ == "0.1.0"
