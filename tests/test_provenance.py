import json

from lob_sim.provenance import source_tree_digest, write_experiment_manifest


def test_source_digest_is_independent_of_absolute_path(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "src").mkdir(parents=True)
        (root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert source_tree_digest(first) == source_tree_digest(second)


def test_manifest_records_configuration_and_input_digests(tmp_path) -> None:
    output = tmp_path / "experiment_manifest.json"
    digest_before_manifest = source_tree_digest(tmp_path)
    manifest = write_experiment_manifest(
        output,
        experiment_name="test_experiment",
        execution_mode="unit_test",
        configuration={"paths": 4, "dt_seconds": 0.1},
        strategy_names=["baseline", "candidate"],
        seeds=[0, 1, 2, 3],
        repository_root=tmp_path,
        input_digests={"event_tape": "abc123"},
    )

    restored = json.loads(output.read_text(encoding="utf-8"))
    assert manifest == restored
    assert restored["format"] == "lob_sim.experiment_manifest.v1"
    assert restored["configuration"]["paths"] == 4
    assert restored["input_digests"]["event_tape"] == "abc123"
    assert restored["source_digests"]["repository"] == digest_before_manifest
