import hecklr_schemas


def test_package_imports_with_version() -> None:
    assert hecklr_schemas.__version__ == "0.1.0"
