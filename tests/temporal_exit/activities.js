// Host implementations of the crossings the emitted ExitSaga workflow proxies
// (roadmap item 253, the exit test). revl emits the SHAPE of this module —
// `RevlActivities` in the generated workflow — and an operator writes the
// bodies; these are the operator's bodies, with two faults injected.
//
// Everything runs in the worker process, so a module-level journal is enough
// to observe what Temporal actually did: which activity ran, in what order,
// and on which ATTEMPT. The attempt number is the whole point — it is how a
// derived retry policy is checked against the real platform rather than
// against a string in the emitted file.
const { Context } = require('@temporalio/activity')

const journal = []

function record(name) {
  const attempt = Context.current().info.attempt
  journal.push({ name, attempt })
  return attempt
}

module.exports.__journal = journal

module.exports.flightsReserve = async function flightsReserve(itinerary) {
  record('flightsReserve')
  return `reserved:${itinerary}`
}

module.exports.flightsCancel = async function flightsCancel(key) {
  record('flightsCancel')
  return `cancelled:${key}`
}

module.exports.paymentsCharge = async function paymentsCharge(card, total) {
  record('paymentsCharge')
  return `charged:${card}:${total}`
}

module.exports.paymentsRefund = async function paymentsRefund(card, total) {
  record('paymentsRefund')
  return `refunded:${card}:${total}`
}

// FAULT 1, on the KEYED crossing. `settle` declares `idempotent(key: card)`,
// so the derivation put it in the retryable group. It fails once; if Temporal
// honours the derived policy the second attempt runs and the saga proceeds
// past it. If it did not, the saga would abort here instead and the
// compensation journal below would be shorter.
module.exports.settle = async function settle(card, total) {
  const attempt = record('settle')
  if (attempt === 1) throw new Error('settle: transient host failure')
  return `settled:${card}:${total}`
}

// FAULT 2, on a crossing with NO evidence. `boom.trigger` declares nothing, so
// it stays at-most-once. It always fails; the number of `boomTrigger` entries
// in the journal is therefore a direct measurement of whether the platform
// re-ran a non-idempotent effect.
module.exports.boomTrigger = async function boomTrigger() {
  record('boomTrigger')
  throw new Error('boom: the injected mid-saga fault')
}

module.exports.recordResidue = async function recordResidue(report) {
  record('recordResidue')
  module.exports.__report = report
}
