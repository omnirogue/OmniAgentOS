"""W4-10 — Counterfeit corpus gate.

A standing corpus of fakes the suite must catch. Each entry is a mutation that
makes a fix LOOK done without doing the work, plus the named test(s) that must
go red when the mutation is applied. If a counterfeit survives, coverage of that
area is decoration.
"""
