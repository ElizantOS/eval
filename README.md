# eval

Reusable eval framework primitives for target drivers and interactive agent evaluation.

This repository intentionally contains framework code only:

- `elizant_eval.common`
- `elizant_eval.adapters.runner_common`
- `elizant_eval.adapters.responses_driver_base`
- `elizant_eval.adapters.chat_completions_driver_base`

Workspace-owned assets such as:

- target configs
- cases
- runs
- local CLI wrappers
- dashboard wiring

stay in the consumer repository.
