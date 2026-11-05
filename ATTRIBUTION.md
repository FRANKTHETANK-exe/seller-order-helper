# Attribution and external components

OpenAI Codex/ChatGPT assisted project implementation and tests. Claude reviewed earlier private versions. A separate Qwen Coder action loop supported a limited Windows offline check. These development tools are distinct from the product's runtime model.

The optional runtime uses IBM Granite 4.2 3B through Ollama to select constrained question intent and wording IDs. Application code composes the factual sentence. The offline stub uses fixed selection and runs no model.

Python, Ollama, Granite model artifacts, and PayPal services are **not bundled**. This repository contains no provider SDK, model weights, runtime installer, PayPal marks, or third-party media. Their separate terms apply to anyone obtaining or using them. References: [Python license](https://docs.python.org/3/license.html), [Ollama license](https://github.com/ollama/ollama/blob/main/LICENSE), [IBM Granite model card](https://huggingface.co/ibm-granite/granite-4.2-3b).

The bundled order examples are synthetic. No private receipt, customer record, or credential is distributed.
