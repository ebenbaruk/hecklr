import hecklr_fix


def test_package_imports_with_version() -> None:
    assert hecklr_fix.__version__ == "0.1.0"
