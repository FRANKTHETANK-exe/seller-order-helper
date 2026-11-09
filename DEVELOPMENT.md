# Seller Order Helper

This application prepares an internal draft for a seller's payment question. It distinguishes an order's status from approval and captured payment. It cannot send a buyer message, capture or refund a payment, or fulfill an order. A human must review the draft and check current transaction facts independently.

## Quick start: macOS, offline

You need Python 3.10 or newer. No pip packages, PayPal account, model, or internet connection are needed for this path. In Terminal, go to the extracted `seller-order-helper` directory and run:

```sh
python3 --version
python3 -B offline_smoke.py
python3 -B app.py --client stub sandbox-review-ui --port 0
```

Open the printed `http://127.0.0.1:<port>/` address on the same computer. Enter a sample buyer question, then choose **Generate response**. The displayed $10 order is marked as sample data. Stub mode uses fixed wording: it makes no PayPal request and runs no model. The **Message response** panel shows a local preview. The page has no message-send or review-acknowledgment action. Stop the server with Control-C.

For a terminal-only preview instead:

```sh
python3 -B app.py --client stub sandbox-ai-review
```

Press Enter at the review prompt to leave the draft pending. This is a functional offline example, not proof of a PayPal or Granite integration.

## Optional: real PayPal Sandbox and local Granite

The source includes a separate one-shot flow that can create one USD 10 Sandbox order, read it once, run local Granite, and open the same draft for human review. It uses no live funds and cannot approve, capture, refund, fulfill, or send. This flow requires the judge's own PayPal Developer Sandbox app, a compatible local Ollama setup with `granite4.2:3b` at the digest checked by the program, a quiet supported macOS computer or dedicated Ubuntu host that passes its resource checks, and explicit consent to make those Sandbox requests. The package does not include credentials, an installer, or model weights. Never put credentials in command arguments, source files, chat, or recordings.

Install [Ollama for your platform](https://docs.ollama.com/quickstart), start its local service and download the model with `ollama pull granite4.2:3b`. The download requires internet access and is approximately 2.2 GB. From the project directory, check local resources and the cached model without generating text or contacting PayPal:

```sh
python3 -B -c 'from local_model import GraniteClient; print(GraniteClient(standalone_demo=True).preflight())'
```

The preflight checks the pinned model and resource guards; it does not authorize inference or check other work queues. If it refuses, read the diagnostic and do not bypass the guard. The credential helper accepts hidden input and creates an owner-only file. Do not overwrite existing credentials or prior attempt records. On a local private terminal, after obtaining a PayPal Sandbox client ID and secret and all necessary permissions, prepare the owner-only directory if needed. The helper checks its ownership and permissions; if an existing directory fails the check, stop and investigate rather than changing its permissions blindly. Choose fresh unused paths under that directory and run:

```sh
python3 -B -c 'from pathlib import Path; (Path.home()/".secondbrain-secrets").mkdir(mode=0o700, exist_ok=True)'
python3 -B prepare_paypal_sandbox_env.py
python3 -u -B judge_sandbox_flow.py --run --standalone --review-ui \
  --reviewer-order-pin "$HOME/.secondbrain-secrets/judge-order.json" \
  --journal "$HOME/.secondbrain-secrets/judge-attempt.json"
```

Follow the prompts, including the exact Sandbox order confirmation and idle-workload attestation. The files must be unused; the program refuses to overwrite an attempt. If a request fails or its result is uncertain, preserve the journal and do not rerun the same create blindly. Open the printed loopback URL only on the same trusted computer. Closing the server ends the local session; a human review is not a buyer message or PayPal approval.

## Verification and platform limits

This isolated successor extends the 71-file curated Linux cold-start source with a bounded Linux swap allowance and a simpler seller screen. It is not the exact package used for the earlier connected results. The original package and frozen fallback are preserved separately.

The preceding complete 106-file candidate passed 284 offline tests on macOS and native Ubuntu. It also completed one actual native Ubuntu read of an existing PayPal Sandbox order and one local Granite inference in 80.25 seconds, including 70.54 seconds of cold model loading. Cleanup confirmed no loaded model, and swap stayed at zero. The prior curated subset passed its 281 applicable offline tests on both hosts, but its native browser draft attempt stopped at the old zero-swap check after the order read. Those results do not verify this successor's connected behavior.

The current seller screen shows the order, customer question and message response without internal task-system notices. It does not offer a send or review-acknowledgment control. Checking status shows a compact expired-order label when a live read is too old; the server always refuses generation from expired facts. A person must still inspect the response and current payment evidence before using it elsewhere.

Linux standalone admission now permits at most **16 MiB of total allocated swap**, while retaining the 4 GiB startup/post-cleanup available-memory requirement, continuous 40% available-memory threshold, model identity and health checks, and existing CPU, cgroup, load and disk checks. This is a bounded engineering allowance, not a measurement or assurance of paging pressure. Missing, malformed or nonfinite resource measurements stop admission. macOS and default controller mode still require zero swap; the existing separately selected stable-baseline path is unchanged. See [the resource policy](LINUX_SMALL_SWAP_POLICY.md).

The standalone Linux profile allows up to 180 seconds for its one model request, including cold loading and inference. macOS and controller mode retain 60-second budgets. Two-second resource monitoring, one-use requests, cleanup and no automatic retry remain enforced. There is no warmup inference or automatic model download.

Offline checks use synthetic fixtures and mocked provider/model transports. They do not establish native rendering or actual inference success for this successor. Native connected verification, a complete Linux demonstration and native Windows execution remain separate gates. Windows private Sandbox files and real Granite remain unsupported; the Windows route is the synthetic stub example only.

## Development checks

```sh
python3 -B -m unittest discover -q
```

The developer suite uses temporary loopback sockets and Node.js for browser-state checks; it mocks provider and model transports. A result with skipped tests is not a full suite pass. See `ATTRIBUTION.md` and `LICENSE` for component and license information. The included examples are synthetic.
