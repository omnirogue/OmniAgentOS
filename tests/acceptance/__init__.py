"""Orchestration Acceptance Test Suite.

These tests answer operator questions about a swarm run *before* and *around*
the work itself: were the right agents created, did each one receive a real
work contract, did the right context reach it, and would a missing prerequisite
have been caught before the run rather than halfway through it.

Every test here is hermetic: ``tmp_path`` only, no network, no live model call,
no write outside the worktree.
"""
