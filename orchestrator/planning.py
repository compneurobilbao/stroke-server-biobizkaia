"""Run plans: which slots a run executes, with which implementation, and why
(card F1-19; design docs/design/run-plans.md 3.2-3.4, contract presets.md 4-5).

``plan(pipeline, input_set, request, enablement) -> Plan | PlanRefused`` is a pure
function: no database, no files, no clock. ``create_run``
(``orchestrator.resolution``) reads the deployment's settings into an
``Enablement``, calls it, and freezes the result in ``run_slots``; the plan preview
of the API (card F1-21) can call it with the same arguments and get the same plan.

The closure itself (rules R0 and steps 1-5 of presets.md 4) is
``contracts.validate.plan_closure``, the reference implementation of the contract.
This module adds what the contract leaves to the orchestrator:

- the plan request: none (the full plan, rule R0), a preset, or free targets;
  request-level refusals (``preset_and_targets``, ``preset_not_found``,
  ``preset_not_enabled``, ``preset_input_set_mismatch``,
  ``implementation_fixed_by_preset``);
- the implementation choice per slot (pipeline.md 6 item 3 with the preset cases of
  presets.md 5). The closure receives, per slot, exactly the implementation this
  rule selects (or none), so that ``enabled`` means "what the choice rule can
  select" (presets.md 4.3) and R0 refuses and skips exactly as run creation did
  before plans existed;
- for the full plan, the refusal code and slot of that earlier run creation
  (``implementation_not_enabled``, ``no_implementation``, ``slot_unsatisfiable``,
  first failing slot in dependency order), so that clients see no change;
- the check that a step reading ``run.provenance`` can read the record the plan
  needs (``step_contract_too_old``, see ``provenance_consumer_reasons``).

Every refusal carries every reason found, not only the first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from contracts import validate as contract
from contracts.validate import PlanReason
from orchestrator.catalog import PresetDefinition, PresetRegistry
from orchestrator.pipeline import PipelineDefinition, sources_of

FULL, PRESET, FREE = "full", "preset", "free"
SELECTED, SKIPPED, UNAVAILABLE = "selected", "skipped", "unavailable"
# Values of a run record that only contract 1.2 can express (provenance.md 3.1 rule 3).
NOT_REQUESTED = "not_requested"
CHOSEN_BY_PRESET = "preset"
_SLOT_PORT = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\.[a-z][a-z0-9]*(_[a-z0-9]+)*$")


@dataclass(frozen=True)
class PresetRef:
    """A preset named in a run request; without ``version``, the highest version
    attached to the pipeline version (run-plans.md 2.3)."""

    id: str
    version: str | None = None


@dataclass(frozen=True)
class TargetRequest:
    """Free-mode targets as the user gave them (slot, ``slot.port`` or artefact type)."""

    required: tuple[str, ...] = ()
    if_available: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanRequest:
    """What is asked for: a preset, free targets, or neither (the full plan); plus the
    user's implementation choice per slot."""

    preset: PresetDefinition | None = None
    targets: TargetRequest | None = None
    implementations: Mapping[str, str] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        if self.preset is not None:
            return PRESET
        return FULL if self.targets is None else FREE


@dataclass(frozen=True)
class Enablement:
    """The deployment state a plan depends on, for one pipeline id:
    ``enabled[slot]`` the enabled implementation ids, ``defaults[slot]`` those that
    are the deployment default (``slot_implementation_settings``), ``presets`` the
    enabled preset ids (``preset_settings``). Ids the pipeline version does not allow
    are ignored."""

    enabled: Mapping[str, frozenset[str]] = field(default_factory=dict)
    defaults: Mapping[str, frozenset[str]] = field(default_factory=dict)
    presets: frozenset[str] = frozenset()

    @classmethod
    def from_settings(
        cls, implementations: Iterable[Any], presets: Iterable[Any] = ()
    ) -> Enablement:
        """From rows with ``slot``, ``implementation_id``, ``enabled``, ``is_default``
        and rows with ``preset_id``, ``enabled``."""
        enabled: dict[str, set[str]] = {}
        defaults: dict[str, set[str]] = {}
        for row in implementations:
            if row.enabled:
                enabled.setdefault(row.slot, set()).add(row.implementation_id)
                if row.is_default:
                    defaults.setdefault(row.slot, set()).add(row.implementation_id)
        return cls(
            {k: frozenset(v) for k, v in enabled.items()},
            {k: frozenset(v) for k, v in defaults.items()},
            frozenset(p.preset_id for p in presets if p.enabled),
        )


@dataclass(frozen=True)
class SlotDecision:
    """One slot of a plan: ``selected`` with an implementation and ``chosen_by``, or
    ``skipped``/``unavailable`` with a reason; ``plan_role`` as in presets.md 4.1."""

    slot: str
    status: str
    reason: str | None = None
    plan_role: str | None = None
    implementation_id: str | None = None
    chosen_by: str | None = None


@dataclass(frozen=True)
class Plan:
    """A feasible plan: one decision per pipeline slot, in pipeline order."""

    mode: str
    input_set: str
    preset: PresetDefinition | None
    targets: (
        dict  # resolved request {required: [slot.port], if_available: [...]}; {} for R0
    )
    slots: tuple[SlotDecision, ...]

    def slot(self, name: str) -> SlotDecision:
        return next(s for s in self.slots if s.slot == name)

    @property
    def selected(self) -> list[SlotDecision]:
        return [s for s in self.slots if s.status == SELECTED]

    @property
    def needs_contract_1_2(self) -> bool:
        """True if the run record of this plan has values that a record declaring
        contract 1.0 or 1.1 cannot carry (slots outside the plan, preset choices)."""
        return any(
            s.reason == NOT_REQUESTED or s.chosen_by == CHOSEN_BY_PRESET
            for s in self.slots
        )


@dataclass(frozen=True)
class PlanRefused:
    """A refused plan request with every reason found. ``slots`` holds the decisions
    of the closure when it was computed (used to keep the order of the full-plan
    refusal, see ``orchestrator.resolution.create_run``)."""

    mode: str
    reasons: tuple[PlanReason, ...]
    slots: tuple[SlotDecision, ...] = ()

    @property
    def code(self) -> str:
        return self.reasons[0].code

    @property
    def slot(self) -> str | None:
        return self.reasons[0].slot

    @property
    def message(self) -> str:
        return " ".join(r.message for r in self.reasons)


def reason_dict(reason: PlanReason) -> dict[str, str]:
    """``{code, message, target, slot, port, required_by}`` without empty fields
    (the ``reasons[]`` items of run-plans.md 3.3)."""
    return {
        k: v
        for k in ("code", "message", "target", "slot", "port", "required_by")
        if (v := getattr(reason, k)) is not None
    }


# ------------------------------------------------------------------------ request


def resolve_request(
    pipeline: PipelineDefinition,
    *,
    preset: PresetRef | None = None,
    targets: TargetRequest | None = None,
    implementations: Mapping[str, str] | None = None,
    presets: PresetRegistry | None = None,
) -> PlanRequest | PlanRefused:
    """Turn a run request into a ``PlanRequest``: look the preset up among those
    attached to this pipeline version (``preset_not_found``), refuse a preset together
    with targets (``preset_and_targets``) and targets that are not strings
    (``unknown_target``)."""
    implementations = dict(implementations or {})
    if preset is not None and targets is not None:
        return PlanRefused(
            PRESET,
            (
                PlanReason(
                    "preset_and_targets",
                    "A run request names either a preset or targets, not both.",
                ),
            ),
        )
    if preset is not None:
        found = (
            presets.get(pipeline.id, pipeline.version, preset.id, preset.version)
            if presets is not None
            else None
        )
        if found is None:
            version = f" version {preset.version}" if preset.version else ""
            return PlanRefused(
                PRESET,
                (
                    PlanReason(
                        "preset_not_found",
                        f"The preset '{preset.id}'{version} is not available for the "
                        f"pipeline {pipeline.id} {pipeline.version}.",
                        target=preset.id,
                    ),
                ),
            )
        return PlanRequest(preset=found, implementations=implementations)
    if targets is not None:
        bad = [
            PlanReason(
                "unknown_target",
                "A target must be a slot, a produced port or an artefact type name.",
            )
            for t in (*targets.required, *targets.if_available)
            if not isinstance(t, str) or not t
        ]
        if bad:
            return PlanRefused(FREE, (bad[0],))
        return PlanRequest(
            targets=TargetRequest(tuple(targets.required), tuple(targets.if_available)),
            implementations=implementations,
        )
    return PlanRequest(implementations=implementations)


# ------------------------------------------------------------------------- choice


def choose_implementation(
    slot: Mapping[str, Any],
    enablement: Enablement,
    requested: str | None,
    preset_choice: tuple[str, str] | None = None,
) -> tuple[str, str] | None | PlanReason:
    """``(implementation id, chosen_by)`` by pipeline.md 6 item 3 and presets.md 5, or
    None when no implementation can be chosen, or the reason why the user's choice
    is refused (``implementation_not_enabled``).

    Order: a preset's ``fixed`` choice; the user's choice; a preset's ``default``; the
    deployment default; the pipeline default. Each must be allowed and enabled; a
    preset never enables an implementation. A fixed choice that is not enabled
    leaves the slot without an implementation (no other choice replaces it).
    """
    name = slot["name"]
    allowed = [a["id"] for a in slot["implementations"]["allowed"]]
    on = enablement.enabled.get(name, frozenset())
    enabled = [i for i in allowed if i in on]
    if preset_choice is not None and preset_choice[0] == "fixed":
        return (
            (preset_choice[1], CHOSEN_BY_PRESET)
            if preset_choice[1] in enabled
            else None
        )
    if requested is not None:
        if requested not in enabled:
            return PlanReason(
                "implementation_not_enabled",
                f"implementation '{requested}' is not enabled for slot '{name}'",
                slot=name,
            )
        return requested, "user"
    if preset_choice is not None and preset_choice[1] in enabled:
        return preset_choice[1], CHOSEN_BY_PRESET
    defaults = [i for i in enabled if i in enablement.defaults.get(name, frozenset())]
    if defaults:
        return defaults[0], "deployment_default"
    pipeline_default = slot["implementations"].get("default")
    if pipeline_default in enabled:
        return pipeline_default, "pipeline_default"
    return None


# --------------------------------------------------------------------------- plan


def plan(
    pipeline: PipelineDefinition,
    input_set: str,
    request: PlanRequest,
    enablement: Enablement,
) -> Plan | PlanRefused:
    """The plan of ``request`` for a run of ``pipeline`` with ``input_set`` in a
    deployment with ``enablement`` (see the module docstring). Pure."""
    mode = request.mode
    preset = request.preset
    requested = dict(request.implementations)
    reasons: list[PlanReason] = []

    required: list[str] | None = None
    if_available: list[str] | None = None
    if preset is not None:
        if preset.id not in enablement.presets:
            reasons.append(
                PlanReason(
                    "preset_not_enabled",
                    f"The preset '{preset.id}' is not enabled in this deployment.",
                    target=preset.id,
                )
            )
        if input_set not in preset.input_sets:
            reasons.append(
                PlanReason(
                    "preset_input_set_mismatch",
                    f"The preset '{preset.id}' does not accept the input set "
                    f"'{input_set}' (it accepts {', '.join(preset.input_sets)}).",
                    target=preset.id,
                )
            )
        required, if_available = preset.required, preset.if_available
    elif request.targets is not None:
        required = list(request.targets.required)
        if_available = list(request.targets.if_available)

    choices: dict[str, tuple[str, str]] = {}
    request_errors: dict[str, PlanReason] = {}
    for slot in pipeline.slots:
        name = slot["name"]
        preset_choice = preset.choice(name) if preset is not None else None
        user = requested.get(name)
        if (
            preset_choice is not None
            and preset_choice[0] == "fixed"
            and user is not None
            and user != preset_choice[1]
        ):
            reasons.append(
                PlanReason(
                    "implementation_fixed_by_preset",
                    f"The preset '{preset.id}' fixes the implementation of the slot "
                    f"'{name}' to '{preset_choice[1]}'; '{user}' cannot be chosen.",
                    slot=name,
                )
            )
        result = choose_implementation(slot, enablement, user, preset_choice)
        if isinstance(result, PlanReason):
            request_errors[name] = result
        elif result is not None:
            choices[name] = result

    if any(r.code == "preset_input_set_mismatch" for r in reasons):
        # The preset was validated for other input sets only; a closure under this
        # one would report reasons the user cannot act on.
        return PlanRefused(mode, tuple(reasons))

    static = contract.plan_closure(
        dict(pipeline.data),
        input_set,
        required,
        if_available,
        {name: [impl] for name, (impl, _by) in choices.items()},
    )
    decisions = []
    replaced: set[str] = set()
    for s in static.slots:
        if s.status == "planned":
            impl, chosen_by = choices[s.slot]
            decisions.append(
                SlotDecision(s.slot, SELECTED, None, s.plan_role, impl, chosen_by)
            )
            continue
        decisions.append(SlotDecision(s.slot, s.status, s.reason, s.plan_role))
        if s.slot in request_errors and s.status == UNAVAILABLE:
            # The slot was reached: the user's choice is what is missing.
            replaced.add(s.slot)
    for r in static.reasons:
        if r.code == "no_implementation" and r.slot in replaced:
            reasons.append(
                PlanReason(
                    request_errors[r.slot].code,
                    request_errors[r.slot].message,
                    target=r.target,
                    slot=r.slot,
                    required_by=r.required_by,
                )
            )
            replaced.discard(r.slot)
        else:
            reasons.append(r)
    # Reached if-available slots whose requested implementation is not enabled: run
    # creation refused them before plans existed, and still does.
    reasons.extend(request_errors[name] for name in sorted(replaced))

    if reasons:
        if mode == FULL:
            reasons = _full_plan_order(pipeline, decisions, request_errors, reasons)
        return PlanRefused(mode, tuple(reasons), tuple(decisions))
    return Plan(mode, input_set, preset, dict(static.targets), tuple(decisions))


def _full_plan_order(
    pipeline: PipelineDefinition,
    decisions: list[SlotDecision],
    request_errors: Mapping[str, PlanReason],
    reasons: list[PlanReason],
) -> list[PlanReason]:
    """For the full plan, put first the refusal that run creation raised before plans
    existed: at the first slot, in dependency order, that is reached with a refused
    user choice (``implementation_not_enabled``), or that is not optional and cannot
    run (``no_implementation`` without an implementation, ``slot_unsatisfiable`` when
    an upstream slot is missing). The statuses are the same by rule R0."""
    by_name = {d.slot: d for d in decisions}
    for name in pipeline.topological_order:
        d = by_name[name]
        optional = bool(pipeline.slot(name).get("optional", False))
        if name in request_errors and d.status == UNAVAILABLE:
            first = request_errors[name]
        elif optional or d.status == SELECTED or d.reason == "condition_false":
            continue
        elif d.status == UNAVAILABLE:
            first = PlanReason(
                "no_implementation",
                f"slot '{name}' has no enabled implementation",
                slot=name,
            )
        else:
            first = PlanReason(
                "slot_unsatisfiable",
                f"slot '{name}' cannot run: an upstream slot is not available",
                slot=name,
            )
        rest = [r for r in reasons if (r.code, r.slot) != (first.code, first.slot)]
        return [first, *rest]
    return reasons


# ------------------------------------------------------------- run record readers


def provenance_consumer_reasons(
    pipeline: PipelineDefinition,
    plan_: Plan,
    manifests: Mapping[str, Mapping[str, Any]],
) -> list[PlanReason]:
    """Selected slots whose step reads ``run.provenance`` but declares a contract older
    than 1.2, when the plan's run record needs 1.2 (``Plan.needs_contract_1_2``).

    A step receives the run record in the form of the contract its manifest declares
    (``orchestrator.provenance.run_provenance_for_step``; versioning.md 2 rule 4: a
    component of contract 1.Y rejects documents of 1.Z with Z > Y). For older steps
    the plan fields are left out, which loses nothing they can read; but slots outside
    the plan (``not_requested``) and preset choices (``chosen_by: preset``) have no
    1.0 or 1.1 form, so such a plan is refused (``step_contract_too_old``) until
    the step has a version that declares contract 1.2 (presets.md 4.3).
    ``manifests`` maps selected slot names to their step manifests."""
    if not plan_.needs_contract_1_2:
        return []
    out = []
    for decision in plan_.selected:
        manifest = manifests.get(decision.slot)
        if manifest is None:
            continue
        declared = {i["name"] for i in manifest.get("inputs", [])}
        reads = any(
            c["port"] in declared and any(src.kind == "run" for src in sources_of(c))
            for c in pipeline.slot(decision.slot)["consumes"]
        )
        version = manifest.get("contract_version")
        if reads and contract.contract_minor(version) < contract.RULES_1_2:
            out.append(
                PlanReason(
                    "step_contract_too_old",
                    f"The step '{manifest['name']} {manifest['version']}' of the slot "
                    f"'{decision.slot}' declares contract {version} and cannot read the "
                    f"run record of this plan, which needs contract 1.2 (slots outside "
                    f"the plan or implementations chosen by the preset); a version of "
                    f"the step that declares contract 1.2 is needed.",
                    slot=decision.slot,
                )
            )
    return out


def targets_errors(targets: Any) -> list[str]:
    """Shape of ``pipeline_runs.targets`` before it is written: ``{}`` or
    ``{required: [slot.port], if_available: [slot.port]}`` with unique ports (the
    ``plan.targets`` of the provenance schema; the database does not check it)."""
    if not isinstance(targets, dict):
        return ["targets must be an object"]
    errs = [
        f"unknown key '{k}'" for k in targets if k not in ("required", "if_available")
    ]
    for key, values in targets.items():
        if not isinstance(values, list):
            errs.append(f"'{key}' must be a list")
            continue
        if len(set(values)) != len(values):
            errs.append(f"'{key}' has duplicates")
        errs += [
            f"'{key}': '{v}' is not a slot.port"
            for v in values
            if not isinstance(v, str) or not _SLOT_PORT.match(v)
        ]
    return errs
