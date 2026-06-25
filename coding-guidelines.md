# Object Calisthenics — Pragmatic Rules

Use these as guidelines, not constraints.

---

## 1. Control Complexity, Not Indentation

* Avoid deep nesting and high cyclomatic complexity.
* Extract logic when it improves readability, not just to satisfy a rule.

**Heuristic:** If you need to scroll or mentally simulate branches, refactor.

---

## 2. Prefer Straight-Line Flow

* Use early returns to reduce nesting when it clarifies intent.
* Keep simple `if/else` when it reads better.

**Goal:** Minimize cognitive jumps, not ban keywords.

---

## 3. Model the Domain When It Pays Off

* Wrap primitives only when there is behavior, invariants, or validation.
* Avoid empty wrappers that add ceremony without value.

**Examples:** `Email`, `Money`, `Age` (with rules).

---

## 4. Encapsulate Collections With Behavior

* Create collection types when there are domain operations (aggregation, validation, policies).
* Keep raw collections when they are just data.

**Signal:** If multiple places manipulate the same list logic, encapsulate it.

---

## 5. Limit Deep Navigation (Demeter, Pragmatically)

* Avoid long chains that expose internal structure.
* Prefer asking the owning object for results when it reduces coupling.
* Don’t introduce pass-through methods that add no value.

---

## 6. Name for Clarity

* Use explicit, intention-revealing names.
* Avoid abbreviations that obscure meaning.
* Avoid excessively long, redundant names.

---

## 7. Optimize for Cohesion, Not Size

* Split classes when responsibilities diverge.
* Don’t create micro-classes without clear boundaries.

**Goal:** Each unit should have a focused reason to change.

---

## 8. Treat Large State as a Smell, Not a Ban

* Many fields can indicate low cohesion.
* Group related data into value objects when it improves clarity.
* Don’t force artificial splits to meet arbitrary limits.

---

## 9. Encapsulate Behavior Over Data

* Keep business rules close to the data they use (Tell, Don’t Ask).
* Provide intention-revealing methods (e.g., `user.isAdult()`).
* Use getters when appropriate (frameworks, serialization, simple access).

---

## Practical Checklist

* Is the flow easy to follow without jumping around?
* Are responsibilities clearly separated?
* Are domain rules encapsulated near the data?
* Is coupling kept under control?
* Are names clear and precise?

If yes, you’re aligned with the spirit—without the dogma.
