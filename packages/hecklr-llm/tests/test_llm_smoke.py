import hecklr_llm


def test_package_imports_with_version() -> None:
    assert hecklr_llm.__version__ == "0.1.0"
