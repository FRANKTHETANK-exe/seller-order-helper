# PayPal Seller Order Helper

**A buyer's payment question, order facts, and a draft for the seller to review.**

This narrow prototype connects PayPal Sandbox with local IBM Granite. For a synthetic question such as “Has this $10 order been paid?”, it creates one USD 10.00 Sandbox order, immediately reads that exact order, and prepares an internal reply for local human review.

Granite selects from allowed question-intent, phrasing and tone options. Application code composes the wording from validated order facts. The seller reviews the resulting draft and suggested next step in a local browser page. The application cannot approve, capture, refund, fulfill, process a payment, or send a customer message.

[Requirements](#requirements) · [Set up credentials](#first-time-local-credential-setup) · [Run the demo](#run-the-connected-flow) · [Verification](#verification-scope)

## Requirements

- Python 3.10+ on supported macOS or compatible native Linux. If `python3` is missing on macOS, install Python yourself from [the official Python downloads page](https://www.python.org/downloads/macos/), reopen Terminal, and check `python3 --version`. Python standard library only; no pip installation is needed for this flow. Windows real Granite/private-file handling is unsupported, with no automatic stub fallback.
- An existing local Ollama service at `http://127.0.0.1:11434`, with `granite4.2:3b` already cached. The code requires digest `40577dc168a3a9ad34e9a1234e0c2570be86097fa75a236d4574ae985705d3c4`. For manual setup, see [Ollama's official quickstart](https://docs.ollama.com/quickstart); its model command is `ollama pull granite4.2:3b`. No installer, model weights, download, service startup, or warmup is included in this launcher. A changed model tag is refused rather than silently accepted.
- Your own [PayPal Developer Sandbox app](https://developer.paypal.com/dashboard/applications/sandbox) and its Sandbox client ID/secret, plus internet access to PayPal Sandbox. Do not use live credentials.

### Resource checks

- No concurrent local model or heavy workload. Ollama must be running with no model loaded before the flow starts.
- At least 40% memory headroom, 20 GiB free disk and a one-minute load average at most 6.0.
- **macOS:** zero swap.
- **Linux:** at least 4 available CPUs, 4 GiB available memory at startup/cleanup, visible unrestricted cgroup v2 and at most 16 MiB allocated swap. Containers or restricted hosts may be refused.

The runtime checks the actual conditions before proceeding and monitors resources during inference. Before a Sandbox order is created, a short local lock probe checks that the lock is writable, unlinked and available, then releases it. This is a readiness check, not a capacity reservation: the original inference-time lock and resource rechecks still apply, and a later refusal is possible. Do not bypass a refusal.

## First-time local credential setup

Clone or extract the complete repository into a directory you own and can write, then open a private local Terminal in its root (beside `app.py` and `RUN_SANDBOX.py`). The runtime creates its local model lock under `data/`, so a read-only checkout cannot run. Do not record, pipe, or paste credentials into chat, source files or command arguments.

```sh
python3 --version
python3 -B RUN_SANDBOX.py --setup
```

This explicitly invokes `prepare_paypal_sandbox_env.py`. It asks for both values with hidden input and aborts without writing if terminal echo control is unavailable. On success, it creates `~/.secondbrain-secrets/paypal-sandbox.env` as owner-only (0600). It creates the private directory as 0700 only if missing; it never fixes existing permissions or overwrites existing credentials. It makes no PayPal or model request. If your credentials already exist, skip setup.

## Run the connected flow

**This is the real connected path, not an offline sample.** PayPal Sandbox and the local model are used only after the official confirmations.

```sh
python3 -u -B RUN_SANDBOX.py
```

On macOS, `START_SANDBOX.command` is the same entry point. If the OS blocks opening it, use the Terminal command above rather than weakening security settings.

1. Enter a synthetic buyer payment question, for example `Has this $10 order been paid?` Do not enter real customer information.
2. Confirm the Sandbox create by typing exactly `CREATE ONE USD 10 SANDBOX ORDER` only if you authorize it.
3. Type exactly `RUN` only after checking that this computer is idle. This is the official idle-workload attestation; the launcher does not answer it for you.
4. The runtime checks resources and the model, performs its one-shot flow, then prints a local `http://127.0.0.1:<port>/` review address. Open it on the same trusted computer. Review the exact returned draft. Closing the server with Ctrl-C does not send or capture anything.

The launcher executes only the existing official command, with fixed owner-private destinations:

```sh
python3 -u -B judge_sandbox_flow.py \
  --run --standalone --review-ui \
  --reviewer-order-pin "$HOME/.secondbrain-secrets/paypal-judge-sandbox-order.json" \
  --journal "$HOME/.secondbrain-secrets/paypal-judge-flow-once.json"
```

Do not substitute new pin/journal paths to repeat a run. These paths are fixed across repository checkouts, extractions and reruns. The launcher stops if either already exists, including a dangling link; it also protects the old documented `judge-order.json` / `judge-attempt.json` aliases. It never deletes or resets them. The official flow atomically creates a one-shot journal, keeps the create's idempotency key and refuses a second attempt. If a request fails, times out, is interrupted or has an uncertain result, preserve the private files and have the previous outcome reviewed before any separately authorized new attempt. The launcher deliberately offers no retry/reset option. Closing or expiring the review page is not permission to recreate the order.

Optional local-only path/permission check:

```sh
python3 -B RUN_SANDBOX.py --check
```

This does not read credential values, contact PayPal/Ollama, run inference, or prove model/resource readiness. A missing credential file means use the hidden-input setup. A prior pin/journal means stop and inspect the previous attempt. A resource/model refusal means resolve the reported condition through normal local setup; do not remove attempt records or bypass guards.

## Verification scope

The published predecessor used the R2 runtime from the verified native-Ubuntu connected run: OAuth 200, exactly one new Sandbox order 201, immediate GET 200, exactly one actual local Granite completion and confirmed model cleanup. The same result's local UI returned HTTP 200; this is not a claim of an independent visual check of that connected result. That source also previously passed 306 offline tests plus smoke on macOS and Ubuntu. The included synthetic fixtures/developer tests are preserved source, not the default execution path.

Startup readiness and failure guidance include a pre-order lock probe, bounded local preflight codes/remedies, fail-closed hidden-input handling, and a missing-Python launcher message. The seller UI, provider flow, inference request/output/cleanup and resource thresholds are unchanged. Focused offline fixture tests cover these startup changes. The launcher supports this flat repository layout and the older nested kit layout; both dispatch to the official judge flow. Its fixed dispatch, one-shot records and confirmations are unchanged.

`DEVELOPMENT.md` contains historical offline-first development instructions and verification records. This README is the current connected entry point. Do not use historical alternate filenames to bypass the fixed one-shot record. No credentials, private pins/journals/receipts, screenshots, performance logs or model weights are included.
