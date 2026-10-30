# Raw local spans, version 1

`diurna export --database PATH --start-ns START --end-ns END` writes a bounded,
read-only JSON Lines snapshot to stdout. The database input and both bounds are
required. START and END are signed 64-bit integer epoch nanoseconds; START < END.
There is no desktop, classifier, network, Fasti service or downstream dependency.
No delivery is acknowledged. Export does not create, migrate, prune or modify
span/identity facts. Schema 3 is required; stop collection and explicitly run
`diurna migrate --database PATH` first for an older archive. Migration takes the
collector's database lock and refuses while it is held. It does not start collection.

The source must be an existing regular file owned by the caller, with no
permissions for group/others, no extra hard links and no final-component symlink.
Copy an archive privately before selecting it. SQLite WAL snapshots are supported;
export uses `mode=ro`, not `immutable`. SQLite may need its normal WAL/SHM sidecars.
Concurrent replacement or tampering with source paths is outside the contract.

## Closed wire format

Every line is a UTF-8 JSON object followed by one LF, with keys in the order below,
no BOM, no extra whitespace and no additional keys. JSON strings use literal
Unicode, with JSON escapes for quotes, backslashes and control characters. Numbers
are decimal integers, never floating point. Consumers must retain integers beyond
IEEE-754's exact range. Every record has `format: "diurna.raw-spans"`, `version: 1`
and `type`. A future format requires a different version and explicit support.

1. Exactly one `snapshot` record: `format`, `version`, `type`, `machine`,
   `generation`, `startNs`, `endNs`, `throughStartNs`.
2. Zero or more `span` records: `format`, `version`, `type`, `machine`, `generation`,
   `id`, `startNs`, `endNs`, `appClass`, `workspace`, `reason`, `input`, `media`,
   `inhibiting`.
3. Exactly one terminal `complete` record: `format`, `version`, `type`, `rows`.
   `rows` is the number of span records, including zero for an empty selection.

Machine and generation are canonical lowercase UUIDv4 strings. The qualified
identity of a span is `(machine, generation, id)`. `id` is `span:` followed by the
canonical signed decimal stored `start_ns` (zero is `span:0`; no plus or leading
zeros). The unique-start storage invariant supplies uniqueness. Payload, row ID,
ordinal, filename, path, scan time and selection are never part of identity.

`startNs` and `endNs` are exact stored signed integer nanoseconds. `appClass` and
`workspace` are exact stored strings; empty strings are legitimate, including an
empty desktop or unknown span. `reason` is exactly one of `active`, `idle`,
`locked`, `suspended`, `disconnected`, `unknown`. `input`, `media`, `inhibiting`
are independently boolean or null. Null means unreported, including all legacy
schema-1 signals. No title, SQL row ID, active flag, host identifier, derived
activity, metadata or additional field is exported. A non-null stored title is
rejected without reading its contents into the output or diagnostic.

These are **raw stored labels and signals**. Raw `reason: "active"` makes no
claim about corrected active time, attention, finished screen periods or Fasti
occurrences. This explicit local export contains app/workspace metadata. It is
not an enabled public sharing projection or a public interoperability claim.

## Selection, snapshot and completion

A single SQLite read transaction pins all reads at the first source read.
`throughStartNs` is the maximum committed stored start in that snapshot, or null
for an empty archive. Optional `--through-start-ns N` caps it at N (the smaller
of N and the observed maximum). A non-null value may be saved and reused to bound subsequent
reads of the same retained history. Null means no spans. A later append cannot
move the high point or enter an already running read.

Membership is interval overlap: `end_ns > START AND start_ns < END`, additionally
`start_ns <= throughStartNs`. Equality at START's ending edge and END's starting
edge excludes a span. Emit complete stored boundaries, even outside the requested
range. Checkpoints stay separate. Order is ascending numeric `start_ns`. Gaps are
preserved, never padded. Neither clipping nor changing/overlapping selection
mints identities. Repeated reads of unchanged retained spans have identical
qualified IDs and payload. The snapshot header can change with the range/high
point; it is not a span identity or a seal/finality assertion.

Hard per-invocation budgets are 10,000 selected rows, 4,096 UTF-8 bytes for each
app/workspace/reason string, 16,777,216 total output bytes (headers and LF included),
and 10,000,000 SQLite VM steps, checked in blocks of 1,000. SQLite's busy timeout
is one second. The SQLite page cache target is 2 MiB. These are ceilings, not
pagination or retention. There is no success-with-truncation mode. Request a
smaller range after budget failure. No promise is made that every arbitrary
large/malformed archive can be scanned within the VM budget.

Missing/unsupported/incomplete storage, invalid identity, invalid intervals or
signals, overlaps/duplicate starts in selected rows, unsafe source selection,
invalid requests and budget exhaustion fail nonzero. Invalid intervals up to
the high point are rejected even if their overlap cannot be determined. Labels
and signals outside the selection are not payload-validated. Failures never
silently drop, pad or coalesce selected spans. Runtime failures print exactly
`diurna: raw export failed` plus LF to stderr; argument syntax errors print
`diurna: invalid request` plus LF and exit 2. Diagnostics contain no paths,
source values or private text.

A runtime failure may leave a partial stdout prefix without `complete`. A consumer
must verify the closed record sequence, row count, and successful process exit;
a terminal record alone cannot prove that writing/flushing stdout succeeded.
Never accept a prefix as a complete snapshot. Pipe/file write errors also fail.
No downstream receipt, publication sequence, bookmark or delivery is implied.

## Independently authored golden bytes

This hand-written example fixes key order, encoding, null/boolean distinctions,
negative IDs and exact integers outside JavaScript's safe range. It selects
[-2, 1), while preserving both complete checkpoint fragments. The final LF after
the complete record is part of the example. These UUIDs are synthetic test values.

```jsonl
{"format":"diurna.raw-spans","version":1,"type":"snapshot","machine":"11111111-1111-4111-8111-111111111111","generation":"22222222-2222-4222-8222-222222222222","startNs":-2,"endNs":1,"throughStartNs":-1}
{"format":"diurna.raw-spans","version":1,"type":"span","machine":"11111111-1111-4111-8111-111111111111","generation":"22222222-2222-4222-8222-222222222222","id":"span:-9007199254740993","startNs":-9007199254740993,"endNs":-1,"appClass":"終端","workspace":"","reason":"active","input":null,"media":null,"inhibiting":null}
{"format":"diurna.raw-spans","version":1,"type":"span","machine":"11111111-1111-4111-8111-111111111111","generation":"22222222-2222-4222-8222-222222222222","id":"span:-1","startNs":-1,"endNs":9007199254740993,"appClass":"終端","workspace":"","reason":"active","input":false,"media":true,"inhibiting":false}
{"format":"diurna.raw-spans","version":1,"type":"complete","rows":2}
```

## Identity, migration and private archive operations

Schema 3 adds one `diurna_identity` row: `singleton=1`,
`format="diurna-identity-v1"`, `machine`, `generation`. The collector creates random
UUIDv4 values once for fresh state or once for a previously anonymous schema-1/2
history. No hostname, system machine ID or downstream app supplies identity.
The generation names this database history. Ordinary restarts never remint it.

An immediate transaction reads the schema version, conditionally adds signals
only for versions before 2, creates identity, and commits `user_version=3`
together. IDs, timestamps, app/workspace facts, reasons, titles and existing
signals are not rewritten. Schema-1 signals become NULL. An exception, interruption
or failed commit rolls back the entire upgrade; retry against intact old state is
safe. Concurrent initialization sees a complete winner or fails. Unsupported
versions and nonempty unversioned databases fail. Established schema-3 state with
missing, duplicate, malformed or unsupported identity blocks both export and
new collection; repair from a complete known-good archive. Identity damage is
never interpreted as permission to assign a new identity.

Stop collection before backup/restore/migration. Keep a private, complete archive
copy, including uncheckpointed SQLite WAL state when present (prefer SQLite's
backup API or a cleanly closed database). Keep the original for rollback. Copying
or restoring the complete archive preserves both identity values and all qualified
span IDs; renaming the file does not change them. Loss/corruption of identity in
an established archive blocks use. An old anonymous backup has no identity to
preserve; assigning one during its explicit migration cannot recover a previously
assigned identity absent from that backup.

Do not run independent divergent collectors from copied archives with the same
identity. Diurna cannot automatically detect arbitrary identical database copies.
Before new collection, explicitly choose a new, empty archive at a separate path;
it receives a new machine/generation pair, while the archived history retains its
original pair. Record/disclose any historical overlap when combining old copies
or later exports. There is no command to relabel existing archived spans in place,
and no automatic clone/new-history inference.

Readers for `today` accept schema 1, 2 and 3, with the same signals for 2 and 3.
Classification, configured grace, local calendar, folds, totals, tabs and bar
behavior are unchanged. Older collectors/readers may reject schema 3. Do not
lower `user_version`, delete identity, or strip columns as a downgrade. Roll back
using the complete pre-upgrade backup and matching old software; any subsequent
new history needs an explicit identity/overlap disposition as described above.

## Finished screen-use journal

`diurna periods` is a separate local format for immutable, finished screen-use
occurrences. `diurna publish` advances it from committed history and
`diurna publication-status` audits its scope and retention. Its frozen classifier,
conservative watermark, app-only grouping and budgets are documented in
[screen-use.md](screen-use.md). Raw export neither advances that journal nor
asserts finality. It remains independently available while publication waits
for a barrier or is blocked. Reads of either format never acknowledge intake.
