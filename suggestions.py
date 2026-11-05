"""Bounded wording selection through an injected local-model client.

This module performs no network or inference operation itself. Tests inject
stubs. The client can select approved wording IDs, never supply reply text,
change order facts, approve a draft, or send a message.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Protocol

from facts import CATEGORIES, OrderFacts

LABEL = "SIMULATED AI SUGGESTION"
MAX_PROMPT_BYTES = 4096
MAX_MODEL_OUTPUT_BYTES = 1024
MAX_REPLY_BYTES = 2048
PHRASE_IDS = ("status_first", "item_first")
STYLE_IDS = ("concise", "friendly")


class SuggestionError(ValueError):
    """A client result cannot safely produce a grounded suggestion."""


class StaleFactsError(SuggestionError):
    """The facts changed after the suggestion was generated."""


class LocalModelClient(Protocol):
    def __call__(self, prompt: str) -> str:
        """Return the requested JSON selection, with no extra text."""


def _facts_snapshot(facts: OrderFacts) -> OrderFacts:
    if type(facts) is not OrderFacts:
        raise SuggestionError("Expected exact synthetic OrderFacts")
    try:
        # Revalidate even a frozen object changed by outside reflection.
        return OrderFacts(**asdict(facts))
    except (TypeError, ValueError) as error:
        raise SuggestionError("Order facts are not valid for this simulation") from error


def fingerprint_order_facts(facts: OrderFacts) -> str:
    """Digest every original facts field, not merely its reply category."""
    snapshot = _facts_snapshot(facts)
    encoded = json.dumps(asdict(snapshot), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _display(body: str, reviewed: bool) -> str:
    review = ("Human review completed; no message has been sent." if reviewed else
              "Human review required; no message has been sent.")
    text = f"{LABEL}\n{review}\n\n{body}"
    if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        raise SuggestionError("Composed reply exceeds its size bound")
    return text


@dataclass(frozen=True)
class ApprovedReply:
    order_id: str
    category: str
    phrase_id: str
    style_id: str
    facts_fingerprint: str
    reply_text: str
    label: str = field(default=LABEL, init=False)
    human_review_required: bool = field(default=False, init=False)
    human_review_completed: bool = field(default=True, init=False)
    simulated: bool = field(default=True, init=False)
    model_execution_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ReplySuggestion:
    order_id: str
    category: str
    phrase_id: str
    style_id: str
    facts_fingerprint: str
    reply_text: str
    _approved_body: str = field(repr=False)
    label: str = field(default=LABEL, init=False)
    human_review_required: bool = field(default=True, init=False)
    human_review_completed: bool = field(default=False, init=False)
    simulated: bool = field(default=True, init=False)
    model_execution_verified: bool = field(default=False, init=False)
    wording_source: str = field(default="injected_client_approved_id_selection", init=False)

    def approve(self, current_facts: OrderFacts) -> ApprovedReply:
        """Explicit human review action; no sending or payment operation exists."""
        if fingerprint_order_facts(current_facts) != self.facts_fingerprint:
            raise StaleFactsError("Order facts changed; generate and review a fresh suggestion")
        snapshot = _facts_snapshot(current_facts)
        choice = _selection(json.dumps({"order_id": self.order_id, "category": self.category,
                                        "phrase_id": self.phrase_id, "style_id": self.style_id}), snapshot)
        body = _compose_body(snapshot, choice)
        if self._approved_body != body or self.reply_text != _display(body, reviewed=False):
            raise SuggestionError("Suggestion content changed; generate and review a fresh suggestion")
        return ApprovedReply(self.order_id, self.category, self.phrase_id,
                             self.style_id, self.facts_fingerprint,
                             _display(body, reviewed=True))


_BODY_TEMPLATES = {
    "payment_pending": {
        "status_first": "Payment is not confirmed for simulated order {order_id}, for one {product} at {currency} {amount}.",
        "item_first": "Your simulated order {order_id} for one {product} ({currency} {amount}) is awaiting payment confirmation.",
    },
    "payment_confirmed": {
        "status_first": "The simulated payment of {currency} {amount} is recorded as completed for order {order_id}, for one {product}.",
        "item_first": "Your simulated order {order_id} for one {product} has a completed payment record for {currency} {amount}.",
    },
    "order_voided": {
        "status_first": "Simulated order {order_id} is marked voided. It was for one {product} at {currency} {amount}.",
        "item_first": "Your simulated order {order_id} for one {product} ({currency} {amount}) is marked voided.",
    },
}


def _prompt(facts: OrderFacts) -> str:
    context = facts.model_context()
    request = {
        "instruction": "Choose approved wording IDs for this synthetic order reply. Return exactly one JSON object matching selection_schema. Do not write reply text or add facts. Category and order_id must match the supplied facts. Human review is required; you cannot approve or send a reply.",
        "facts": context,
        "selection_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["order_id", "category", "phrase_id", "style_id"],
            "properties": {
                "order_id": {"type": "string", "const": context["order_id"]},
                "category": {"type": "string", "const": context["category"]},
                "phrase_id": {"type": "string", "enum": list(PHRASE_IDS)},
                "style_id": {"type": "string", "enum": list(STYLE_IDS)},
            },
        },
        "approved_choices": {
            "status_first": "State the order/payment status first, then the item.",
            "item_first": "Introduce the item/order first, then its status.",
            "concise": "Use the approved factual sentence only.",
            "friendly": "Prepend the fixed greeting: Thanks for checking in.",
        },
    }
    prompt = json.dumps(request, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=True, allow_nan=False)
    if len(prompt.encode("ascii")) > MAX_PROMPT_BYTES:
        raise SuggestionError("Model prompt exceeds its size bound")
    return prompt


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SuggestionError("Duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_constant(value):
    raise SuggestionError("Nonfinite JSON values are not allowed")


def _selection(raw: str, facts: OrderFacts) -> dict:
    if type(raw) is not str:
        raise SuggestionError("Model selection must be JSON text")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise SuggestionError("Model selection is not valid UTF-8 text") from error
    if not 1 <= size <= MAX_MODEL_OUTPUT_BYTES:
        raise SuggestionError("Model selection exceeds its size bound")
    try:
        choice = json.loads(raw, object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise SuggestionError("Model selection is malformed or violates its schema") from error
    if (type(choice) is not dict or set(choice) != {"order_id", "category", "phrase_id", "style_id"}
            or any(type(value) is not str for value in choice.values())):
        raise SuggestionError("Model selection must contain exactly the four string fields")
    if choice["order_id"] != facts.order_id:
        raise SuggestionError("Model selection refers to a different order")
    if choice["category"] not in CATEGORIES or choice["category"] != facts.category:
        raise SuggestionError("Model selection conflicts with the order facts")
    if choice["phrase_id"] not in PHRASE_IDS or choice["style_id"] not in STYLE_IDS:
        raise SuggestionError("Model selection contains an unapproved wording ID")
    return choice


def generate_suggestion(facts: OrderFacts, client: LocalModelClient) -> ReplySuggestion:
    """Request bounded phrase/style selection; compose factual text locally.

    A successful stub response demonstrates the model-assistance interface,
    not proof that a real model executed. That proof belongs to the caller's
    separately reviewed integration and is never inferred here.
    """
    snapshot = _facts_snapshot(facts)
    original_fingerprint = fingerprint_order_facts(snapshot)
    prompt = _prompt(snapshot)
    if not callable(client):
        raise SuggestionError("An injected callable model client is required")
    try:
        raw = client(prompt)
    except Exception:
        # Do not expose client output, transport details or exception messages.
        raise SuggestionError("Injected model client failed") from None
    choice = _selection(raw, snapshot)
    if fingerprint_order_facts(facts) != original_fingerprint:
        raise StaleFactsError("Order facts changed during selection; request a fresh suggestion")
    body = _compose_body(snapshot, choice)
    return ReplySuggestion(snapshot.order_id, snapshot.category,
                           choice["phrase_id"], choice["style_id"],
                           original_fingerprint, _display(body, reviewed=False), body)


def _compose_body(facts: OrderFacts, choice: dict) -> str:
    context = facts.model_context()
    body = _BODY_TEMPLATES[facts.category][choice["phrase_id"]].format(
        order_id=context["order_id"], product=context["product"],
        currency=context["currency"], amount=context["amount"])
    if choice["style_id"] == "friendly":
        body = "Thanks for checking in. " + body
    return body
