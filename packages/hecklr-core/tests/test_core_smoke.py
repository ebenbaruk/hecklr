import hecklr_core


def test_package_imports_with_version() -> None:
    assert hecklr_core.__version__ == "0.1.0"
