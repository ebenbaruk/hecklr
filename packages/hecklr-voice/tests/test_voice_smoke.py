import hecklr_voice


def test_package_imports_with_version() -> None:
    assert hecklr_voice.__version__ == "0.1.0"
