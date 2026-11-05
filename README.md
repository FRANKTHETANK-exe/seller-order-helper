# Seller Order Helper

Seller Order Helper prepares a draft response to a buyer's payment question. It keeps an order's recorded status separate from buyer approval and captured payment. A person reviews the draft; the app cannot send it or change the order.

## Run the offline demo

Use Python 3.10 or newer. No account, credentials, model, or Python packages are needed.

```sh
python3 -B app.py --client stub sandbox-review-ui --port 0
```

On Windows, replace `python3 -B` with `py -3`. Open the printed `http://127.0.0.1:<port>/` address, enter a sample question such as “Has my payment gone through?”, and generate a draft. The default order is **synthetic example data** with status `CREATED`; it is not a current PayPal lookup or evidence of payment. Stub mode uses fixed wording and makes no PayPal or model request. The draft remains internal and unsent. Stop the server with Ctrl-C.

If an action is interrupted, use **Reload session status** to inspect this local session. It does not repeat generation or a PayPal request. Once the server closes, its in-memory review state is gone.

## Optional real Sandbox workflow

[SETUP.md](SETUP.md) describes a separately confirmed, one-shot CLI wizard using your own PayPal Sandbox app and a locally installed IBM Granite model through Ollama. It can create one USD 10 Sandbox order, read that exact order, and generate an internal draft. The resource guard can refuse a run. This release has offline checks; a successful live run of this exact version has **not** been verified. The offline browser demo does not exercise PayPal or Granite.

The prototype supports a narrow `CREATED`, unapproved-order case. It cannot approve, capture, refund, fulfill, or send a message. Order `COMPLETED` alone is not treated as proof of a captured payment; the code requires matching capture evidence. It does not establish shipping or refund facts.

## Develop and inspect

```sh
python3 -B offline_smoke.py
python3 -B -m unittest discover -q
```

The full suite uses temporary loopback sockets and Node.js for its simulated browser-state checks. Provider and model transports are mocked. Test results do not establish live integration, seller benefit, or independent judge-host support. No model weights, provider SDK, credentials, private order receipts, journals, or customer records are bundled.

The project code and documentation are under [MIT](LICENSE). [ATTRIBUTION.md](ATTRIBUTION.md) distinguishes development assistance from runtime AI and lists external tools that are not bundled.
