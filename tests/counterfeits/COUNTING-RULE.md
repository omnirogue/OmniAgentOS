# Counterfeit corpus counting rule

The corpus total is a runtime result, not a constant.  Count it with
`len(load_corpus())` after every eligible manifest has parsed and passed validation.

## Snapshot at the UP-09 base

At `ebf3846178962053e15761de8bd312e7f425ee72`, mechanical loading produces:

```
29  entries in tests/counterfeits/corpus.toml (legacy manifest)
55  entries across 12 eligible tests/counterfeits/corpus.d/*.toml fragments
--
84  entries total
```

`corpus.d/README.md` is present but ineligible.  The 12 fragment counts, in loader order,
are `3, 2, 7, 12, 6, 2, 4, 1, 5, 3, 8, 2`.

Batch A must recount after UP-07 instead of preserving 84 in an assertion.  If UP-07 adds
one valid fragment entry and changes no existing entry, the expected recount is 29 legacy
plus 56 fragments = 85.  The durable acceptance rule remains the loader-derived count and
categories, so later fragments do not require editing a universal numeric constant.

## Eligibility and validation

These rules describe `tests/counterfeits/harness.py` as it exists at the base.

1. **Eligible files.** `discover_fragments()` sorts direct children by filename and keeps
   `p.is_file() and p.suffix == ".toml"`.  A missing fragment directory contributes zero.
   Hidden TOMLs and direct symlinks to regular TOMLs are therefore eligible; README files,
   subdirectories, and non-TOMLs are not.
2. **TOML structure.** Each eligible file is UTF-8 decoded and parsed with `tomllib`.
   Decode/parse errors fail closed.  `[[counterfeit]]` must exist as a non-empty array, and
   every row must be a table.
3. **Unique IDs.** Every row requires `id`; it is converted with `str()` and must be
   non-blank after normalization.  IDs are globally unique across the legacy manifest and
   all fragments under case- and surrounding-whitespace-insensitive normalization.
   Collisions fail the entire load and name both definitions.
4. **Patch existence and containment.** Every row requires `patch`; its string value must
   be non-blank, resolve within the corpus patch root by inode ancestry, and name an
   existing file.  No `.patch` suffix is required.  Application is checked later by the
   gate (`git apply`, then the `patch(1)` fallback), not by corpus counting.
5. **`must_fail`.** The key is required.  A string becomes one node ID and a list of
   strings becomes the node-ID tuple; other shapes fail.  An empty list fails, but the
   current parser does not reject a blank string.  Before a patched failure can count, the
   union of named controls must collect and pass unpatched; patched nodes must then fail.
6. **`failure_re`.** The key is required, converted with `str()`, and compiled case- and
   multiline-insensitively.  Invalid regex fails loading.  The current parser permits a
   blank regex (which matches any output); a non-blank requirement would be a future policy,
   not a rule counted here.  A patched failure counts as caught only when its combined
   output matches this expression.

Any eligibility or validation error invalidates the corpus load; partial counts are not a
valid repository-wide total.
