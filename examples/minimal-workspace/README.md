# Minimal Workspace Example

This example shows the intended split:

- external dependency `eval` provides protocol base classes
- the consumer repository keeps a local `evals/` workspace

This example is intentionally generic and does not depend on any private project.

## Layout

```text
minimal-workspace/
  pyproject.toml
  evals/
    config/
      app.yaml
      targets/
        demo-chat.yaml
        demo-responses.yaml
    cases/
      demo-chat/
      demo-responses/
    targets/
      demo_chat.py
      demo_responses.py
    eval_cli.py
```

## What It Demonstrates

- how to depend on the external git package
- how `driver_class` points at a local target implementation
- how a local target inherits a protocol base class from `eval`
- how the local `eval_cli.py` stays as a tiny wrapper around the external package
