# Optional one-shot PayPal Sandbox and Granite setup

The offline demo in [README.md](README.md) needs no credentials or model. This optional CLI path creates one USD 10 **Sandbox** order after explicit confirmation, reads its returned ID, and may ask a local model to select constrained wording for an internal draft. It offers no approve, capture, refund, fulfillment, or message-send action.

## Requirements

- macOS and Python 3.10 or newer.
- Your own PayPal Developer Sandbox REST app.
- Ollama already running at `127.0.0.1:11434`, with `granite4.2:3b` installed at digest `40577dc168a3a9ad34e9a1234e0c2570be86097fa75a236d4574ae985705d3c4`.
- A quiet machine that passes the built-in guard: at least 40% free memory pressure, zero swap, one-minute load at most 6, at least 20 GiB free disk, and no loaded model or other queued AI work. The app refuses to run if these checks fail; it does not install a model or waive a guard. Linux live admission is unsupported, and Windows real Granite fails closed.

## Local credentials

Run the helper in your own terminal. It requests credentials through hidden input and writes an owner-only file; do not put secrets in command arguments, chat, or the repository.

```sh
python3 -B -c 'from pathlib import Path; (Path.home()/".secondbrain-secrets").mkdir(mode=0o700, exist_ok=True)'
python3 -B prepare_paypal_sandbox_env.py
```

The helper creates `~/.secondbrain-secrets/paypal-sandbox.env` with mode `0600`, containing `PAYPAL_SANDBOX_CLIENT_ID` and `PAYPAL_SANDBOX_CLIENT_SECRET`. It refuses to overwrite an existing file or use an insecure or linked directory.

## One chosen attempt

Choose new, absent paths for the order pin and attempt journal:

```sh
python3 -u -B judge_sandbox_flow.py --run --standalone \
  --reviewer-order-pin "$HOME/.secondbrain-secrets/my-judge-order.json" \
  --journal "$HOME/.secondbrain-secrets/my-judge-attempt.json"
```

The wizard asks for a synthetic buyer question, confirmation to create one USD 10 Sandbox order, and an idle-workload attestation. A successful path makes at most one OAuth request, one idempotent create, one immediate GET for the returned ID, and one Granite draft attempt. The draft stays pending human review and is not sent. Preserve the private pin and journal, especially if a create result is uncertain. Do not choose new paths merely to bypass a stopped or uncertain attempt.

The live CLI path is distinct from the offline browser demo. Current-version live behavior and a separate judge computer have not been verified. Do not share credential files, order pins, journals, or raw provider responses.
