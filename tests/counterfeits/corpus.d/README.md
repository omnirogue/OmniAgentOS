# `corpus.d/` — per-lane counterfeit fragments

Add your lane's counterfeits **here**, not at the end of `../corpus.toml`.

## Why

Every concurrent lane used to append its `[[counterfeit]]` entries to the EOF of
the single `corpus.toml`. Four lanes landed on main in one day and *every*
multi-lane rebase produced a textual conflict in that same file — a mechanical
conflict with no semantic content, re-resolved by hand each time.

One file per lane makes that conflict impossible by construction: two lanes
never write the same path, so git never has to merge them.

## How

Create `tests/counterfeits/corpus.d/<lane-id>.toml` — the file name is the lane
id, e.g. `lane-coordination.toml`. It uses the *same* format as `corpus.toml`:

```toml
# corpus.d/my-lane.toml — counterfeits owned by lane `my-lane`.

[[counterfeit]]
id = "cf-my-guard-inverted"
patch = "patches/cf-my-guard-inverted.patch"
rationale = "Inverts the containment check so a sibling prefix is admitted."
must_fail = [
  "tests/api/test_path_containment.py::test_string_prefix_counterfeit_fails_loudly",
]
failure_re = "string-prefix containment counterfeit"
```

`patch` is resolved against the **corpus root** (`tests/counterfeits/`), exactly
as it is in `corpus.toml` — so companion patches still live in
`tests/counterfeits/patches/`, and you can move an entry between `corpus.toml`
and a fragment without editing its `patch`.

## Rules the loader enforces (hard errors, never skips)

- **Unique ids.** The same `id` in two files — `corpus.toml` and a fragment, or
  two fragments — is a hard error naming both files. Silent last-wins would let
  one lane delete another lane's counterfeit and still report green.
- **Valid manifests.** A malformed or entry-less fragment is a hard error, with
  the same seriousness the runner gives a patch that fails to apply.
- **Deterministic order.** Base manifest first, then fragments in filename
  order. Runs are reproducible.
- **Non-`.toml` files are ignored.** This README is one; it keeps the directory
  in git without carrying a manifest.

An empty (or absent) `corpus.d/` behaves exactly as the corpus did before
fragments existed.

Loader: `load_corpus()` in `tests/counterfeits/harness.py`. `make
counterfeit-gate` and `scripts/merge-gate.sh` both drive the harness, so they
pick fragments up with no further wiring.
