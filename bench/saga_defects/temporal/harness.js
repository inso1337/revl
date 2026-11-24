// Runs one saga port on a REAL Temporal server, once with no fault and then once
// per failure point, and prints what happened as JSON on stdout.
//
// Usage: node harness.js <address> <port-dir> <workflow-name>
//
// The result is one JSON object after a `@@RESULT@@` marker on stdout.
//
// <port-dir> holds `src/workflows.ts` (the workflow bundle) and `dist/` (the
// compiled activities and state). The harness never judges: it reports each
// run's failure, the fake world and the journal, and the Python runner applies
// the assertions, so they can be tested without a server.
//
// The number of failure points is not configured. It is the number of
// `faultPoint` activities the saga calls in its clean run, the way revl's sweep
// reads its step count from the IR; a saga with none has no failure points and
// the runner refuses to score it.
const path = require('path')
const { Worker, NativeConnection } = require('@temporalio/worker')
const { Client, Connection } = require('@temporalio/client')

const [address, portDir, workflowName] = process.argv.slice(2)
if (!address || !portDir || !workflowName) {
  process.stderr.write('usage: node harness.js <address> <port-dir> <workflow-name>\n')
  process.exit(2)
}
const root = path.resolve(portDir)
const state = require(path.join(root, 'dist', 'state.js'))
const activities = require(path.join(root, 'dist', 'activities.js'))

function failureMessage(error) {
  let cursor = error
  const seen = new Set()
  while (cursor && cursor.cause && !seen.has(cursor)) {
    seen.add(cursor)
    cursor = cursor.cause
  }
  return String(cursor && cursor.message ? cursor.message : cursor)
}

async function main() {
  const taskQueue = `revl-saga-defects-${process.pid}-${Date.now()}`
  const connection = await Connection.connect({ address })
  let counter = 0
  try {
    const worker = await Worker.create({
      connection: await NativeConnection.connect({ address }),
      taskQueue,
      workflowsPath: path.join(root, 'src', 'workflows.ts'),
      activities,
    })
    const client = new Client({ connection })

    async function run(failAfterStep) {
      state.reset()
      let failed = false
      let message = null
      try {
        await client.workflow.execute(workflowName, {
          taskQueue,
          workflowId: `${taskQueue}-${counter++}`,
          args: [failAfterStep === null ? {} : { failAfterStep }],
          workflowExecutionTimeout: '60 seconds',
        })
      } catch (error) {
        failed = true
        message = failureMessage(error)
      }
      return { step: failAfterStep, failed, message, world: state.snapshot() }
    }

    let out = null
    await worker.runUntil((async () => {
      const control = await run(null)
      const steps = control.world.journal.filter((call) => call.name === 'faultPoint').length
      const runs = []
      for (let step = 1; step <= steps; step++) runs.push(await run(step))
      out = { workflow: workflowName, steps, control, runs }
    })())
    // worker log lines may share stdout; the runner reads after this marker
    process.stdout.write('\n@@RESULT@@' + JSON.stringify(out))
  } finally {
    await connection.close()
  }
}

main().then(() => process.exit(0), (error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error))
  process.exit(1)
})
