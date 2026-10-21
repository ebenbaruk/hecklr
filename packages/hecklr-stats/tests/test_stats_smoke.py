import hecklr_stats


def test_package_imports_with_version() -> None:
    assert hecklr_stats.__version__ == "0.1.0"
