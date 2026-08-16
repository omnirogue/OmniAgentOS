"""Graph Runtime V2 service — typed diamond execution with fan-in completeness."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from omniagentos.graph_runtime.contracts import (
    BUILTIN_TEMPLATES,
    DIAMOND_TEMPLATE,
    GraphTemplate,
    compile_template,
)
from omniagentos.graph_runtime.store import GraphStore

LOG = logging.getLogger(__name__)


class NodeCompletionConflict(ValueError):
    """Another writer transitioned the node out of ``ready``/``running`` first.

    Distinguishable from the hard rejections ``complete_node`` also raises: the
    call was legitimate when it started and nothing of it was written, so a
    caller may re-read the node and treat the winner's result as its own.
    """


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GraphRuntimeService:
    def __init__(self, store: GraphStore | None = None, db_path: str | None = None) -> None:
        self.store = store or GraphStore(db_path)
        self.store.seed_templates()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": "graph-v2",
            "templates": [t.slug for t in BUILTIN_TEMPLATES],
            "default_template": DIAMOND_TEMPLATE.slug,
            "live": True,
        }

    def list_templates(self) -> list[dict[str, Any]]:
        return self.store.list_templates()

    def start_diamond(
        self,
        *,
        title: str = "Diamond graph run",
        goal: str = "",
        company_id: str | None = None,
        project_id: str | None = None,
        completeness_policy: str = "fail_closed",
    ) -> dict[str, Any]:
        """Start the built-in fan-out → reduce → verify → synthesize product."""
        return self.start_from_template(
            DIAMOND_TEMPLATE.slug,
            title=title or goal or "Diamond graph run",
            company_id=company_id,
            project_id=project_id,
            completeness_policy=completeness_policy,
        )

    def compile(self, template: GraphTemplate | str) -> dict[str, Any]:
        """Validate and order a template (GraphCompiler surface)."""
        if isinstance(template, str):
            t = self.store.get_template(template) or next(
                (x for x in BUILTIN_TEMPLATES if x.slug == template), None
            )
            if t is None:
                raise KeyError(f"unknown template {template}")
            template = t
        return compile_template(template)

    def start_from_template(
        self,
        slug: str,
        *,
        title: str,
        company_id: str | None = None,
        project_id: str | None = None,
        completeness_policy: str | None = None,
        swarm_run_id: str | None = None,
    ) -> dict[str, Any]:
        template = self.store.get_template(slug)
        if template is None:
            # Fall back to in-memory builtins
            template = next((t for t in BUILTIN_TEMPLATES if t.slug == slug), None)
        if template is None:
            raise KeyError(f"unknown template {slug}")
        report = compile_template(template)
        if not report["ok"]:
            raise ValueError(f"template failed compile: {report['errors']}")
        return self.store.create_run(
            title=title,
            template=template,
            completeness_policy=completeness_policy,
            company_id=company_id,
            project_id=project_id,
            swarm_run_id=swarm_run_id,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(run_id)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_runs(limit=limit)

    def ready_nodes(self, run_id: str) -> list[dict[str, Any]]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return [n for n in run["nodes"] if n["status"] in {"ready", "running"}]

    def complete_node(
        self,
        run_id: str,
        node_key: str,
        *,
        outputs: dict[str, Any],
        verified: bool = False,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Publish outputs for a node, bind downstream inputs, advance readiness.

        Idempotent for already-completed nodes (M-39). Fail-closed gates are
        unbypassable: only ``ready``/``running`` nodes may be completed or failed;
        ``blocked``/``pending`` completions are rejected so synthesize cannot
        skip an unverified verify edge.

        M-40: every write here — the output artifacts, the node transition and
        the readiness propagation — is one transaction. Artifact ids are minted
        per call, so under autocommit a failure partway through left orphan
        artifact rows that a retry could not adopt: the retry minted new ids and
        the run accumulated duplicate artifacts for the same ``(node, port)``,
        with the node still sitting in ``ready``.

        FH-001: the gates below run on a status read outside that transaction,
        which two concurrent completions of one ready node both pass. The
        transition is therefore claimed as a compare-and-swap first thing inside
        the transaction; the racer that loses it raises
        ``NodeCompletionConflict`` without publishing an artifact.
        """
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        node = next((n for n in run["nodes"] if n["key"] == node_key), None)
        if node is None:
            raise KeyError(f"node {node_key}")
        # M-39: Idempotent — if already completed, return run without changes
        if node.get("status") == "completed":
            return run
        # M-39: Fail-closed gate enforcement — only ready/running nodes can complete
        status = str(node.get("status") or "")
        if status not in {"ready", "running"}:
            raise ValueError(
                f"cannot complete node {node_key} in status {status!r}"
                + (f": {node.get('error')}" if node.get("error") else "")
            )
        if error:
            with self.store.transaction("graph_fail_node"):
                if not self.store.transition_node(
                    run_id, node_key, to_status="failed", error=error
                ):
                    raise NodeCompletionConflict(
                        f"node {node_key} left ready/running before this failure was recorded"
                    )
                self.store.set_run_status(run_id, "failed")
            return self.store.get_run(run_id) or {}

        # Schema check: required output ports must be present
        ports = node.get("output_ports") or []
        out_map: dict[str, str] = {}
        with self.store.transaction("graph_complete_node"):
            # FH-001: claim the node before anything is published for it.
            if not self.store.transition_node(run_id, node_key, to_status="completed"):
                raise NodeCompletionConflict(f"node {node_key} was completed by another writer")
            for port in ports:
                name = port.get("name") if isinstance(port, dict) else getattr(port, "name", None)
                if not name:
                    continue
                if name not in outputs:
                    if port.get("required") if isinstance(port, dict) else True:
                        raise ValueError(f"missing required output port {name} on {node_key}")
                    continue
                payload = outputs[name]
                raw = (
                    json.dumps(payload, sort_keys=True).encode("utf-8")
                    if not isinstance(payload, (bytes, bytearray))
                    else payload
                )
                if isinstance(payload, (bytes, bytearray)):
                    content_hash = _sha(bytes(payload))
                    uri = f"memory://{run_id}/{node_key}/{name}"
                    body: dict[str, Any] = {"bytes_len": len(payload)}
                else:
                    content_hash = _sha(
                        raw
                        if isinstance(raw, bytes)
                        else json.dumps(payload, sort_keys=True).encode()
                    )
                    uri = f"memory://{run_id}/{node_key}/{name}"
                    body = payload if isinstance(payload, dict) else {"value": payload}
                art_type = (
                    port.get("artifact_type") if isinstance(port, dict) else "json"
                ) or "json"
                # verified is explicit: verify-node outputs are NOT auto-trusted.
                # Callers pass verified=True only after a fresh check actually passed.
                art_id = self.store.insert_artifact(
                    {
                        "graph_run_id": run_id,
                        "node_key": node_key,
                        "port": name,
                        "artifact_type": art_type,
                        "content_hash": content_hash,
                        "content_uri": uri,
                        "schema_ok": True,
                        "verified": bool(verified),
                        "payload": body,
                    }
                )
                out_map[name] = art_id

            self.store.update_node(run_id, node_key, output_artifacts=out_map)
            self._propagate(run_id)
        return self.store.get_run(run_id) or {}

    def _propagate(self, run_id: str) -> None:
        """Bind artifacts along edges; mark nodes ready/blocked/completed."""
        run = self.store.get_run(run_id)
        if run is None:
            return
        edges = run["edges"]
        nodes = {n["key"]: n for n in run["nodes"]}
        arts_by_id = {a["id"]: a for a in run["artifacts"]}
        # Map (node, port) -> art_id from completed nodes
        produced: dict[tuple[str, str], str] = {}
        for n in run["nodes"]:
            for port, art_id in (n.get("output_artifacts") or {}).items():
                produced[(n["key"], port)] = art_id

        policy = str(run.get("completeness_policy") or "fail_closed")
        any_failed = False
        for n in run["nodes"]:
            if n["status"] == "failed":
                any_failed = True

            if n["status"] not in {"pending", "ready", "blocked"}:
                continue

            # Incoming edges
            incoming = [e for e in edges if e["to_node_key"] == n["key"]]
            if not incoming:
                if n["status"] == "pending":
                    self.store.update_node(run_id, n["key"], status="ready")
                continue

            bindings: dict[str, str] = {}
            missing_required = []
            for e in incoming:
                key = (e["from_node_key"], e["from_port"])
                art_id = produced.get(key)
                if art_id:
                    # Completeness: schema_ok + verified for verify→synth
                    art = arts_by_id.get(art_id) or {}
                    if policy == "fail_closed" and e.get("required"):
                        if not art.get("schema_ok", 1):
                            missing_required.append(f"{e['from_node_key']}.{e['from_port']}:schema")
                            continue
                        # verify outputs must be verified when feeding synthesize.
                        # Keyed on the source node's TYPE, never on its name: a
                        # renamed or nested verifier is still a verifier, and a
                        # node merely CALLED "verify" is not one.
                        src_type = (nodes.get(e["from_node_key"]) or {}).get("node_type")
                        if n["node_type"] == "synthesize" and src_type == "verify":
                            if not art.get("verified"):
                                missing_required.append(
                                    f"{e['from_node_key']}.{e['from_port']}:unverified"
                                )
                                continue
                    bindings[e["to_port"]] = art_id
                elif e.get("required", 1):
                    src = nodes.get(e["from_node_key"])
                    if src and src["status"] == "failed":
                        missing_required.append(f"{e['from_node_key']}:failed")
                    elif src and src["status"] != "completed":
                        missing_required.append(f"{e['from_node_key']}:pending")
                    else:
                        missing_required.append(f"{e['from_node_key']}.{e['from_port']}")

            if missing_required:
                # If any upstream still running/pending, stay pending/blocked
                upstream_live = any(
                    nodes.get(e["from_node_key"], {}).get("status")
                    in {"pending", "ready", "running"}
                    for e in incoming
                )
                if upstream_live:
                    self.store.update_node(run_id, n["key"], status="pending")
                else:
                    if policy == "fail_closed":
                        self.store.update_node(
                            run_id,
                            n["key"],
                            status="blocked",
                            error=f"incomplete fan-in: {', '.join(missing_required)}",
                        )
                    else:
                        # allow_partial: ready with what we have if any binding
                        if bindings:
                            self.store.update_node(
                                run_id,
                                n["key"],
                                status="ready",
                                input_bindings=bindings,
                            )
                        else:
                            self.store.update_node(
                                run_id,
                                n["key"],
                                status="blocked",
                                error="no inputs for partial policy",
                            )
            else:
                self.store.update_node(run_id, n["key"], status="ready", input_bindings=bindings)

        # Re-read for terminal state
        run = self.store.get_run(run_id)
        if run is None:
            return
        statuses = {n["status"] for n in run["nodes"]}
        if any_failed or "failed" in statuses:
            self.store.set_run_status(run_id, "failed")
        elif all(s in {"completed", "skipped", "cancelled"} for s in statuses):
            self.store.set_run_status(run_id, "completed")
        elif "blocked" in statuses and not any(s in {"ready", "running"} for s in statuses):
            self.store.set_run_status(run_id, "blocked")
        else:
            self.store.set_run_status(run_id, "running")

    def run_diamond_deterministic(
        self,
        *,
        title: str = "Diamond demo",
        findings_a: dict[str, Any] | None = None,
        findings_b: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute the full diamond with deterministic reduce/verify/synth (no LLM)."""
        run = self.start_diamond(title=title)
        run_id = run["id"]
        a = findings_a or {"claim": "A", "score": 0.8, "source": "worker-a"}
        b = findings_b or {"claim": "B", "score": 0.9, "source": "worker-b"}
        self.complete_node(run_id, "fan_a", outputs={"finding": a})
        self.complete_node(run_id, "fan_b", outputs={"finding": b})
        # Reduce: deterministic merge + rank
        items: list[dict[str, Any]] = sorted([a, b], key=lambda x: -float(x.get("score") or 0))
        reduced: dict[str, Any] = {
            "items": items,
            "count": 2,
            "reducer": "deterministic_rank",
        }
        self.complete_node(run_id, "reduce", outputs={"reduced": reduced})
        # Verify: fresh check — require score thresholds
        ok = all(float(i.get("score") or 0) >= 0.5 for i in items)
        verdict = {
            "passed": ok,
            "lenses": {"correctness": ok, "support": ok, "currentness": True},
            "verifier": "fresh_mechanical",
        }
        self.complete_node(run_id, "verify", outputs={"verdict": verdict}, verified=ok)
        if not ok:
            return self.store.get_run(run_id) or {}
        deliverable = {
            "summary": f"Synthesized {reduced['count']} findings",
            "top": items[0] if items else {},
            "verdict": verdict,
        }
        self.complete_node(run_id, "synthesize", outputs={"deliverable": deliverable})
        return self.store.get_run(run_id) or {}

    def live_view(self, run_id: str) -> dict[str, Any]:
        """Operator graph view: nodes, edges, expected vs received, critical path."""
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        edges = run["edges"]
        produced = {}
        for n in run["nodes"]:
            for port, art_id in (n.get("output_artifacts") or {}).items():
                produced[(n["key"], port)] = art_id
        edge_flow = []
        for e in edges:
            key = (e["from_node_key"], e["from_port"])
            art_id = produced.get(key)
            edge_flow.append(
                {
                    "from": e["from_node_key"],
                    "to": e["to_node_key"],
                    "from_port": e["from_port"],
                    "to_port": e["to_port"],
                    "required": bool(e.get("required")),
                    "artifact_id": art_id,
                    "status": "bound" if art_id else "waiting",
                }
            )
        return {
            "run_id": run_id,
            "title": run.get("title"),
            "status": run.get("status"),
            "completeness_policy": run.get("completeness_policy"),
            "template_slug": run.get("template_slug"),
            "critical_path": run.get("critical_path"),
            "nodes": [
                {
                    "key": n["key"],
                    "type": n["node_type"],
                    "title": n["title"],
                    "status": n["status"],
                    "model_role": n.get("model_role"),
                    "outputs": n.get("output_artifacts") or {},
                    "inputs": n.get("input_bindings") or {},
                    "error": n.get("error"),
                }
                for n in run["nodes"]
            ],
            "edge_flow": edge_flow,
            "artifact_count": len(run.get("artifacts") or []),
            "cost_usd": run.get("cost_usd"),
        }
