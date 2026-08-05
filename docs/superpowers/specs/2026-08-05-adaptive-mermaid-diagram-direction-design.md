# Adaptive Mermaid diagram direction for `/describe`

Ticket: [AI-189](https://novamedia.atlassian.net/browse/AI-189)
Date: 2026-08-05
Target: upstream contribution to `The-PR-Agent/pr-agent`

## Problem

`/describe` attaches a Mermaid "changes diagram" to the PR description. The direction of that
graph is hard-coded to left-to-right: `pr_agent/settings/pr_description_prompts.toml:66` asks the
model for "a horizontal diagram ... in the format of a valid mermaid LR flowchart".

When the model produces a long chain of nodes, the resulting graph is wider than the markdown
column it is rendered into. GitLab scales the SVG down until the node labels are unreadable;
GitHub overflows the column. The only control available today is
`pr_description.enable_pr_diagram`, which turns the feature off entirely.

The complaint has surfaced upstream before, but always framed as diagram quality rather than
layout: issue #1919 ("the changes diagram is counterproductive") was closed with advice to
disable it or pass `extra_instructions`. Issue #2342 is open and covers a different failure, a
diagram whose Mermaid syntax is invalid and renders as an error on GitLab. Issues #1943 and #2211
covered label escaping. No open upstream issue asks for control over direction.

## Constraints

Mermaid has no responsive or reflow mode. Direction is fixed by the diagram author, and the
renderer will not choose one. The configuration knobs Mermaid does expose through frontmatter
(`nodeSpacing`, `rankSpacing`, `wrappingWidth`) change spacing, not topology, so they cannot turn
a wide graph into a tall one.

The ELK layout engine handles dense graphs far better than the default dagre layout, but it was
unbundled from Mermaid core in v11 and has to be registered by the host page. Neither GitHub nor
GitLab registers it, so it is not available to us.

That leaves the direction keyword on the flowchart header as the only lever.

## Design

Direction is decided after the model has produced the diagram, by inspecting the shape of the
graph it produced. This is deterministic, costs no tokens, and is testable without a model in the
loop.

### Metric

The trigger is the **longest chain** in the graph — the longest path from a source node to a sink
node, measured in nodes — not the total node count.

Total node count is the wrong proxy. In an `LR` flowchart, width is set by the longest path:
eight nodes fanning out from a single source render comfortably left-to-right, whereas an
eight-node chain does not. Conversely a long chain is exactly what `TD` handles well. Measuring
the chain therefore targets the property that actually causes the unreadable render.

### Configuration

Two new keys in the `[pr_description]` section of `pr_agent/settings/configuration.toml`, placed
next to the existing diagram flag:

```toml
pr_diagram_direction='adaptive'   # 'adaptive', 'LR', 'TD'
pr_diagram_direction_threshold=5  # longest chain, in nodes, before switching to TD
```

This mirrors the `collapsible_file_list='adaptive'` / `collapsible_file_list_threshold=6` pair
already present three lines below in the same section, so the convention is not new to the file.

Under `'adaptive'` the direction is chosen from the graph shape: `LR` when the longest chain is
at or under the threshold, `TD` above it. A threshold of 5 means a five-node chain stays
horizontal and a six-node chain becomes vertical.

Setting `'LR'` or `'TD'` pins the direction and skips the analysis entirely. This gives anyone
who dislikes the heuristic an escape hatch short of disabling diagrams, and it satisfies the
"configurable" half of the ticket's ask alongside the "adaptive" half. Any other value, including
a malformed one, falls back to `'adaptive'` rather than raising.

The direction is chosen from shape regardless of which direction the model emitted, so there is a
single code path rather than a one-way LR-to-TD special case. In practice the model almost always
emits `LR` because the prompt asks for it.

### Where it hooks in

`pr_agent/tools/pr_description.py:471` currently reads:

```python
if 'changes_diagram' in self.data:
    sanitized = sanitize_diagram(self.data.pop('changes_diagram'))
    if sanitized:
        self.data['changes_diagram'] = sanitized
```

A new module-level function `apply_diagram_direction(diagram, direction, threshold)` is applied to
the sanitized output. `sanitize_diagram()` keeps its current responsibility — fence hygiene and
backtick stripping — unchanged.

Settings are read at the call site and passed in as arguments rather than read inside the
function. This matches the existing pure-function style of `sanitize_diagram()` and keeps the new
logic testable without patching global settings.

### Parsing

The function works on the lines between the code fences. It ignores an optional YAML frontmatter
block, `%%` comments, `%%{...}%%` directives, and `style` / `classDef` / `linkStyle` / `click` /
`subgraph` / `end` / `direction` statements. Edges inside subgraphs still count; the subgraph
grouping itself does not affect the metric.

The header is located with a match on `^\s*(flowchart|graph)\s+(TB|TD|BT|RL|LR)\b`. If no such
header exists — a `sequenceDiagram`, a `classDiagram`, or anything else — the diagram is returned
untouched.

On each remaining line, quoted label contents are blanked before anything else is examined, so
that a label containing literal arrow text cannot be mistaken for an edge. Pipe-form edge labels
(`-->|text|`) and remaining bracketed shapes (`[...]`, `(...)`, `{...}`) are then stripped. What
is left is split on a connector pattern covering the arrow variants: `-->`, `---`, `-.->`, `==>`,
`~~~`, and the `--o` / `--x` arrowheads. Consecutive connectors with nothing between them collapse,
which is what makes the middle-label form (`A -- "text" --> B`) parse as a single `A → B` edge
once the quoted text has been blanked. Fan-out shorthand (`A --> B & C`) expands on `&`.

Chained statements (`A --> B --> C`) fall out of this naturally: the token sequence on a line
becomes consecutive edges.

Known limitation: an **unquoted** middle label (`A -- text --> B`) parses as a three-node chain
rather than a two-node one, because there is no way to distinguish an unquoted label from a node
ID at that position. The prompt already instructs the model to quote every label, and the error
biases toward `TD`, which is the safe direction. This is accepted rather than worked around.

### Failure behaviour

Every failure mode returns the diagram exactly as it came in: unparseable input, no header match,
no edges found, a cycle in the graph, or any unexpected exception. The whole body is wrapped in a
`try`/`except` that logs at debug level and returns the original.

This property matters more than the feature itself. Given #2342, broken diagrams are already a
live complaint upstream, and a layout tweak that could corrupt a working diagram would be a net
loss. The change can only ever improve a diagram or leave it alone.

## Testing

Parametrized cases extend `tests/unittest/test_pr_description.py`, which already exercises
`sanitize_diagram` directly:

- a three-node chain stays `LR`; a six-node chain becomes `TD`
- eight nodes fanning out from one source stay `LR`, confirming the metric is chain and not count
- each arrow variant, both edge-label forms, and the `&` fan-out shorthand
- a label containing a literal `-->` does not create a phantom edge
- chained `A --> B --> C` statements
- edges inside a `subgraph` block
- a cyclic graph is returned unchanged
- `graph LR` is treated the same as `flowchart LR`
- a `sequenceDiagram` is returned unchanged
- `pr_diagram_direction='LR'` and `'TD'` pin the direction and ignore the shape

## Documentation

Two rows in the configuration table in `docs/docs/tools/describe.md`, and a sentence in the
Sequence Diagram Support section noting that direction adapts to the size of the graph.

## Out of scope

Capping how many nodes the model may emit, tuning Mermaid spacing through frontmatter config, and
the inline diagrams that can appear in `/review` output. AI-189 scopes this to the description
diagram, and each of those is a separable change with its own risk.

## Interim local override

An override is available today with no code: `extra_instructions` under `[pr_description]` in the
repository's `.pr_agent.toml` can ask for a top-down flowchart. The recommendation is not to ship
it. It costs prompt tokens on every `/describe` run and forces `TD` unconditionally, which makes
small diagrams worse than they are now. This satisfies the ticket's requirement to log a decision
on the interim override.

## Upstream plan

The spec is written before the upstream issue deliberately: the issue is more persuasive with a
working implementation and passing tests behind it. Once the code is green, an issue goes to
`The-PR-Agent/pr-agent` describing the layout problem and the proposal, followed by a PR
referencing it.

The spec document itself is a local process artifact and must not appear in the upstream diff, so
the PR branch will carry only the code, configuration, documentation and test commits.
