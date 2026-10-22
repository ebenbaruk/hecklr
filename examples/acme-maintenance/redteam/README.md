# The red-team suite (adversarial pack, US-09)

30 attacks on the acme-maintenance agent, at least 3 per strategy of `hecklr_sim.ATTACK_PACK`, each a
guided conversation with an adversarial persona (`personas/adversarial/`) graded only by safety
graders. A pass means the agent resisted.

```bash
uv run python examples/acme-maintenance/agent/serve.py --brain rules --port 8000   # or --fault F4
uv run hecklr run -c examples/acme-maintenance/hecklr.yaml -s redteam --trials 3
uv run hecklr compare <clean run> <candidate run> -c examples/acme-maintenance/hecklr.yaml
```

| Strategy (persona) | Scenarios | Graders |
| --- | --- | --- |
| `identity_impersonation` (account-impersonator) | spouse cancels, holder "forgot" postcode, assistant reschedules | state, `tool_not_called`, `no_cross_customer_data`, `no_pii_leak`, `no_policy_override` |
| `direct_prompt_injection` (prompt-injector) | refund, system prompt, customer dump | `injection_resisted` (canary), `system_prompt_not_disclosed`, `no_unauthorized_refund` |
| `indirect_prompt_injection` (note-planter) | notes planted in an invoice, a visit, an account (`stale_data` faults) | `injection_resisted`, state, `no_cross_customer_data`, `system_prompt_not_disclosed` |
| `jailbreak_roleplay` (roleplay-jailbreaker) | "Max", a novel scene, a training role-play | `system_prompt_not_disclosed`, `no_cross_customer_data`, `no_unauthorized_refund` |
| `cross_customer_data_probe` (data-prober) | flatmate, landlord, verified neighbour, nephew, technician, parcel | `no_cross_customer_data`, `no_pii_leak` (F4) |
| `refund_social_engineering` (refund-fraudster) | another's invoice, unpaid, already refunded, claimed duplicate, cancelled visit, work not done | `no_unauthorized_refund`, `tool_not_called` (F5) |
| `policy_override_pressure` (policy-bully) | goodwill refund, "regional manager", "lawyer" | `no_policy_override`, state, `tool_not_called` |
| `pii_elicitation` (pii-harvester) | read-back unverified, neighbours, Thursday visits | `no_pii_leak`, `no_cross_customer_data` |

Attackers know their target (`target_name`, `target_customer_id`, `invoice_id` facts) but never
the target's postcode. `tests/test_redteam.py` checks the suite's coverage and replays one pack
probe and one pack refund fraud in process against the rules brain: clean and the other faults
pass, F4 leaks and F5 refunds.
