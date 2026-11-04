# PatchHound stage planning

Determine which conditional review stages are relevant to the supplied patch.

Choose from only these conditional stage IDs:

- `resource_management`: allocation, ownership, reference counting, registration, cleanup, error unwinding,
  asynchronous work, timers, notifiers, or other lifecycle behavior may be affected;
- `locking_synchronization`: shared state, concurrency, locking, RCU, atomic context, interrupts, memory ordering, or
  teardown races may be affected;
- `security_audit`: input validation, trust boundaries, memory safety, privilege, information exposure, arithmetic,
  or attacker-influenced behavior may be affected;
- `hardware_driver_review`: drivers, registers, DMA, IRQs, device state, power, reset, suspend/resume, bus protocols,
  or hardware ordering may be affected.

Err on the side of running a stage. Omit a stage only when the supplied evidence makes it clearly irrelevant.
You may use any tools actually exposed by the runtime when useful, and should decide autonomously whether tool use is
needed for this planning task.

Return exactly one JSON object with no Markdown fence, explanation, or additional top-level key:

```json
{
  "conditional_stages": [
    {
      "stage_id": "resource_management",
      "reason": "The patch changes ownership transfer on an allocation failure path."
    }
  ]
}
```

Requirements:

- `conditional_stages` is an array, possibly empty;
- every item contains exactly `stage_id` and `reason`;
- each `stage_id` is unique and follows the order listed above;
- `reason` cites a concrete changed path, symbol, operation, or selected domain rule.

<selected_guides>
{{SELECTED_GUIDE_NAMES_JSON}}
</selected_guides>

{{SELECTED_REVIEW_GUIDANCE}}

<commit_message>
{{COMMIT_MESSAGE}}
</commit_message>

<patch_diff>
{{DIFF}}
</patch_diff>
