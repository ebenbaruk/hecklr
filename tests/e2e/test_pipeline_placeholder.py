import pytest

import hecklr


@pytest.mark.e2e
@pytest.mark.fake
def test_meta_package_imports_every_core_package() -> None:
    import hecklr_adapters
    import hecklr_channels
    import hecklr_core
    import hecklr_fix
    import hecklr_grade
    import hecklr_llm
    import hecklr_report
    import hecklr_schemas
    import hecklr_sim
    import hecklr_stats
    import hecklr_world

    packages = [
        hecklr_adapters, hecklr_channels, hecklr_core, hecklr_fix, hecklr_grade, hecklr_llm,
        hecklr_report, hecklr_schemas, hecklr_sim, hecklr_stats, hecklr_world,
    ]  # fmt: skip
    assert {p.__version__ for p in packages} == {hecklr.__version__}
