# Risk classes R0–R3 (OmniAgentOS)

Maps Volume I/II risk classes onto ActionClass + gates. **Separate product** — not merged to OmniAgentOS.

| Risk | Meaning | Typical ActionClass | Human? |
|------|---------|---------------------|--------|
| **R0** | Local reversible; scratch/workspace | `read_only`, `sandboxed_creation`, `internal_reversible` | Auto in AUTO mode |
| **R1** | Standard product work; declared scope | `internal_reversible`, `external_reversible` | Auto |
| **R2** | High blast / customer-visible / multi-file critical | often `external_reversible` or `consequential` | Auto unless it is a customer write |
| **R3** | Critical: money/customer writes, production deletes, Tier-P | `consequential` / `irreversible` | AD-15 finance-only park; bank writes refuse |

## Gate mapping

| Gate | Risk interaction |
|------|------------------|
| G0 intake | Reject unauthorized; attach risk class |
| G2 dispatch | Capacity + risk floors on model tier |
| G3 tool | Scope + ActionClass; AD-15 money/customer/production-delete gate; toolplane |
| G5 local verify | Mechanical + scope |
| G6 independent review | R2+ preferred |
| G8 release | Consequential send needs grant_id or human |
| G10 learning | Never self-promote |

## Implementation

- Task contracts: `omniagentos.taskcontract.RiskClass`
- Policy AUTO: `irreversible` is a routing floor; the approval resolver applies AD-15 finance-only decisions
- Money/customer writes and production or unresolved deletes park; bank writes always refuse
- Only absolute delete targets proven beneath isolated temp roots auto-approve (`scope: local_temp`)
- Secret reads park for a human (H3); remote commands are not approval hard stops and normal execution-contract scope still applies
- A `worktrees` substring alone never proves scratch scope; unresolved targets fail closed as production-delete

## Notes (human)
