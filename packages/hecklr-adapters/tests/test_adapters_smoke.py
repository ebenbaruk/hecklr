import hecklr_adapters


def test_package_imports_with_version() -> None:
    assert hecklr_adapters.__version__ == "0.1.0"
