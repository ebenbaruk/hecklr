import hecklr_grade


def test_package_imports_with_version() -> None:
    assert hecklr_grade.__version__ == "0.1.0"
