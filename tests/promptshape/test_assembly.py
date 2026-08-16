from omniagentos.promptshape.assembly import assemble


def test_assembly_preserves_layer_order_and_hashes_each_part() -> None:
    text, hashes = assemble({"authority": "system"}, ["context", "task"])
    assert text == "system\n\ncontext\n\ntask"
    assert set(hashes) == {"system:authority", "context:0", "context:1"}
    assert hashes["context:0"] != hashes["context:1"]
