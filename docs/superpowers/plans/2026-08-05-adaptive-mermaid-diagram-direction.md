# Adaptive Mermaid Diagram Direction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Choose the direction of the `/describe` Mermaid changes diagram from the shape of the graph the model produced, instead of hard-coding left-to-right.

**Architecture:** A deterministic post-processing step applied to the output of the existing `sanitize_diagram()`. Three module-level functions in `pr_agent/tools/pr_description.py`: a line parser that extracts edges, a longest-path calculation over those edges, and a public entry point that rewrites the flowchart header. Settings are read at the call site and passed in as arguments, keeping the new functions pure and testable without patching globals.

**Tech Stack:** Python 3.12, pytest, Dynaconf settings, stdlib `re` only — no new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-05-adaptive-mermaid-diagram-direction-design.md`
- Ticket: AI-189. Upstream target: `The-PR-Agent/pr-agent`.
- Every failure mode returns the diagram unchanged. The change must never turn a working diagram into a broken one.
- No new imports beyond what `pr_agent/tools/pr_description.py` already has (`re`, `List`, `Tuple` are present; do not add `collections`).
- Ruff, `line-length = 120`.
- Run tests with `PYTHONPATH=. ./.venv/bin/pytest`.
- Do not reformat or reorder unrelated lines in prompt or config TOML files.
- The threshold counts **nodes** along the longest chain. Default 5: a five-node chain stays `LR`, a six-node chain becomes `TD`.

---

### Task 1: Edge parsing and longest-chain calculation

**Files:**
- Modify: `pr_agent/tools/pr_description.py` (add module-level constants and two helpers after `sanitize_diagram`, which ends at line 800)
- Test: `tests/unittest/test_pr_description.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_parse_diagram_edges(lines: List[str]) -> List[Tuple[str, str]]` and `_longest_diagram_chain(edges: List[Tuple[str, str]]) -> int`. `_longest_diagram_chain` raises `ValueError` when the graph contains a cycle. Task 2 consumes both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unittest/test_pr_description.py`, and extend the import on line 5 to
`from pr_agent.tools.pr_description import (PRDescription, _longest_diagram_chain, _parse_diagram_edges, sanitize_diagram)`:

```python
class TestDiagramEdgeParsing:

    def test_simple_edge(self):
        assert _parse_diagram_edges(['A --> B']) == [('A', 'B')]

    def test_chained_statement_becomes_consecutive_edges(self):
        assert _parse_diagram_edges(['A --> B --> C']) == [('A', 'B'), ('B', 'C')]

    def test_node_shapes_are_stripped(self):
        assert _parse_diagram_edges(['A["file.py"] --> B("output")']) == [('A', 'B')]

    def test_quoted_middle_label_does_not_create_a_node(self):
        assert _parse_diagram_edges(['A -- "calls" --> B']) == [('A', 'B')]

    def test_pipe_edge_label_does_not_create_a_node(self):
        assert _parse_diagram_edges(['A -->|calls| B']) == [('A', 'B')]

    def test_arrow_inside_a_label_is_not_an_edge(self):
        assert _parse_diagram_edges(['A["a --> b"]']) == []

    def test_arrow_variants(self):
        assert _parse_diagram_edges(['A --- B']) == [('A', 'B')]
        assert _parse_diagram_edges(['A -.-> B']) == [('A', 'B')]
        assert _parse_diagram_edges(['A ==> B']) == [('A', 'B')]
        assert _parse_diagram_edges(['A --o B']) == [('A', 'B')]

    def test_fan_out_shorthand_expands(self):
        assert _parse_diagram_edges(['A --> B & C']) == [('A', 'B'), ('A', 'C')]

    def test_structural_statements_are_ignored(self):
        lines = ['subgraph one', 'direction LR', 'A --> B', 'end', 'style A fill:#fff', '%% A --> Z']
        assert _parse_diagram_edges(lines) == [('A', 'B')]

    def test_fence_and_frontmatter_lines_produce_no_edges(self):
        assert _parse_diagram_edges(['```', '---', 'config:', '---']) == []


class TestLongestDiagramChain:

    def test_empty_graph_is_zero(self):
        assert _longest_diagram_chain([]) == 0

    def test_chain_length_counts_nodes(self):
        assert _longest_diagram_chain([('A', 'B'), ('B', 'C')]) == 3

    def test_fan_out_is_two_regardless_of_width(self):
        edges = [('A', chr(ord('B') + i)) for i in range(8)]
        assert _longest_diagram_chain(edges) == 2

    def test_longest_branch_wins(self):
        edges = [('A', 'B'), ('B', 'C'), ('C', 'D'), ('A', 'E')]
        assert _longest_diagram_chain(edges) == 4

    def test_cycle_raises(self):
        with pytest.raises(ValueError):
            _longest_diagram_chain([('A', 'B'), ('B', 'A')])
```

The file does not currently import pytest — add `import pytest` to the import block at the top,
above `import yaml`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_pr_description.py -q`
Expected: FAIL — `ImportError: cannot import name '_parse_diagram_edges'`

- [ ] **Step 3: Write the implementation**

Add to `pr_agent/tools/pr_description.py`, immediately after the `sanitize_diagram` function:

```python
DIAGRAM_HEADER_PATTERN = re.compile(r'^(\s*)(flowchart|graph)(\s+)(TB|TD|BT|RL|LR)\b(.*)$')
DIAGRAM_CONNECTOR_PATTERN = re.compile(r'<?[-=.~]{2,}[->ox]?')
DIAGRAM_NODE_ID_PATTERN = re.compile(r'[A-Za-z0-9_]+')
DIAGRAM_STATEMENT_KEYWORDS = ('subgraph', 'end', 'direction', 'style', 'classDef', 'linkStyle', 'click')


def _strip_diagram_labels(line: str) -> str:
    """Remove label text, so that arrows written inside a label are not read as edges."""
    line = re.sub(r'"[^"]*"', '', line)  # quoted labels, including the ones the prompt asks for
    line = re.sub(r'\|[^|]*\|', '', line)  # pipe-form edge labels: -->|text|
    for pattern in (r'\[[^\[\]]*\]', r'\([^()]*\)', r'\{[^{}]*\}'):
        previous = None
        while previous != line:  # nested shapes such as [[...]] need more than one pass
            previous = line
            line = re.sub(pattern, '', line)
    return line


def _parse_diagram_edges(lines: List[str]) -> List[Tuple[str, str]]:
    """Extract the directed edges of a mermaid flowchart body, tolerating its syntax variants."""
    edges = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith('%%'):
            continue
        if line.split(' ')[0].rstrip(';') in DIAGRAM_STATEMENT_KEYWORDS:
            continue

        cleaned = _strip_diagram_labels(line)
        if not DIAGRAM_CONNECTOR_PATTERN.search(cleaned):
            continue

        # Split on connectors; each remaining chunk is one or more node ids joined by '&'.
        # Chunks that are empty once labels are stripped collapse away, which is what makes
        # `A -- "text" --> B` read as the single edge A -> B.
        node_groups = []
        for chunk in DIAGRAM_CONNECTOR_PATTERN.split(cleaned):
            node_ids = []
            for token in chunk.split('&'):
                match = DIAGRAM_NODE_ID_PATTERN.match(token.strip().lstrip(';'))
                if match:
                    node_ids.append(match.group(0))
            if node_ids:
                node_groups.append(node_ids)

        for left, right in zip(node_groups, node_groups[1:]):
            edges.extend((source, target) for source in left for target in right)
    return edges


def _longest_diagram_chain(edges: List[Tuple[str, str]]) -> int:
    """Length, in nodes, of the longest path through the graph. Raises ValueError on a cycle."""
    adjacency = {}
    nodes = set()
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
        nodes.add(source)
        nodes.add(target)

    longest_from = {}
    in_progress = set()

    def walk(node: str) -> int:
        if node in in_progress:
            raise ValueError(f"cycle detected at node '{node}'")
        if node in longest_from:
            return longest_from[node]
        in_progress.add(node)
        longest = 1
        for neighbour in adjacency.get(node, []):
            longest = max(longest, 1 + walk(neighbour))
        in_progress.discard(node)
        longest_from[node] = longest
        return longest

    return max((walk(node) for node in nodes), default=0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_pr_description.py -q`
Expected: PASS, including the pre-existing `sanitize_diagram` tests.

- [ ] **Step 5: Commit**

```bash
git add pr_agent/tools/pr_description.py tests/unittest/test_pr_description.py
git commit -m "feat(describe): parse mermaid diagram edges and measure the longest chain"
```

---

### Task 2: Direction selection and header rewrite

**Files:**
- Modify: `pr_agent/tools/pr_description.py` (add `apply_diagram_direction` after `_longest_diagram_chain`)
- Test: `tests/unittest/test_pr_description.py`

**Interfaces:**
- Consumes: `_parse_diagram_edges`, `_longest_diagram_chain`, `DIAGRAM_HEADER_PATTERN` from Task 1.
- Produces: `apply_diagram_direction(diagram: str, direction: str = 'adaptive', threshold: int = 5) -> str`. Task 3 calls it from `_prepare_data`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unittest/test_pr_description.py`, adding `apply_diagram_direction` to the import on line 5:

```python
def _fenced(body: str) -> str:
    return f'\n```mermaid\n{body}\n```'


class TestApplyDiagramDirection:

    def test_short_chain_stays_horizontal(self):
        diagram = _fenced('flowchart LR\nA --> B --> C')
        assert apply_diagram_direction(diagram) == diagram

    def test_long_chain_becomes_vertical(self):
        body = 'A --> B --> C --> D --> E --> F'
        assert apply_diagram_direction(_fenced(f'flowchart LR\n{body}')) == _fenced(f'flowchart TD\n{body}')

    def test_chain_exactly_at_threshold_stays_horizontal(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E')
        assert apply_diagram_direction(diagram) == diagram

    def test_wide_fan_out_stays_horizontal(self):
        body = 'A --> B\nA --> C\nA --> D\nA --> E\nA --> F\nA --> G\nA --> H\nA --> I'
        diagram = _fenced(f'flowchart LR\n{body}')
        assert apply_diagram_direction(diagram) == diagram

    def test_graph_alias_is_rewritten_too(self):
        body = 'A --> B --> C --> D --> E --> F'
        assert apply_diagram_direction(_fenced(f'graph LR\n{body}')) == _fenced(f'graph TD\n{body}')

    def test_vertical_short_diagram_is_flipped_back_to_horizontal(self):
        assert apply_diagram_direction(_fenced('flowchart TD\nA --> B')) == _fenced('flowchart LR\nA --> B')

    def test_explicit_direction_pins_and_ignores_shape(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E --> F')
        assert apply_diagram_direction(diagram, direction='LR') == diagram
        assert apply_diagram_direction(_fenced('flowchart LR\nA --> B'), direction='TD') == \
            _fenced('flowchart TD\nA --> B')

    def test_custom_threshold_is_honoured(self):
        body = 'A --> B --> C'
        assert apply_diagram_direction(_fenced(f'flowchart LR\n{body}'), threshold=2) == \
            _fenced(f'flowchart TD\n{body}')

    def test_indentation_and_trailing_semicolon_are_preserved(self):
        body = 'A --> B --> C --> D --> E --> F'
        diagram = _fenced(f'  graph LR;\n{body}')
        assert apply_diagram_direction(diagram) == _fenced(f'  graph TD;\n{body}')

    def test_sequence_diagram_is_untouched(self):
        diagram = _fenced('sequenceDiagram\nA->>B: hello')
        assert apply_diagram_direction(diagram) == diagram

    def test_diagram_without_edges_is_untouched(self):
        diagram = _fenced('flowchart LR\nA["only a node"]')
        assert apply_diagram_direction(diagram) == diagram

    def test_cyclic_graph_is_untouched(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E --> F --> A')
        assert apply_diagram_direction(diagram) == diagram

    def test_unparseable_threshold_leaves_diagram_untouched(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E --> F')
        assert apply_diagram_direction(diagram, threshold='not-a-number') == diagram

    def test_subgraph_edges_are_counted(self):
        body = 'subgraph one\nA --> B --> C\nend\nsubgraph two\nC --> D --> E --> F\nend'
        assert apply_diagram_direction(_fenced(f'flowchart LR\n{body}')) == _fenced(f'flowchart TD\n{body}')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_pr_description.py -k ApplyDiagramDirection -q`
Expected: FAIL — `ImportError: cannot import name 'apply_diagram_direction'`

- [ ] **Step 3: Write the implementation**

Add to `pr_agent/tools/pr_description.py`, immediately after `_longest_diagram_chain`:

```python
def apply_diagram_direction(diagram: str, direction: str = 'adaptive', threshold: int = 5) -> str:
    """Set the flowchart direction, adapting it to the shape of the graph unless one is pinned.

    Width in an LR flowchart is set by the longest path, not by the node count, so the longest
    chain is what decides. Anything unexpected - no flowchart header, no edges, a cycle, a bad
    setting - returns the diagram untouched.
    """
    try:
        lines = diagram.split('\n')
        header_index, header_match = None, None
        for index, line in enumerate(lines):
            header_match = DIAGRAM_HEADER_PATTERN.match(line)
            if header_match:
                header_index = index
                break
        if header_index is None:
            return diagram

        requested = str(direction).strip().upper()
        if requested in ('LR', 'TD'):
            chosen = requested
        else:
            edges = _parse_diagram_edges(lines[header_index + 1:])
            if not edges:
                return diagram
            chosen = 'LR' if _longest_diagram_chain(edges) <= int(threshold) else 'TD'

        lines[header_index] = (f"{header_match.group(1)}{header_match.group(2)}"
                               f"{header_match.group(3)}{chosen}{header_match.group(5)}")
        return '\n'.join(lines)
    except Exception as e:
        get_logger().debug(f"Failed to adapt the diagram direction: {e}")
        return diagram
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_pr_description.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pr_agent/tools/pr_description.py tests/unittest/test_pr_description.py
git commit -m "feat(describe): choose diagram direction from the shape of the graph"
```

---

### Task 3: Wire into `_prepare_data`, add settings and documentation

**Files:**
- Modify: `pr_agent/tools/pr_description.py:472-475`
- Modify: `pr_agent/settings/configuration.toml:133`
- Modify: `docs/docs/tools/describe.md` (Sequence Diagram Support section around line 65, and the configuration table around line 144)
- Test: `tests/unittest/test_pr_description.py:19-23`

**Interfaces:**
- Consumes: `apply_diagram_direction` from Task 2.
- Produces: settings keys `pr_description.pr_diagram_direction` and `pr_description.pr_diagram_direction_threshold`. Nothing downstream depends on them.

- [ ] **Step 1: Write the failing test**

In `tests/unittest/test_pr_description.py`, replace the `_mock_settings` helper at lines 19-23 with a version that returns real values from `.get()`, because a bare `MagicMock.get()` returns a mock that cannot be compared with `<=`:

```python
def _mock_settings(pr_diagram_direction: str = 'adaptive', pr_diagram_direction_threshold: int = 5):
    """Mock get_settings used by _prepare_data."""
    settings = MagicMock()
    settings.pr_description.add_original_user_description = False
    settings.pr_description.get.side_effect = lambda key, default=None: {
        'pr_diagram_direction': pr_diagram_direction,
        'pr_diagram_direction_threshold': pr_diagram_direction_threshold,
    }.get(key, default)
    return settings
```

Then append this test to `TestPRDescriptionDiagram`:

```python
    @patch('pr_agent.tools.pr_description.get_settings')
    def test_long_chain_diagram_is_flipped_during_prepare_data(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        body = 'A --> B --> C --> D --> E --> F'
        obj = _make_instance(_prediction_with_diagram(f'```mermaid\nflowchart LR\n{body}\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == f'\n```mermaid\nflowchart TD\n{body}\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_pinned_direction_is_respected_during_prepare_data(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings(pr_diagram_direction='LR')
        body = 'A --> B --> C --> D --> E --> F'
        obj = _make_instance(_prediction_with_diagram(f'```mermaid\nflowchart LR\n{body}\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == f'\n```mermaid\nflowchart LR\n{body}\n```'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_pr_description.py -k prepare_data -q`
Expected: FAIL — the diagram still reads `flowchart LR` because `_prepare_data` does not call `apply_diagram_direction` yet.

- [ ] **Step 3: Wire it into `_prepare_data`**

Replace lines 472-475 of `pr_agent/tools/pr_description.py`:

```python
        if 'changes_diagram' in self.data:
            sanitized = sanitize_diagram(self.data.pop('changes_diagram'))
            if sanitized:
                self.data['changes_diagram'] = apply_diagram_direction(
                    sanitized,
                    get_settings().pr_description.get("pr_diagram_direction", "adaptive"),
                    get_settings().pr_description.get("pr_diagram_direction_threshold", 5),
                )
```

- [ ] **Step 4: Add the settings**

In `pr_agent/settings/configuration.toml`, directly below the existing `enable_pr_diagram` line (line 133), add:

```toml
pr_diagram_direction='adaptive' # 'adaptive', 'LR', 'TD'. 'adaptive' picks the direction from the shape of the diagram
pr_diagram_direction_threshold=5 # with 'adaptive', a chain longer than this many nodes is drawn top-down
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_pr_description.py tests/unittest/test_pr_description_output_core.py -q`
Expected: PASS

- [ ] **Step 6: Update the documentation**

In `docs/docs/tools/describe.md`, in the Sequence Diagram Support section, after the line
"This option is enabled by default via the `pr_description.enable_pr_diagram` param.", add:

```markdown
The direction of the diagram adapts to its shape. A diagram whose longest chain of nodes exceeds
`pr_description.pr_diagram_direction_threshold` is drawn top-down rather than left-to-right, so that
wide diagrams do not get scaled down until they are unreadable. Set
`pr_description.pr_diagram_direction` to `LR` or `TD` to pin the direction instead.
```

In the configuration table, directly after the `enable_pr_diagram` row, add:

```html
      <tr>
        <td><b>pr_diagram_direction</b></td>
        <td>Direction of the generated Mermaid flowchart: <b>adaptive</b>, <b>LR</b> or <b>TD</b>. With adaptive, the direction is chosen from the shape of the diagram. Default is adaptive.</td>
      </tr>
      <tr>
        <td><b>pr_diagram_direction_threshold</b></td>
        <td>With <b>adaptive</b> direction, a diagram whose longest chain exceeds this many nodes is drawn top-down instead of left-to-right. Default is 5.</td>
      </tr>
```

- [ ] **Step 7: Run the full unit suite and the linter**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest -q`
Expected: PASS, no regressions.

Run: `./.venv/bin/ruff check pr_agent/tools/pr_description.py tests/unittest/test_pr_description.py`
Expected: no findings.

- [ ] **Step 8: Commit**

```bash
git add pr_agent/tools/pr_description.py pr_agent/settings/configuration.toml \
        docs/docs/tools/describe.md tests/unittest/test_pr_description.py
git commit -m "feat(describe): make the changes-diagram direction configurable and adaptive"
```

---

## After the plan

The upstream issue on `The-PR-Agent/pr-agent` gets raised once these three tasks are green, followed by a PR referencing it. The PR branch must contain only the three commits above — the spec and this plan live under `docs/superpowers/` and must not appear in the upstream diff.
