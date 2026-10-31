# Contributing

Start with a concrete issue, expected behavior and the smallest reproducible case.
Keep changes focused and preserve working project behavior. Reuse the existing
runtime and deployment paths before adding services or dependencies.

Run the checks affected by your patch. Runtime changes use `tests/`; deployment
changes use `deploy/test_deployment.py` and the Docker smoke workflow; templates
use `templates/tests/`. Document any verification that requires credentials.

Include the change, reason, evidence and rollback approach in the pull request.
Use synthetic inputs in tests and examples. Real workspaces, credentials, account
state and production evidence belong outside the public source repository.

For workflow improvements, include a previous failure case and compare the new
behavior with the baseline. Discuss substantial architecture changes in an issue
before implementation.
