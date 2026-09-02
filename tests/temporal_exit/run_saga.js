// Runs the emitted ExitSaga workflow on a REAL Temporal server and prints what
// happened as JSON on stdout (roadmap item 253's exit test).
//
// Usage: node run_saga.js <address> <workflow-file.ts>
//
// The workflow file is the emitter's own output, written by the Python test
// immediately before this runs — never a hand-written stand-in, which is the
// only way the run says anything about the emitter.
const path = require('path')
const { Worker } = require('@temporalio/worker')
const { Client, Connection } = require('@temporalio/client')

const address = process.argv[2]
const workflowPath = path.resolve(process.argv[3])
const activities = require('./activities')

async function main() {
  const taskQueue = `revl-exit-saga-${process.pid}-${Date.now()}`
  const connection = await Connection.connect({ address })
  try {
    const worker = await Worker.create({
      connection: await require('@temporalio/worker').NativeConnection.connect({ address }),
      taskQueue,
      workflowsPath: workflowPath,
      activities,
    })
    const client = new Client({ connection })
    let failure = null
    await worker.runUntil(async () => {
      try {
        await client.workflow.execute('ExitSaga', {
          taskQueue,
          workflowId: taskQueue,
          workflowExecutionTimeout: '2 minutes',
        })
      } catch (error) {
        const cause = error && error.cause ? error.cause : error
        failure = {
          message: String(cause && cause.message ? cause.message : cause),
          type: (cause && cause.type) || null,
          details: (cause && cause.details) || null,
        }
      }
    })
    process.stdout.write(JSON.stringify({
      journal: activities.__journal,
      report: activities.__report ?? null,
      failure,
    }))
  } finally {
    await connection.close()
  }
}

main().then(() => process.exit(0), (error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error))
  process.exit(1)
})
