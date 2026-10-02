"""Strategy intake: spec v2 validation, deployment profiles and execution resolution.

Three inputs stay separate. The spec (`ideas/<idea>/spec.yaml`) holds strategy intent only. A
deployment profile (`research/profiles/<name>.yaml`) holds broker and product rules and is shared.
Execution settings are resolved by the platform, with optional per-idea overrides in
`execution.yaml`. `fields` is the leaf module: nothing it imports may import it back.
"""
