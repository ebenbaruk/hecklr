import hecklr_report


def test_package_imports_with_version() -> None:
    assert hecklr_report.__version__ == "0.1.0"
