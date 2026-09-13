import { SqlCodeBlock } from '@/components/SqlCodeBlock'

// A structured, human-readable view of a version's serialized_space (Genie schema v2). Each
// schema concept gets its own clearly-labeled section: data_sources split into Tables and
// Metric views; instructions.{text_instructions, join_specs, sql_snippets.{measures,
// expressions, filters}, sql_functions, example_question_sqls}; sample_questions
// (config/root); and benchmarks.questions. The snapshot type is `unknown`, so every read is
// defensive: unknown keys are preserved under "Other", empty sections are omitted, and the
// raw JSON stays available behind a disclosure for power users.

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function text(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  // Descriptions and similar fields are often string arrays; render them as plain text
  // rather than a JSON dump. Objects still fall back to JSON.
  if (Array.isArray(value)) return value.map(text).filter(Boolean).join(' ')
  return JSON.stringify(value)
}

function firstText(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = text(record[key])
    if (value) return value
  }
  return ''
}

// text_instructions entries may be a plain string or { content: string[] } — the API
// concatenates content elements (each ends with \n) into one instruction.
function instructionText(value: unknown): string {
  if (typeof value === 'string') return value
  const record = asObject(value)
  if (Array.isArray(record.content)) return record.content.map(text).join('')
  return text(value)
}

const box = 'rounded-md border border-default bg-surface p-2'

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-secondary">
        {title}{typeof count === 'number' ? <span className="ml-1 text-muted">({count})</span> : null}
      </h4>
      {children}
    </section>
  )
}

// One table / metric view: identifier, description, and its column list (flat for now —
// Slice 2 turns this into a collapsible tree with per-column attribute badges).
function DataSourceCard({ entry, index }: { entry: unknown; index: number }) {
  const table = asObject(entry)
  const columns = asArray(table.column_configs)
  return (
    <div className={box}>
      <p className="font-mono text-xs text-secondary break-all">{firstText(table, ['identifier', 'table', 'name']) || `Data source ${index + 1}`}</p>
      {firstText(table, ['description']) && <p className="mt-0.5 text-xs text-muted break-words">{firstText(table, ['description'])}</p>}
      {columns.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {columns.map((column, ci) => {
            const col = asObject(column)
            const description = firstText(col, ['description'])
            return (
              <li key={ci} className="text-xs text-muted break-words">
                <span className="font-mono text-secondary break-all">{firstText(col, ['column_name', 'name'])}</span>
                {description ? ` — ${description}` : ''}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

// A sql_snippet (measure / expression / filter): label, SQL, and its optional API metadata
// (synonyms, instruction, comment).
function SqlEntry({ record, index, labelKeys }: { record: Record<string, unknown>; index: number; labelKeys: string[] }) {
  const label = firstText(record, labelKeys) || `#${index + 1}`
  const sql = firstText(record, ['sql', 'answer', 'query'])
  const synonyms = asArray(record.synonyms).map(text).filter(Boolean)
  const instruction = firstText(record, ['instruction'])
  const comment = firstText(record, ['comment'])
  return (
    <div className={`${box} space-y-1`}>
      <p className="text-xs text-secondary break-words">{label}</p>
      {sql ? <SqlCodeBlock code={sql} maxLines={8} /> : null}
      {synonyms.length > 0 && <p className="text-xs text-muted break-words">Synonyms: {synonyms.join(', ')}</p>}
      {instruction && <p className="text-xs text-muted break-words">{instruction}</p>}
      {comment && <p className="text-xs text-muted break-words">{comment}</p>}
    </div>
  )
}

// An example_question_sqls entry: question, SQL, bound parameters, and usage guidance.
function ExampleSqlEntry({ record, index }: { record: Record<string, unknown>; index: number }) {
  const question = firstText(record, ['question', 'display_name', 'id']) || `#${index + 1}`
  const sql = firstText(record, ['sql'])
  const parameters = asArray(record.parameters)
  const usage = firstText(record, ['usage_guidance'])
  return (
    <div className={`${box} space-y-1`}>
      <p className="text-xs text-secondary break-words">{question}</p>
      {sql ? <SqlCodeBlock code={sql} maxLines={8} /> : null}
      {parameters.length > 0 && (
        <div className="space-y-0.5">
          <p className="text-[11px] uppercase tracking-wide text-muted">Parameters</p>
          {parameters.map((param, pi) => {
            const p = asObject(param)
            const name = firstText(p, ['name'])
            const typeHint = firstText(p, ['type_hint', 'type'])
            const desc = firstText(p, ['description'])
            const def = firstText(asObject(p.default_value), ['values']) || firstText(p, ['default_value'])
            return (
              <p key={pi} className="text-xs text-muted break-words">
                <span className="font-mono text-secondary break-all">{name}</span>
                {typeHint ? ` · ${typeHint}` : ''}
                {desc ? ` — ${desc}` : ''}
                {def ? ` (default: ${def})` : ''}
              </p>
            )
          })}
        </div>
      )}
      {usage && <p className="text-xs text-muted break-words">Usage: {usage}</p>}
    </div>
  )
}

// A sql_functions entry: UC identifier + description (no SQL body in the config).
function FunctionEntry({ record, index }: { record: Record<string, unknown>; index: number }) {
  const identifier = firstText(record, ['identifier', 'name', 'id']) || `#${index + 1}`
  const description = firstText(record, ['description'])
  return (
    <div className={`${box} space-y-0.5`}>
      <p className="font-mono text-xs text-secondary break-all">{identifier}</p>
      {description && <p className="text-xs text-muted break-words">{description}</p>}
    </div>
  )
}

// A benchmarks.questions entry: the evaluation question + its expected SQL answer.
function BenchmarkEntry({ record, index }: { record: Record<string, unknown>; index: number }) {
  const question = firstText(record, ['question', 'id']) || `#${index + 1}`
  const answer = asObject(asArray(record.answer)[0])
  const sql = firstText(answer, ['content', 'sql'])
  return (
    <div className={`${box} space-y-1`}>
      <p className="text-xs text-secondary break-words">{question}</p>
      {sql ? <SqlCodeBlock code={sql} maxLines={8} /> : null}
    </div>
  )
}

export function ConfigView({ snapshot }: { snapshot: unknown }) {
  const root = asObject(snapshot)
  const dataSources = root.data_sources
  const tables = Array.isArray(dataSources) ? dataSources : asArray(asObject(dataSources).tables)
  const metricViews = Array.isArray(dataSources) ? [] : asArray(asObject(dataSources).metric_views)

  const instructions = asObject(root.instructions)
  const textInstructions = (asArray(instructions.text_instructions).length
    ? asArray(instructions.text_instructions)
    : asArray(root.text_instructions))
  const exampleSqls = asArray(instructions.example_question_sqls)
  const joins = asArray(instructions.join_specs)
  const snippets = asObject(instructions.sql_snippets)
  const filters = asArray(snippets.filters)
  const expressions = asArray(snippets.expressions)
  const measures = asArray(snippets.measures)
  const sqlFunctions = asArray(instructions.sql_functions)
  const sampleQuestions = (asArray(root.sample_questions).length
    ? asArray(root.sample_questions)
    : asArray(instructions.sample_questions).length
      ? asArray(instructions.sample_questions)
      : asArray(asObject(root.config).sample_questions))
  const benchmarkQuestions = asArray(asObject(root.benchmarks).questions)

  const metadataEntries = Object.entries(root).filter(
    ([key, value]) =>
      !['data_sources', 'instructions', 'text_instructions', 'sample_questions', 'config', 'benchmarks'].includes(key) &&
      (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'),
  )
  const handled = new Set(['data_sources', 'instructions', 'text_instructions', 'sample_questions', 'config', 'benchmarks', ...metadataEntries.map(([key]) => key)])
  const otherEntries = Object.entries(root).filter(([key]) => !handled.has(key))

  const hasAnything =
    tables.length || metricViews.length || textInstructions.length || joins.length ||
    measures.length || expressions.length || filters.length || sqlFunctions.length ||
    exampleSqls.length || sampleQuestions.length || benchmarkQuestions.length ||
    metadataEntries.length || otherEntries.length

  return (
    <div className="space-y-4">
      {tables.length > 0 && (
        <Section title="Tables" count={tables.length}>
          <div className="space-y-2">
            {tables.map((entry, index) => <DataSourceCard key={index} entry={entry} index={index} />)}
          </div>
        </Section>
      )}

      {metricViews.length > 0 && (
        <Section title="Metric views" count={metricViews.length}>
          <div className="space-y-2">
            {metricViews.map((entry, index) => <DataSourceCard key={index} entry={entry} index={index} />)}
          </div>
        </Section>
      )}

      {textInstructions.length > 0 && (
        <Section title="Instructions">
          <div className="space-y-2">
            {textInstructions.map((entry, index) => (
              <pre key={index} className={`${box} max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs text-secondary`}>
                {instructionText(entry)}
              </pre>
            ))}
          </div>
        </Section>
      )}

      {measures.length > 0 && (
        <Section title="Measures" count={measures.length}>
          <div className="space-y-2">
            {measures.map((entry, index) => (
              <SqlEntry key={index} record={asObject(entry)} index={index} labelKeys={['display_name', 'name', 'id']} />
            ))}
          </div>
        </Section>
      )}

      {expressions.length > 0 && (
        <Section title="Expressions" count={expressions.length}>
          <div className="space-y-2">
            {expressions.map((entry, index) => (
              <SqlEntry key={index} record={asObject(entry)} index={index} labelKeys={['display_name', 'name', 'id']} />
            ))}
          </div>
        </Section>
      )}

      {filters.length > 0 && (
        <Section title="Filters" count={filters.length}>
          <div className="space-y-2">
            {filters.map((entry, index) => (
              <SqlEntry key={index} record={asObject(entry)} index={index} labelKeys={['display_name', 'id']} />
            ))}
          </div>
        </Section>
      )}

      {joins.length > 0 && (
        <Section title="Joins" count={joins.length}>
          <div className="space-y-2">
            {joins.map((entry, index) => {
              const join = asObject(entry)
              const left = firstText(asObject(join.left), ['identifier', 'alias'])
              const right = firstText(asObject(join.right), ['identifier', 'alias'])
              const sql = asArray(join.sql).map(text).filter(Boolean).join('\n') || firstText(join, ['sql'])
              return (
                <div key={index} className={`${box} space-y-1 min-w-0`}>
                  <p className="text-xs text-secondary break-all">{left && right ? `${left} ⋈ ${right}` : firstText(join, ['id']) || `Join ${index + 1}`}</p>
                  {firstText(join, ['comment', 'instruction']) && <p className="text-xs text-muted break-words">{firstText(join, ['comment', 'instruction'])}</p>}
                  {sql && <SqlCodeBlock code={sql} maxLines={6} />}
                </div>
              )
            })}
          </div>
        </Section>
      )}

      {sqlFunctions.length > 0 && (
        <Section title="SQL functions" count={sqlFunctions.length}>
          <div className="space-y-2">
            {sqlFunctions.map((entry, index) => (
              <FunctionEntry key={index} record={asObject(entry)} index={index} />
            ))}
          </div>
        </Section>
      )}

      {exampleSqls.length > 0 && (
        <Section title="Example SQL" count={exampleSqls.length}>
          <div className="space-y-2">
            {exampleSqls.map((entry, index) => (
              <ExampleSqlEntry key={index} record={asObject(entry)} index={index} />
            ))}
          </div>
        </Section>
      )}

      {sampleQuestions.length > 0 && (
        <Section title="Sample questions" count={sampleQuestions.length}>
          <ul className="space-y-1">
            {sampleQuestions.map((entry, index) => (
              <li key={index} className="text-xs text-secondary">{firstText(asObject(entry), ['question', 'text']) || text(entry)}</li>
            ))}
          </ul>
        </Section>
      )}

      {benchmarkQuestions.length > 0 && (
        <Section title="Benchmarks" count={benchmarkQuestions.length}>
          <div className="space-y-2">
            {benchmarkQuestions.map((entry, index) => (
              <BenchmarkEntry key={index} record={asObject(entry)} index={index} />
            ))}
          </div>
        </Section>
      )}

      {metadataEntries.length > 0 && (
        <Section title="Metadata">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            {metadataEntries.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-muted">{key}</dt>
                <dd className="text-secondary break-words">{text(value)}</dd>
              </div>
            ))}
          </dl>
        </Section>
      )}

      {otherEntries.length > 0 && (
        <Section title="Other">
          <pre className={`${box} max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs text-muted`}>
            {JSON.stringify(Object.fromEntries(otherEntries), null, 2)}
          </pre>
        </Section>
      )}

      {!hasAnything && (
        <p className="text-xs text-muted">No structured configuration in this snapshot.</p>
      )}

      <details className="rounded-lg border border-default bg-surface-secondary/40">
        <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-secondary">
          View raw JSON
        </summary>
        <pre className="max-h-80 overflow-auto px-3 pb-3 text-xs text-muted whitespace-pre-wrap break-words">
          {JSON.stringify(snapshot, null, 2)}
        </pre>
      </details>
    </div>
  )
}
