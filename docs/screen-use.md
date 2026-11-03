# Finished screen use, version 1

This is Diurna policy `diurna-screen-use-app-fold-180s-v1`, independent of
`today`, hot settings, workspace grouping and UI `settle()`. It freezes the
inspected pure default classifier at 10,000,000,000 ns grace over **identical
complete context**. Configured views and provisional prefixes can disagree.
The collector and all existing views retain their meaning. Export can wait
indefinitely. No Fasti specification bundle is included; these occurrence
vectors freeze this slice's format pending comparison with the published
protocol. This is a local journal, without a sharing or interoperability claim.

## Frozen classifier, ordered passes

Input is validated, committed, nonoverlapping raw history in numeric start
order. Missing trailing signals are NULL. All reasons (`active`, `idle`,
`locked`, `suspended`, `unknown`, `disconnected`) and every independent
NULL/0/1 input/media/inhibiting combination are supported. An inconsistent
stored active flag, unknown reason, duplicate/overlap, non-null title, invalid
signal or invalid timestamp blocks; nothing is repaired or skipped.

1. Change reason `active` with exact, case-sensitive app class
   `org.omarchy.screensaver` to `idle`. Preserve every other reason, including
   explicit lock, suspend, unknown and disconnected on that class.
2. Only remaining active rows get an activity. NULL input gives legacy
   unsplit activity, regardless of media/inhibiting. Input 1 gives hands-on,
   regardless of media/inhibiting. Input 0 gives watching when media=1, or
   when media=NULL and inhibiting=1. Media=0 overrides the inhibitor. All other
   combinations give on-screen. Nonactive rows ignore media/inhibiting.
3. Find each contiguous run of screensaver-class rows, regardless of reason
   or signals. Sum its elapsed extent across checkpoints. **Strictly more
   than 10,000,000,000 ns** triggers a backward walk before the run. Walk only
   adjacent rows, stopping at a gap or a preceding input=1. Convert on-screen
   to idle/away, walk through idle, and stop on every other reason/activity
   (including watching, hands-on, legacy unsplit, locked, suspended, unknown
   and disconnected). Screensaver signals within the run do not stop that
   run from growing. Process runs in source order.
4. Walk forward with an away flag initially false. A gap clears it. If away,
   convert watching/on-screen to idle/away **before** updating the flag.
   Input=1 then clears it, even on an inactive or screensaver row. Otherwise
   idle/locked/suspended or any screensaver-class row sets it. Otherwise
   unknown/disconnected or legacy unsplit active clears it. Other rows carry
   it unchanged. Thus screensaver unknown+input=NULL sets away, while an
   ordinary unknown clears away; exceptional input=1 wins in this forward
   update. Legacy idle with NULL input still sets away. The converted-row
   marker records active time moved to idle; an originally active screensaver
   reads idle without that marker, exactly as in the inspected classifier.

No day/window clipping occurs before these passes. Persisted complete context
and forward-away interpretation survive restarts and policy cuts. The frozen
module never imports view code or settings. Tests compare every stored
reason/signal combination with the inspected default and enumerate future
appends to test watermark invariance.

## Conservative seal

A committed **non-screensaver** row supports a barrier at its start if it has
input=1, reason unknown/disconnected, or reason active with input=NULL.
Evidence records start, kind, preceding endpoint and committed barrier endpoint.
A strictly positive gap is supported only once a later committed row proves
it; its barrier is the **later row's start**, even if that row is a screensaver.
The monotonic classifier watermark W is the latest such start, or NULL when
none exists. No silence, timer, checkpoint, input announcement without a
committed interval, settings change, process exit, calendar or elapsed cut
supplies evidence. No source reset is manufactured.

For fixed rules and validated append-only history, classification **strictly
before W** is invariant under supported future appends. A backward walk cannot
cross a supported barrier: input stops it, unknown/disconnected and unsplit
active stop it, and a positive source gap breaks adjacency. Screensaver rows
are excluded from generic barriers because their contiguous run can still grow,
including with exceptional input or unknown flags. Forward-away state does not
travel backward. This is a deliberately conservative proof, without extra
shortcuts for seemingly stable examples.

For example, on-screen [0,60), idle [60,90), screensaver [90,100) keeps the
first minute active at exactly ten seconds. One additional screensaver
nanosecond changes that minute to away. Neither idle nor midnight seals it.
The same dependency can reach across midnight, or through an hour of idle
following twelve hours on screen. These zero-based numbers are explanatory
relative offsets; occurrence boundaries must be positive epoch nanoseconds.

## Complete grouping domains

Classify complete source history first. A contiguous active run ends on actual
classified inactivity or a proved source gap. Split its grouping domains at
Asia/Taipei calendar midnights and at most 24 elapsed hours from each domain's
start, whichever comes first. This also handles historical long calendar days.
A cut is covered when committed active source reaches it, including equality
at a committed endpoint. It limits grouping; it never seals classification.
A terminal active suffix stays open unless it covers a cut. No missing or idle
time is filled, absorbed or padded.

Publish a domain only if its closure is <= W. Fold the **whole** domain. An input
barrier inside an active run never creates a domain/period boundary. A cut
inside unresolved look-back waits for a later seal, and forward-away context
continues across it. Caller export windows select already journaled whole
occurrences by overlap; they cannot change grouping or identity.

Within each domain:

1. Coalesce adjacent active slices of the exact same app, ignoring workspace,
   signals and checkpoints. Each row retains exact per-app nanoseconds.
2. Repeatedly choose the shortest row **strictly below 180,000,000,000 ns**,
   earliest start on equal lengths. Preserve a lone short row.
3. If both neighbors have the same nonmixed dominant app, join all three.
   Otherwise join the shorter neighbor, choosing the earlier neighbor on ties.
   Re-evaluate shortest-first after every join.
4. A dominant app must hold **strictly more than half** the row duration.
   Exact half is mixed. Rank parts by descending duration then ascending raw
   UTF-8 bytes; internal evidence lists apps by ascending UTF-8 bytes, without
   normalization or locale collation. Finally merge adjacent equal labels,
   including mixed next to mixed, within the domain only.

There are no workspace/signal-only splits and no UI settling. Active duration
is the exact positive sum of active slices, equal to end minus start because
these domains contain neither idle nor gaps.

## Occurrence bytes and identities

The closed occurrence object has exactly `machine`, `generation`, `id`, `start`,
`end`, and `activity`. Machine/history UUIDs come from raw archive identity.
The qualified identity is `(machine, generation, id)` within this fixed rule's
scope. Allocate `id = "screen-use:" + canonical positive decimal publication
sequence` atomically, starting at 1. It is an assigned local identifier, never
a filename, timestamp, scan time or payload hash. No replacement IDs exist.

`start` and `end` are positive, signed-64-bit epoch-nanosecond instants rendered
as canonical UTC `YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ`, with exactly nine fractional
digits and end > start. Never pass nanoseconds through floating point.
`activity` has exactly `kind: "screen-use"`, `useMode: "application"` or
`"mixed"`, conditional `application`, and positive decimal-string
`activeDurationNs` without sign, leading zeros or exponent. Mixed omits
application. Application IDs retain exact Unicode; require 1–4096 UTF-8 bytes,
printable non-whitespace characters and no slash/backslash. Validate **every**
app contributing to a period, even in mixed output. Empty desktop classes,
paths, control characters and oversized IDs block publication rather than
inventing an app or skipping time. Titles, workspace, paths and raw signals
are absent from occurrences. Private seal/grouping evidence stays in storage.

Golden application vector (machine and generation are synthetic UUIDv4s):

```json
{"activity":{"activeDurationNs":"2","application":"終端","kind":"screen-use","useMode":"application"},"end":"1970-04-15T05:59:59.254740995Z","generation":"22222222-2222-4222-8222-222222222222","id":"screen-use:1","machine":"11111111-1111-4111-8111-111111111111","start":"1970-04-15T05:59:59.254740993Z"}
```

Golden mixed vector (one nanosecond each in a and b):

```json
{"activity":{"activeDurationNs":"2","kind":"screen-use","useMode":"mixed"},"end":"1970-01-01T00:00:00.000000003Z","generation":"22222222-2222-4222-8222-222222222222","id":"screen-use:2","machine":"11111111-1111-4111-8111-111111111111","start":"1970-01-01T00:00:00.000000001Z"}
```

JSON encoding is UTF-8, literal Unicode, lexically sorted keys, compact separators
and no NaN. JSONL adds exactly one LF per record. Publication sequence is a
numeric U64 (stored as canonical decimal TEXT to avoid SQLite's signed limit),
**publication order**, never a time cursor. Exhaustion blocks. This version
supports source-prefix catch-up, not independent backdated insertion. A future
explicit backfill policy must assign later sequences while preserving old
facts/IDs; event timestamps cannot be used as feed progress.

## Transactions, integrity and resource bounds

`screen_use_state` and `screen_use_journal` are additive tables under their own
`diurna.screen-use-journal-v2` format; raw `user_version` stays 3. An additive
`screen_use_guard` column on archive identity detects lost publication tables
instead of silently reminting them. First-use initialization establishes the
guard atomically before replay; no occurrence is advertised during it.

Each bounded pump takes `BEGIN IMMEDIATE` before its source snapshot. Competing
pumps serialize. Persist validated source rows, pending classifier output,
barrier evidence, W, rule/qualified identity, journal digest and sequence with
immutable occurrence payload and domain/grouping evidence in one transaction.
Journal and state must agree on every read and subsequent pump. SHA-256 detects
accidental state damage; it is not authentication against an adversary able to
rewrite the entire archive and checksums. Source replacement/tampering during
file-open and divergent producers of copied archives are outside this contract.

The pump reads a bounded source prefix beginning at the last sealed checkpoint,
using the source start index. Per pump/read limits: 4096 source rows per page,
4096 domains/periods per page, 4096 bytes per raw string, 16 MiB each for page
source serialization, state/checkpoint document, journal page/output, and
5,000,000 SQLite VM steps checked per 1000 steps. SQLite writer timeout is the
collector connection's existing timeout (five seconds), catch-up/read timeout
one second. App folding has bounded quadratic work within the source-page
ceiling. No code reads wall-clock age for publication.

At half the source-row ceiling, a pump can retire a prefix at a committed
classifier barrier **only if** classified inactivity immediately precedes that
barrier, or a positive gap proves the domain closure. Thus an input barrier
inside a continuous active grouping domain never splits it. The committed
barrier row is retained in both the sealed evidence and the next page's suffix;
it proves closure and resets forward-away interpretation. Checkpoints retain
all accepted raw observations, frozen classification evidence, qualified
identity, sequence base and page journal digest. Checkpoint documents form a
SHA-256 chain whose head is in current state. Sequence allocation remains
global and contiguous. A pump advances one page, so repeat catch-up to drain an
archive; a `ready` result need not have reached the latest raw endpoint.

SQLite guards enforce append-only source insertion, and latch source edits or
deletions and journal/checkpoint edits or deletions. Guard presence is audited
on each open/pump. This permits sealed pages to stop replaying old raw rows on
every write. Ordinary raw append depends only on the raw table even if derived
tables are lost. Reads audit the current suffix, and reconstruct each requested
sealed page from its checksummed evidence and frozen classifier before emitting
its occurrences. Reads are paginated by publication sequence; wide exports can
wait at their output or VM ceiling. A bounded read may audit adjacent pages.
Checksums and SQLite guards protect accidental damage and ordinary SQL edits;
an adversary able to replace guards, source and matching evidence is outside
the integrity contract. The v2 storage format refuses an existing v1 journal;
no automatic reminting, latch clearing or migration of published identity occurs.
The held installation with no journal tables initializes v2 directly.

A resource ceiling raises `PublicationWaiting`, rolls back all work from that
pump and **never writes a durable latch**. This includes source/journal/state
bytes, row/domain/period/output ceilings, and an interrupt known to come from
our SQLite VM budget. It is distinct from storage errors and arbitrary SQLite
interrupts. A suffix with no safe retirement point may wait indefinitely:
there is no truncation, age-out, forced finish, silent reset or garbage
collection. Restoring sufficient resources permits a later retry. Repeated
healthy replay is idempotent.

Raw `Store.write()` commits first, then calls publication under a separate
transaction/error boundary. Failure leaves raw storage/views/export usable and
prints a sanitized diagnostic. Catch-up is also explicit. Corruption and storage faults
roll back allocation, journal and W together, then attempt a separate durable
blocked latch. Even an uncertain commit (including an injected post-commit
failure) blocks; old durable state or the complete new publication can remain,
never a half-publication. Structural corruption/lost state fails audits without
reinitialization. If storage cannot persist a blocked latch, the call fails and
advertises nothing; storage health and the surviving journal require operator
inspection before another pump. Do not retry an uncertain operation as a new
stream, reset identity or issue replacement IDs.

Edited/deleted accepted source rows, mutated published payload/grouping,
unsupported rules, corrupt/lost identity/journal/pending state and exhausted
sequence require intervention. The collector never clears a blocked latch.
Keep a complete private backup; corruption recovery and future format migration
require an explicit inspected repair, outside this slice. Budget waits do not
require intervention. Nothing here changes
live owner data, installs software or claims a working deployment.

## Commands and typed consumer interface

```sh
diurna publish --database PRIVATE_ARCHIVE
# Raw commits also run this bounded advancement automatically.
diurna publication-status --database PRIVATE_ARCHIVE
diurna periods --database PRIVATE_ARCHIVE --start-ns POSITIVE_START --end-ns END
```

Catch-up opens existing private schema-3 state read/write without collection or
migration. `periods` and `publication-status` are read-only audits; they never
pump. An uninitialized journal is explicitly blocked/unavailable until a pump.
A healthy scope reports rule/format, machine, generation, status `ready`, nullable
`watermark_ns`, numeric `through_sequence`, `retention_floor_sequence: 1`,
`original_beginning_ns` (NULL for empty source), and `retained_all: true`.
A NULL W means waiting for evidence, even if raw history is old. `ready` means
healthy storage, not that the active suffix is finished. Budget waits from `publish` or `publication-status` return
`{"status":"waiting","interventionRequired":false}` with exit 2; `periods`
returns exit 2 with no output. These outcomes make no integrity or completion
claim and never clear an existing latch. Integrity/storage failure returns
`{"status":"blocked","interventionRequired":true}` from status and exit 1.
Raw commits suppress budget-wait diagnostics, so a large unresolved suffix does
not print an intervention warning on every write.

Finished JSONL contains one `type: scope` object with those scope fields, zero
or more `type: occurrence` records with decimal-string `sequence` and the closed
`occurrence` object, then exactly one `type: complete` record with numeric `rows`.
The explicit finite positive range selects overlapping whole occurrences.
Check successful exit and the completion record. Output is assembled within
budget before writing; failed writes do not imply successful export.

`publication.Journal` is a context-managed pinned snapshot. `scope()` returns
immutable typed `Scope`; `entries(after_sequence, limit)` returns typed `Entry`
records in numeric publication order. Its integrity audit uses the frozen
producer implementation; a future feed consumes this interface and performs
no classification/grouping. Reads/export are never ingestion acknowledgments.
Initially retain **all** published facts from the original beginning, floor 1,
regardless of reads. Future retention needs explicit floor/sequence semantics;
reset needs a new disclosed history and migration plan; independent backfill
needs explicit overlap handling. None is automatic in this version. Raw export
remains independent when the classifier waits or the finished stream blocks.
