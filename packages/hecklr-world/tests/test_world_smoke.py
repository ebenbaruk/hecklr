import hecklr_world


def test_package_imports_with_version() -> None:
    assert hecklr_world.__version__ == "0.1.0"
