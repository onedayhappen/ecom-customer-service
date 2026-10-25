import test from 'node:test'
import assert from 'node:assert/strict'
import {
  acknowledge,
  advanceCursor,
  awaitingReply,
  mergeMessages,
  restorePending,
} from '../src/conversationState.js'

const event = (sequence, id = `reply-${sequence}`) => ({
  sequence,
  kind: 'message',
  actor: 'bot',
  created_at: '2026-10-20T00:00:00Z',
  payload: { event_id: id, role: 'assistant', content: `回复 ${sequence}` },
})

test('typing tracks unanswered user messages, pending sends and reply coverage', () => {
  const user = { role: 'user', sequence: 1, request_id: 'a' }
  const reply = { role: 'assistant', sequence: 2, received_sequence: 1 }
  const pending = [{ payload: { request_id: 'a' }, status: 'sending' }]
  assert.equal(awaitingReply([], [], 0), false)
  assert.equal(awaitingReply([], pending), true)
  assert.equal(awaitingReply([], [{ ...pending[0], status: 'failed' }]), false)
  assert.equal(awaitingReply([user], [], 1), true)
  assert.equal(awaitingReply([user, reply], pending, 1), false)
  const followup = { role: 'user', sequence: 3, request_id: 'b' }
  const lateReply = { ...reply, sequence: 4 }
  assert.equal(awaitingReply([user, reply, followup, lateReply], [], 3), true)
  assert.equal(awaitingReply([user, { role: 'agent', internal: true, sequence: 2 }], []), true)
  assert.equal(awaitingReply([user, { role: 'agent', sequence: 2 }], []), false)
})

test('SSE replay, out-of-order notifications and refreshed history show each reply once', () => {
  let messages = mergeMessages([], [event(2), event(5)])
  messages = mergeMessages(messages, [
    event(5),
    event(8),
    { kind: 'task_processing', sequence: 9, payload: {} },
  ])
  messages = mergeMessages(messages, [event(2), event(5)])
  messages = mergeMessages(messages, [event(1), event(2)])
  assert.deepEqual(
    messages.map((m) => m.sequence),
    [1, 2, 5, 8],
  )
  const reloaded = mergeMessages([], [event(1), event(2), event(5), event(8)])
  assert.deepEqual(messages, reloaded)
  assert.equal(advanceCursor(9, 5), 9)
  assert.equal(advanceCursor(9, 12), 12)
})

test('parallel sends resolve independently, including an acknowledgement before HTTP returns', () => {
  const pending = ['a', 'b', 'c'].map((request_id) => ({
    payload: { request_id },
    status: 'sending',
  }))
  const afterB = acknowledge(pending, [{ request_id: 'b' }])
  assert.deepEqual(
    afterB.map((x) => x.payload.request_id),
    ['a', 'c'],
  )
  assert.deepEqual(acknowledge(afterB, [{ request_id: 'b' }, { request_id: 'a' }]), [pending[2]])
  assert.deepEqual(
    pending.map((x) => x.payload.request_id),
    ['a', 'b', 'c'],
  )
})

test('unknown HTTP outcome survives refresh with the identical request id and original text', () => {
  const payload = { request_id: 'stable-id', session_id: 's', message: '不是 A，是 B' }
  const storage = { getItem: () => JSON.stringify([{ payload, status: 'sending' }]) }
  const restored = restorePending(storage, 'key', 's')
  assert.deepEqual(restored, [{ payload, status: 'failed' }])
  assert.deepEqual(acknowledge(restored, [{ request_id: 'stable-id' }]), [])
  assert.deepEqual(restorePending(storage, 'key', 'other'), [])
  assert.deepEqual(restorePending({ getItem: () => '{bad' }, 'key', 's'), [])
})
