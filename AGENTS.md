# Cloud Company Kit development

Read the project goal, current plan, source version and checkpoint first. Use
`docs/WORKFLOW.zh-CN.md` for plan-driven role activation and phase reporting.
Public role configuration lives in `.codex/`; it is loaded into the cloud worker
through the bundled deployment configuration.

Keep one accountable lead and one writer per file. Delegate independent work
when it helps; activate only the roles and tools required by the current plan.
Reuse the current Git state, worker supervisor, coordinator and Redis wakeup path.
Preserve existing behavior and user changes, and keep a rollback baseline.

Run the tests relevant to changed behavior. Use independent acceptance commands
and review real output. Save phase summaries and evidence in the working project,
with project identity, input version, next action and recovery instructions.

This repository is a reusable public distribution. Tests and examples use
synthetic data. Keep operational workspaces, credentials and private company
records in their own storage. Follow existing license files for packaged code.
