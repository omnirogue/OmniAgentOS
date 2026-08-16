"""C1 — the narrow park-list for irreversible NON-FINANCE actions.

Ratified by the operator on 2026-08-04. Before it, ``resolve_approval`` auto-approved with
NO human whenever ``_classify_request`` found no finance risk, and that
fall-through silently covered the two most consequential non-finance acts the
system can take. Measured on the pre-C1 HEAD, at the ActionClass the live
hook-eval path (``api/routes/sessions.py`` -> ``classify_shell``) really computes:

    vercel deploy --prod                       irreversible  -> AUTO-APPROVED
    gcloud app deploy app.yaml --quiet         irreversible  -> AUTO-APPROVED
    kubectl apply -f k8s/prod/deploy.yaml      irreversible  -> AUTO-APPROVED
    terraform apply -auto-approve              irreversible  -> AUTO-APPROVED
    ssh prod-web-01 'shutdown -h now'          consequential -> AUTO-APPROVED
    ssh prod-web-01 'mkfs.ext4 /dev/sdb1'      consequential -> AUTO-APPROVED
    ssh prod-web-01 'systemctl stop app'       consequential -> AUTO-APPROVED
    kubectl exec -it web-0 -- sh -c 'kill 1'   irreversible  -> AUTO-APPROVED

THIS FILE IS WRITTEN ADVERSARIALLY ON PURPOSE. ``approvals.py`` is a graveyard of
near-misses — ``wipe /srv/prod/customer_database`` and
``topup --wallet customer amount 500`` both auto-approved in production, and
``cat payload.txt | bash`` parked only because "payload" happens to contain "pay".
Coincidental coverage is not coverage. So every park here is paired with the
neighbouring request that must NOT park, and the discriminator is always
STRUCTURAL: a token only counts at a COMMAND POSITION, never because it appears
somewhere in the text.

Three properties are load-bearing and each has its own test below:

1. the rule is a CONJUNCTION — class floor AND enumerated surface;
2. it FAILS CLOSED — an unevaluable park-list parks, it never approves;
3. it is ADDITIVE — ``HARD_STOP_CLASSES`` / ``is_hard_stop`` are the frozen
   cross-package class floor auto-provisioning grants scope from, and nothing here
   reads, shadows or rebinds them.
"""

from __future__ import annotations

import inspect

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.orchestrator import approvals
from omniagentos.orchestrator.approvals import (
    PARK_LIST_PRODUCTION_DEPLOY,
    PARK_LIST_REMOTE_DESTRUCTIVE,
    PARK_LIST_UNEVALUABLE,
    classify_hard_stop,
    park_list_surface,
    resolve_approval,
)
from omniagentos.orchestrator.contracts import ApprovalRequest
from omniagentos.policy import HARD_STOP_CLASSES, is_hard_stop


def _request(command: str, action_class: str = "consequential") -> ApprovalRequest:
    return ApprovalRequest(
        proposed_action=command,
        action_class=action_class,
        tool_name="Bash",
        tool_input={"command": command},
    )


# ---------------------------------------------------------------------------
# Surface 1 — a production deploy parks.
# ---------------------------------------------------------------------------

PRODUCTION_DEPLOYS: tuple[tuple[str, str], ...] = (
    ("vercel deploy --prod", "preview-default tool, explicitly marked production"),
    ("vercel --prod", "the same tool without the deploy subcommand"),
    ("netlify deploy --prod", "second preview-default tool"),
    ("gcloud app deploy app.yaml --quiet", "production-default: there is no preview mode"),
    ("gcloud run deploy api --region us-central1", "same tool, different service"),
    ("firebase deploy", "production-default"),
    ("flyctl deploy", "production-default"),
    ("kubectl apply -f k8s/prod/deploy.yaml", "cluster state applied for real"),
    ("kubectl rollout restart deployment/api", "a rollout IS a deploy"),
    ("helm upgrade --install app ./chart --namespace prod", "release upgraded in place"),
    ("terraform apply -auto-approve", "infrastructure applied with no human in the loop"),
    ("tofu apply", "the terraform fork"),
    ("pulumi up --yes", "same act, different tool"),
    ("cdk deploy --require-approval never", "same act, different tool"),
    ("aws cloudformation deploy --stack-name api", "two required tokens, both present"),
    ("serverless deploy --stage prod", "production-default"),
    ("wrangler publish", "production-default"),
    ("npm publish", "a registry release cannot be taken back"),
    ("cargo publish", "same, other ecosystem"),
    ("twine upload dist/*", "same, other ecosystem"),
    ("./deploy.sh production", "the tool nobody enumerated: verb IS the executable"),
    ("bash deploy-prod.sh", "an interpreter running a deploy script"),
    ("make deploy-prod", "a task runner whose TARGET names the deploy"),
    ("npm run deploy:prod", "same shape, packed behind a colon"),
    ("ansible-playbook -i prod site.yml", "configuration pushed at a production inventory"),
    ("ssh deploy@web-01 'vercel deploy --prod'", "a deploy wrapped in a remote transport"),
    ("sudo terraform apply", "a wrapper does not hide the executable"),
)


@pytest.mark.parametrize(
    ("command", "why"), PRODUCTION_DEPLOYS, ids=[r[0] for r in PRODUCTION_DEPLOYS]
)
def test_a_production_deploy_parks(command: str, why: str) -> None:
    decision = resolve_approval(_request(command))
    assert decision.approved is False, f"AUTO-APPROVED a production deploy ({why}): {command}"
    assert decision.escalated is True
    assert park_list_surface(_request(command)) == PARK_LIST_PRODUCTION_DEPLOY


# ---------------------------------------------------------------------------
# Surface 2 — a remote destructive command parks.
# ---------------------------------------------------------------------------

REMOTE_DESTRUCTIVE: tuple[tuple[str, str], ...] = (
    ("ssh prod-web-01 'shutdown -h now'", "the halt that started this"),
    ("ssh prod-web-01 'poweroff'", "a synonym, so no row passes on one spelling"),
    ("ssh prod-web-01 'reboot'", "third spelling of the same act"),
    ("ssh prod-web-01 'mkfs.ext4 /dev/sdb1'", "mkfs is a FAMILY of executables"),
    ("ssh prod-web-01 'wipefs -a /dev/sdb'", "partition metadata destroyed"),
    ("ssh prod-web-01 'systemctl stop app'", "destructive only in this MODE"),
    ("ssh prod-web-01 'systemctl disable nginx'", "second destructive mode"),
    ("ssh -p 2222 prod-web-01 'sudo systemctl mask sshd'", "flags, values and a wrapper"),
    ("ssh prod 'crontab -r'", "the mode is a FLAG, not a word"),
    ("ssh prod 'redis-cli flushall'", "datastore emptied over the wire"),
    ("ssh prod 'dropdb customers'", "database dropped over the wire"),
    ("ssh prod 'userdel deploy'", "account destroyed"),
    ("ssh prod 'iptables --flush'", "the host loses its firewall"),
    ("ssh prod 'zfs destroy tank/data'", "storage destroyed"),
    ("ssh prod 'docker rm -f api'", "container destroyed"),
    ("ssh prod 'pkill -9 -f api'", "processes killed"),
    ("ssh prod 'cd /srv/app && rm -rf releases'", "a separator INSIDE the quoted payload"),
    ("ssh prod 'sh -c \"halt\"'", "nested interpreter inside the payload"),
    ("kubectl exec -it web-0 -- sh -c 'kill 1'", "kubectl exec is remote execution"),
    ("docker exec api rm -rf /data", "docker exec is remote execution"),
    ("mosh prod-web-01 'shutdown -h now'", "second transport"),
    ("gcloud compute ssh web-01 --command='shutdown -h now'", "the payload is packed behind ="),
    ("gcloud compute ssh web-01 --command 'halt'", "same transport, detached value"),
    (
        "aws ssm send-command --parameters 'commands=[\"reboot\"]'",
        "an API-shaped transport whose payload is JSON",
    ),
)


@pytest.mark.parametrize(
    ("command", "why"), REMOTE_DESTRUCTIVE, ids=[r[0] for r in REMOTE_DESTRUCTIVE]
)
def test_a_remote_destructive_command_parks(command: str, why: str) -> None:
    decision = resolve_approval(_request(command))
    assert decision.approved is False, f"AUTO-APPROVED remote destruction ({why}): {command}"
    assert decision.escalated is True


# ---------------------------------------------------------------------------
# The other half — ordinary reversible non-finance work still auto-approves.
# A classifier that parks everything is exactly as broken as one that approves
# everything, and only a true-negative row can tell those two apart.
# ---------------------------------------------------------------------------

STILL_AUTO_APPROVES: tuple[tuple[str, str], ...] = (
    ("vercel deploy", "a PREVIEW deploy names no production target"),
    ("netlify deploy", "same"),
    ("make deploy", "a deploy target with no production marker"),
    ("kubectl apply --dry-run=client -f k8s/prod/app.yaml", "a dry run deploys nothing"),
    ("terraform plan -var env=prod", "planning is not applying"),
    ("terraform apply --help", "reading the help text"),
    ("kubectl get pods -n prod", "reading a production namespace"),
    ("helm list -n prod", "listing releases"),
    ("cat deploy-prod.sh", "READING a deploy script is not deploying"),
    ("vim infra/prod/deploy.tf", "editing a deploy file is not deploying"),
    ("grep -rn 'terraform apply' docs/", "SEARCHING for a deploy command is a read"),
    ("git commit -m 'deploy the api to prod'", "a commit MESSAGE about a deploy"),
    ("git push origin main", "pushing is deliberately NOT enumerated"),
    ("git push origin prod", "not even to a branch named prod"),
    ("make build", "a task runner with no deploy target"),
    ("npm run build -- --pixel-ratio 2", "runner target that is not a deploy"),
    ("npm ci && npm test", "installing and testing"),
    ("pytest -q", "running tests"),
    ("ruff check .", "linting"),
    ("docker build -t app:latest .", "building an image"),
    ("bash scripts/build.sh", "an interpreter running a build script"),
    ("python -m pytest tests/x.py -k scrubbed_env", "-m is a flag; pytest is the operand"),
    ("tar czf backup.tgz /srv/production/releases", "BACKING UP a releases directory"),
    ("ssh host 'systemctl restart nginx'", "restart is not an enumerated mode"),
    ("ssh host 'grep -r kill /etc'", "'kill' as an ARGUMENT is not a remote kill"),
    ("ssh host 'apt list --installed'", "apt with a read subcommand"),
    ("ssh host 'df -h'", "reading remote disk usage"),
    ("ssh host 'tail -n 100 /var/log/app.log'", "reading a remote log"),
    ("ssh host 'docker ps -a'", "listing remote containers"),
    ("scp app.tar prod-web-01:/srv/app/", "a plain file copy is deliberately NOT enumerated"),
    ("systemctl stop app", "LOCAL halt: the ruling enumerated REMOTE ones"),
    ("sudo shutdown -h now", "LOCAL halt, wrapper and all"),
)


@pytest.mark.parametrize(
    ("command", "why"), STILL_AUTO_APPROVES, ids=[r[0] for r in STILL_AUTO_APPROVES]
)
def test_ordinary_non_finance_work_still_auto_approves(command: str, why: str) -> None:
    request = _request(command)
    decision = resolve_approval(request)
    assert decision.approved is True, (
        f"PARKED ordinary work ({why}): {command} -> {decision.reason}"
    )
    assert decision.category is None
    assert park_list_surface(request) is None
    assert decision.reason.startswith("auto-approved per finance-only policy")


# ---------------------------------------------------------------------------
# Property 1 — the rule is a CONJUNCTION. Both halves are load-bearing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action_class",
    ["read_only", "sandboxed_creation", "internal_reversible", "external_reversible"],
)
def test_the_class_floor_is_load_bearing(action_class: str) -> None:
    """Below CONSEQUENTIAL the park-list does not fire — that is the ruling's wording.

    Without this, "irreversible/consequential AND an enumerated surface" would
    have collapsed into "an enumerated surface", which is a different policy from
    the one the operator ratified.
    """
    request = _request("vercel deploy --prod", action_class)
    assert park_list_surface(request) is None
    assert resolve_approval(request).approved is True


@pytest.mark.parametrize("action_class", ["consequential", "irreversible"])
def test_the_class_floor_admits_both_ratified_classes(action_class: str) -> None:
    assert park_list_surface(_request("vercel deploy --prod", action_class)) is not None
    assert park_list_surface(_request("ssh prod 'halt'", action_class)) is not None


def test_the_surface_half_is_load_bearing() -> None:
    """``classify_shell`` calls ordinary work irreversible too, so the class alone parks nothing.

    Measured: ``make build``, ``pytest -q`` and ``ruff check .`` are all
    ``ActionClass.IRREVERSIBLE``. If the class half were sufficient, C1 would have
    parked the entire engineering loop.
    """
    for command in ("make build", "pytest -q", "ruff check ."):
        request = _request(command, "irreversible")
        assert park_list_surface(request) is None, command
        assert resolve_approval(request).approved is True, command


def test_a_deploy_word_outside_a_command_position_never_parks() -> None:
    """The discriminator is STRUCTURE, not vocabulary.

    Every string below contains both halves of the deploy vocabulary ("deploy"
    and "prod") and none of them deploys anything. A keyword matcher parks all
    five; this rule parks none.
    """
    for command in (
        "grep -rn deploy infra/prod/",
        "cat infra/prod/deploy.yaml",
        "ls -la /srv/prod/deploy",
        "git log --oneline --grep 'deploy to prod'",
        "echo 'terraform apply on prod'",
    ):
        assert park_list_surface(_request(command)) is None, command


def test_a_destructive_word_outside_a_command_position_never_parks() -> None:
    """Same argument on the remote side: an ARGUMENT is not an executable."""
    for command in (
        "ssh host 'ls /home/rm'",
        "ssh host 'cat /var/log/shutdown.log'",
        "ssh host 'grep -c halt /etc/motd'",
        "ssh host 'echo reboot'",
    ):
        assert park_list_surface(_request(command)) is None, command


def test_remoteness_is_required_for_the_destructive_surface() -> None:
    """The ruling enumerated REMOTE destructive commands, not every destructive command.

    A LOCAL ``systemctl stop`` keeps its pre-C1 behaviour exactly; adding the
    remote transport is the only difference between these two rows.
    """
    assert park_list_surface(_request("systemctl stop app")) is None
    assert park_list_surface(_request("ssh prod 'systemctl stop app'")) == (
        PARK_LIST_REMOTE_DESTRUCTIVE
    )


# ---------------------------------------------------------------------------
# Property 2 — it FAILS CLOSED.
# ---------------------------------------------------------------------------


def test_a_park_list_evaluation_error_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unevaluable park-list must ask a human, never wave the request through.

    Uncertainty in this module has exactly one safe direction, and a classifier
    that crashed open would be a worse defect than the one C1 fixes.
    """

    def _boom(_segment: str) -> list[str]:
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(approvals, "_park_list_segments", _boom)
    request = _request("make build")
    decision = resolve_approval(request)
    assert decision.approved is False
    assert decision.escalated is True
    assert decision.category == "delete"
    assert f"trigger: {PARK_LIST_UNEVALUABLE}" in decision.reason
    with pytest.raises(RuntimeError):
        park_list_surface(request)


def test_an_unknown_action_class_parks_on_an_enumerated_surface() -> None:
    """A malformed class is uncertainty, and uncertainty parks.

    Same direction ``is_hard_stop`` already fails, reached through a SEPARATE
    predicate over a SEPARATE set.
    """
    assert park_list_surface(_request("vercel deploy --prod", "not-a-real-class")) == (
        PARK_LIST_PRODUCTION_DEPLOY
    )
    # ...but an unknown class is still not a park by itself.
    assert park_list_surface(_request("make build", "not-a-real-class")) is None


def test_a_park_list_park_is_escalated_and_named_in_the_audit_reason() -> None:
    """The audit must be truthful: this park did not come from the finance policy."""
    decision = resolve_approval(_request("terraform apply -auto-approve"))
    assert decision.reason == (
        "parked per non-finance park-list "
        "(class: consequential; trigger: production-deploy; scope: unresolved)"
    )


# ---------------------------------------------------------------------------
# Property 3 — additive. The frozen class floor is untouched, and finance
# behaviour is byte-for-byte what it was.
# ---------------------------------------------------------------------------


def test_hard_stop_class_floor_is_unchanged() -> None:
    """``HARD_STOP_CLASSES`` is the frozen predicate auto-provisioning grants scope from.

    C1 was explicitly narrowed so this could stay frozen. If a later change tries
    to implement "park every irreversible action" by widening the class floor
    instead, this test is what fails.
    """
    assert HARD_STOP_CLASSES == frozenset({ActionClass.IRREVERSIBLE})
    assert is_hard_stop(ActionClass.IRREVERSIBLE) is True
    for lower in (
        ActionClass.READ_ONLY,
        ActionClass.SANDBOXED_CREATION,
        ActionClass.INTERNAL_REVERSIBLE,
        ActionClass.EXTERNAL_REVERSIBLE,
        ActionClass.CONSEQUENTIAL,
    ):
        assert is_hard_stop(lower) is False
    assert is_hard_stop("not-a-real-class") is True  # still fails closed
    assert is_hard_stop(None) is True  # type: ignore[arg-type]


def test_the_park_list_neither_imports_nor_rebinds_the_frozen_floor() -> None:
    """A separate resolver step, not a redefinition of the class floor.

    Asserted on the module object AND on its source, because "we did not change
    ``HARD_STOP_CLASSES``" has to be checkable, not merely claimed in a comment.
    """
    assert not hasattr(approvals, "HARD_STOP_CLASSES")
    assert not hasattr(approvals, "is_hard_stop")
    source = inspect.getsource(approvals)
    assert "HARD_STOP_CLASSES =" not in source
    assert "def is_hard_stop" not in source
    # The park-list's own floor is a distinct set with a distinct membership.
    assert approvals._PARK_LIST_CLASS_FLOOR == frozenset(
        {ActionClass.CONSEQUENTIAL, ActionClass.IRREVERSIBLE}
    )
    assert approvals._PARK_LIST_CLASS_FLOOR != HARD_STOP_CLASSES


FINANCE_UNCHANGED: tuple[tuple[str, str, str], ...] = (
    # (command, category, pre-C1 audit reason -- reproduced verbatim)
    (
        "wipe /srv/prod/customer_database",
        "delete",
        "parked per finance-only policy "
        "(class: consequential; trigger: production-delete; scope: production)",
    ),
    (
        "zelle send 500 to X",
        "money",
        "parked per finance-only policy (class: consequential; trigger: money; scope: unresolved)",
    ),
    (
        "topup --wallet customer amount 500",
        "money",
        "parked per finance-only policy (class: consequential; trigger: money; scope: unresolved)",
    ),
    (
        "stripe charges create --amount 100",
        "money",
        "parked per finance-only policy (class: consequential; trigger: money; scope: unresolved)",
    ),
)


@pytest.mark.parametrize(
    ("command", "category", "reason"), FINANCE_UNCHANGED, ids=[r[0] for r in FINANCE_UNCHANGED]
)
def test_a_finance_action_behaves_exactly_as_before(
    command: str, category: str, reason: str
) -> None:
    """The park-list runs ONLY where the finance classifier auto-approved.

    Reasons are pinned verbatim, not just "it parked": the finance path's audit
    strings are consumed downstream (``toolplane/session.py`` maps them to denial
    codes), so a silent reword would be a real regression.
    """
    request = _request(command)
    decision = resolve_approval(request)
    assert decision.approved is False
    assert decision.category == category
    assert classify_hard_stop(request) == category
    assert decision.reason == reason


def test_a_bank_write_is_still_refused_not_parked() -> None:
    """The permanent refusal outranks everything, including a park-list surface."""
    decision = resolve_approval(_request("ssh prod 'wire transfer 500 via ACH to bank account'"))
    assert decision.category == "bank"
    assert decision.escalated is False
    assert decision.reason.startswith("refused per finance-only policy")
