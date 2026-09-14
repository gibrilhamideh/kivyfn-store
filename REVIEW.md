# kivyfn-store Full Architecture and Code Review

I want a deep technical review of the entire `kivyfn-store` project.

Before giving conclusions:

1. Read this file completely.
2. Inspect the entire `src/kivyfn/store/` package.
3. Inspect the entire `tests/` directory.
4. Understand how the major components interact before judging individual files.
5. Do not assume behavior from names alone. Follow the actual execution paths.
6. Do not propose rewrites merely because another design is more familiar.
7. Distinguish real architectural problems from personal style preferences.

Do not modify the project unless explicitly asked.

---

# Project Intent

`kivyfn-store` is intended to be a schema-driven reactive Store and State framework for Python/Kivy applications.

The main architecture is approximately:

```text
Application
    ↓
State / CollectionState / QueryCollectionState
    ↓
StateManager
    ↓
StoreClient
    ↓
Transport
    ↓
StoreHost
    ↓
Store
    ↓
Engine
    ↓
Node / Schema / Persistence
```

Canonical changes return through:

```text
StoreCommit
    ↓
StoreHost
    ↓
SubscriptionEvent
    ↓
StoreClient
    ↓
StateManager
    ↓
Reactive State
    ↓
Kivy bindings
```

The intended separation is:

```text
Store
    canonical data
    validation
    constraints
    persistence
    query execution
    atomic execution

Host / Client
    protocol
    batching
    subscriptions
    synchronization
    transport

State
    reactive application API
    optimistic behavior
    strict mutations
    transactions
    Kivy events
    signals
```

The application should primarily interact with State rather than directly managing Store subscriptions or transport details.

---

# Main Features to Understand

Review how these features actually work together:

* `SingleSchema`
* `ManySchema`
* schema fields and validation
* Nodes
* Engine instructions and execution
* Store transactions
* Store commits
* per-schema revisions
* persistence
* Store boot/version behavior
* StoreHost
* StoreClient
* Direct transport
* Socket.IO transport
* request/response codecs
* subscription management
* reconnect/resynchronization
* `State`
* `CollectionState`
* `QueryCollectionState`
* `StateManager`
* optimistic mutations
* strict mutations
* `StateTransaction`
* live Queries
* Query joins
* projected joined fields
* writable joined fields
* Signals
* signal events
* signals inside transactions

---

# Review Goals

I want an assessment of the project from several independent perspectives.

## 1. Overall Architecture

Determine whether the main architecture is coherent.

Evaluate:

* whether responsibilities are clearly separated;
* whether Store, Host, Client, StateManager, and reactive State have sensible boundaries;
* whether any layer knows too much about another layer;
* whether there are circular responsibilities;
* whether abstractions are justified;
* whether important abstractions are missing;
* whether the architecture will remain understandable as the project grows;
* whether transport-independent behavior really is transport-independent;
* whether local and remote behavior can remain consistent;
* whether the architecture has unnecessary duplication;
* whether data flows have obvious ownership.

Trace several complete operations end-to-end rather than reviewing components only in isolation.

At minimum trace:

```text
optimistic property assignment
strict set
insert
remove
multi-action transaction
query subscription
joined-field write
signal emission
signal-only transaction
reconnect
```

For each, identify the authoritative layer at every step.

---

# 2. Code Clarity

Review the implementation for readability and maintainability.

Look for:

* classes doing too much;
* methods doing too much;
* methods with unclear contracts;
* misleading names;
* unnecessary validation;
* duplicate validation;
* duplicate transformation logic;
* overly defensive code;
* hidden side effects;
* surprising mutation;
* excessive casting;
* typing workarounds hiding design problems;
* deeply nested control flow;
* abstraction layers that add complexity without meaning;
* duplicated concepts under different names;
* inconsistent naming;
* comments/docstrings that disagree with implementation;
* code that is technically correct but difficult to reason about.

Also identify code that is particularly clean and should be preserved.

Do not recommend abstraction merely to reduce line count.

Prefer simple code with explicit ownership.

---

# 3. Folder and Package Structure

Review the project tree as an API and architecture map.

Ask whether someone unfamiliar with the project could reasonably predict where code belongs.

Evaluate folders such as:

```text
builder/
client/
codec/
core/
events/
host/
node/
protocol/
query/
schema/
state/
storage/
store/
transport/
```

Look for:

* folders with overlapping responsibility;
* files located in unintuitive packages;
* package names that no longer match current architecture;
* unnecessary nesting;
* folders that should be combined;
* folders that have become dumping grounds;
* concepts split across too many locations;
* circular imports caused by poor ownership;
* public vs internal boundaries that are unclear.

Give a proposed tree only if the current tree genuinely has structural problems.

Do not reorganize merely for aesthetics.

---

# 4. State Architecture

Review the reactive State system carefully.

Understand:

```text
State
CollectionState
QueryCollectionState
StateFactory
StateRegistry
StateManager
StateData
StateTransaction
transaction actions
```

Evaluate:

* identity guarantees;
* canonical vs overlay data;
* optimistic updates;
* strict updates;
* rollback;
* transaction lifecycle;
* pending state;
* Kivy event behavior;
* generated properties;
* generated signal methods/events;
* collection membership;
* query membership;
* row identity;
* registry caching;
* dynamic State type generation.

Look specifically for cases where visible State could diverge from canonical Store data.

---

# 5. Transaction Semantics

Review transaction correctness thoroughly.

Expected high-level behavior:

```text
State actions
    ↓
StateTransaction
    ↓
protocol request actions
    ↓
Store
    ↓
resolve
    ↓
validate
    ↓
execute atomically
    ↓
StoreCommit
```

Check:

* atomicity;
* rollback;
* optimistic rollback;
* insert/remove cancellation;
* repeated sets;
* multiple schemas;
* trigger-generated mutations;
* transaction participant tracking;
* transaction lifecycle callbacks;
* ordering of canonical events;
* transaction response ordering;
* interaction with subscription events.

Determine whether callbacks can observe partially committed data.

---

# 6. Revision and Synchronization Model

The project uses per-schema integer revisions.

Review whether this model is correct and consistently implemented.

Check:

* when revisions advance;
* whether one schema advances at most once per transaction;
* whether unchanged schemas remain unchanged;
* partial dependency revision events;
* signal-only events with `{}` revisions;
* revision gaps;
* duplicate events;
* out-of-order events;
* reconnects;
* new Store instances;
* cached subscriptions;
* snapshots;
* resubscription.

Look for silent desynchronization possibilities.

---

# 7. Queries

Review Query architecture end-to-end.

Important concepts include:

* immutable Query description;
* query compilation;
* dependencies;
* live HostQuery;
* QueryShape;
* QueryFieldShape;
* joins;
* aliases;
* query subscriptions;
* membership transitions;
* projected fields;
* canonical field routing.

Joined fields may be writable because query metadata identifies their canonical schema, field, and relationship path.

Example:

```text
query field:
    user_username

canonical source:
    UserSchema
    username
    joined user row ID
```

Check whether this mapping is robust for:

* root fields;
* joined fields;
* multiple joined fields;
* nested joins;
* nullable joins;
* changed foreign keys;
* query membership changes;
* joined rows changing independently.

Look specifically for cases where a projected field could accidentally be sent to the query's root schema.

---

# 8. Signals

Signals are ephemeral occurrences, not retained State.

Expected properties:

* no canonical value;
* no persistence;
* no snapshot;
* no optimistic overlay;
* no rollback value;
* repeated identical signals are separate occurrences;
* signals do not advance schema revisions.

For a `ManySchema`, a signal belongs to a row:

```python
scale.tare()
```

Listeners may exist at both levels:

```text
row State
    on_tare(state, data)

CollectionState
    on_tare(collection, row_id, data)
```

Expected dispatch order:

```text
row
collection
```

Signals participating in a transaction should be emitted only after successful canonical mutation.

Callbacks should observe final committed State.

Review:

* Store representation;
* protocol representation;
* codec;
* Host routing;
* query routing;
* client revision handling;
* StateManager dispatch;
* transaction failure behavior;
* signal-only commits;
* signal ordering.

Look for any path that could emit a signal from a failed transaction.

---

# 9. Persistence

Review persistence semantics and failure handling.

Check:

* atomicity of persisted changes;
* boot behavior;
* corruption handling;
* schema/store version compatibility;
* migration behavior;
* delete persistence;
* interrupted writes;
* partial writes;
* stale files;
* recovery behavior;
* invalid versions.

Determine whether an unexpected process shutdown could leave the Store in an inconsistent state.

---

# 10. Error Handling

Review error boundaries carefully.

Determine whether errors are raised at the correct layer.

Look for:

* raw internal exceptions leaking to application code;
* errors being swallowed;
* overly broad `except Exception`;
* impossible states silently ignored;
* network errors incorrectly represented as Store errors;
* protocol violations incorrectly represented as validation errors;
* failed transactions that do not fully clean up;
* pending operations left unresolved;
* inconsistent error mapping.

Review the relationship between:

```text
Store errors
protocol failure responses
transaction failures
connection failures
State rollback
```

---

# 11. Concurrency and Ordering

Even if much of the Store itself is synchronous, the surrounding Client/Host/State system is asynchronous.

Review:

* asyncio lifecycle;
* request batching;
* callback ordering;
* subscription event ordering;
* transaction response vs subscription event ordering;
* reconnect races;
* disconnect during transactions;
* messages arriving during resubscription;
* duplicate subscriptions;
* pending futures;
* cancellation;
* transport reconnect behavior.

Identify race conditions even if they are difficult to reproduce in tests.

---

# 12. Edge Cases

Actively search for untested edge cases.

Do not restrict the review to edge cases already represented in tests.

Consider at least:

### State

* same value assignment;
* assignment before synchronization;
* mutation during reconnect;
* mutation after disconnect;
* multiple State objects pointing to the same canonical row;
* deleting a row with outstanding mutation;
* strict + optimistic mutation overlap;
* failed transaction after optimistic membership change.

### Transactions

* empty transaction;
* duplicate action;
* set same field multiple times;
* insert then remove;
* remove then insert same ID;
* signal before mutation in transaction;
* signal after mutation;
* multiple signals;
* failed mutation + signal;
* transaction touching multiple schemas;
* trigger changes additional schemas.

### Subscriptions

* duplicate subscription;
* unsubscribe during event delivery;
* stale subscription ID;
* unknown subscription ID;
* reconnect while events are buffered;
* Store instance changes;
* missing revision;
* revision gap;
* duplicate revision;
* partial query dependency revision;
* event with no actions;
* signal-only event.

### Queries

* zero results;
* row enters query;
* row leaves query;
* dependency changes without result changes;
* joined target changes;
* foreign key changes;
* joined row deleted;
* nested join;
* nullable relationship;
* alias collision;
* projected field name collision.

### Signals

* no payload;
* arbitrary payload;
* repeated signal;
* two different signals;
* row callback ordering;
* collection callback ordering;
* query membership filtering;
* failed transaction suppression;
* signal-only transaction;
* signal mixed with multiple mutations.

### Persistence

* restart;
* delete then restart;
* migration failure;
* corrupt state;
* missing version;
* interrupted write.

### Transport

* disconnect mid-request;
* reconnect before response;
* duplicate transport delivery;
* malformed payload;
* unexpected response type;
* wrong transaction ID;
* wrong Store instance ID.

---

# 13. Test Suite Review

Read every test.

Do not only report whether tests pass.

Evaluate:

* what behavior each test actually proves;
* important behavior that is only incidentally covered;
* redundant tests;
* brittle tests;
* tests coupled too strongly to implementation;
* missing unit tests;
* missing integration tests;
* missing full-stack tests;
* race conditions not tested;
* negative cases not tested.

Create a coverage matrix.

Suggested format:

| Feature        | Existing coverage | Missing cases                     | Risk   |
| -------------- | ----------------- | --------------------------------- | ------ |
| Optimistic set | Good              | concurrent mutation               | Medium |
| Signals        | Partial           | failed transaction + query signal | High   |
| Query joins    | ...               | ...                               | ...    |

Do not claim "fully covered" unless you can justify it.

---

# 14. Public API Review

Evaluate whether application code has a coherent public API.

Look at usage such as:

```python
users = state.create(UserSchema)

user = users["user-1"]

user.username = "new"

await user.set(username="new")

users.insert(...)

with state.transaction():
    ...

Query(...)

row.signal()
```

Determine:

* whether APIs are predictable;
* whether similar concepts behave similarly;
* whether implementation concerns leak through;
* whether important behavior is hidden;
* whether names communicate optimistic vs strict semantics clearly.

---

# 15. Type System Review

Review Pyright-facing design.

Look for:

* `Any` leaking unnecessarily;
* excessive `cast`;
* inaccurate Protocols;
* runtime-generated State types fighting static typing;
* incorrect generics;
* TypeAlias misuse;
* APIs where static typing and runtime semantics disagree.

Separate unavoidable dynamic typing from typing problems that indicate architectural ambiguity.

---

# 16. Performance and Scale

Do not micro-optimize.

Instead identify structural performance risks.

Consider:

* subscription fan-out;
* query reevaluation;
* repeated snapshots;
* large collections;
* many clients;
* large numbers of schemas;
* State object count;
* serialization overhead;
* Socket.IO traffic;
* persistence writes;
* revision bookkeeping.

Explain what is fine for current scale and what may become problematic later.

---

# Required Final Report

Produce the final review in this order.

## 1. Executive Assessment

Give an overall score from 1–10 for:

* architecture;
* code clarity;
* folder structure;
* correctness;
* edge-case handling;
* test quality;
* maintainability.

Explain each score briefly.

---

## 2. Architecture Map

Describe the architecture as it actually exists in the code.

Do not simply repeat this document.

Mention any differences between intended and implemented architecture.

---

## 3. What Is Strong

Identify parts of the design that should be preserved.

---

## 4. Critical Problems

Only genuine correctness or architectural risks.

For every finding include:

```text
Severity:
Files:
Problem:
Why it matters:
Example failure scenario:
Recommended direction:
```

Severity:

```text
Critical
High
Medium
Low
```

---

## 5. Code Clarity Findings

Identify confusing or unnecessarily complicated implementation areas.

---

## 6. Folder Structure Findings

Assess whether package organization communicates the architecture clearly.

---

## 7. Edge Cases

List edge cases grouped by:

```text
covered correctly
partially covered
not covered
```

---

## 8. Test Coverage Matrix

Map major features to their tests and identify missing tests.

---

## 9. Tests I Would Add

Give concrete test names and describe exactly what each should verify.

For example:

```text
test_failed_transaction_does_not_emit_signal
test_signal_callback_observes_final_committed_state
test_duplicate_subscription_event_is_rejected
```

Prioritize them.

---

## 10. Simplification Opportunities

Identify places where code can become simpler without losing capability.

Avoid large rewrites unless necessary.

---

## 11. Architecture Risks for Future Growth

Focus on realistic future pressure rather than theoretical problems.

---

## 12. Recommended Next Steps

Give a prioritized sequence:

```text
1. correctness problems
2. missing tests
3. architectural cleanup
4. code clarity cleanup
5. optional optimization
```

---

# Important Review Rules

Do not praise the project merely because tests pass.

Do not criticize it merely because it differs from common frameworks.

Do not propose replacing custom architecture with Redux, SQLAlchemy, Django,
Pydantic, etc. unless there is a concrete technical reason.

Do not focus primarily on formatting.

Do not recommend defensive validation everywhere.

Validation should happen at the correct ownership boundary and should not be
duplicated unnecessarily.

Do not rewrite the project during the review.

First understand it.

Then identify specific problems supported by actual code paths.

When possible, cite exact files, classes, functions, and relevant execution
paths.

If you are uncertain whether something is a bug, label it as a concern rather
than presenting it as fact.

