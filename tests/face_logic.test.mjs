// Node test of the liveness state machines in staff/web/face.js using scripted face metrics (no camera needed).
import assert from 'node:assert/strict';
globalThis.document = { createElement: () => ({}) };           // face.js builds a probe canvas in FaceEngine only
const { FaceVerifier, TaskTimeout } = await import('../staff/web/face.js');

const face = (o = {}) => ({ faces: 1, box: { cx: 0.5, cy: 0.5, w: 0.4, h: 0.5 }, turn: 0, blink: 0.05, smile: 0.05, jaw: 0.02, brightness: 120, ...o });

function run(tasks, script, { timeout = 20000 } = {}) {
  let i = 0;
  const engine = { analyze: () => script(i++) };
  const v = new FaceVerifier({ engine, video: {}, onInstruction() {}, onWarning() {}, onTask() {} });
  v.capture = async () => ({ size: 1 });
  return Promise.race([v.run(tasks), new Promise((_, rej) => setTimeout(() => rej(new Error('test timeout')), timeout))]);
}

// framing problems are reported, not accepted
assert.match(FaceVerifier.framingProblem(face({ faces: 0 })), /can’t see/);
assert.match(FaceVerifier.framingProblem(face({ faces: 2 })), /Only you/);
assert.match(FaceVerifier.framingProblem(face({ brightness: 20 })), /dark/);
assert.match(FaceVerifier.framingProblem(face({ box: { cx: 0.5, cy: 0.5, w: 0.08, h: 0.1 } })), /closer/);
assert.match(FaceVerifier.framingProblem(face({ turn: 0.4 })), /straight/);
assert.equal(FaceVerifier.framingProblem(face()), '');

// center + blink: needs ~1.3s of stable framing, then open -> closed -> open
let r = await run(['center', 'blink'], (n) => {
  if (n < 5) return face({ faces: 0 });                       // nobody yet
  if (n < 30) return face();                                  // framed, eyes open
  if (n < 34) return face({ blink: 0.8 });                    // blink
  return face({ blink: 0.05 });
});
assert.deepEqual(r.steps.map((s) => s.name), ['center', 'blink']);

// smile and open_mouth need a neutral frame first (a frozen smiling photo does not pass)
await assert.rejects(run(['smile'], () => face({ smile: 0.9 }), { timeout: 20000 }), /(?:We couldn’t complete|test timeout)/);

r = await run(['smile', 'open_mouth'], (n) => (n < 6 ? face() : n < 14 ? face({ smile: 0.8 }) : n < 20 ? face() : face({ jaw: 0.7 })));
assert.equal(r.steps.length, 2);

// turn_head requires BOTH sides then back to centre, in either direction (sign-agnostic)
r = await run(['turn_head'], (n) => (n < 5 ? face() : n < 12 ? face({ turn: -0.3 }) : n < 20 ? face({ turn: 0.3 }) : face({ turn: 0.02 })));
assert.equal(r.steps[0].name, 'turn_head');
r = await run(['turn_head'], (n) => (n < 5 ? face() : n < 12 ? face({ turn: 0.3 }) : n < 20 ? face({ turn: -0.3 }) : face({ turn: 0.02 })));
assert.equal(r.steps[0].name, 'turn_head');
// turning only one way never passes
await assert.rejects(run(['turn_head'], (n) => (n < 5 ? face() : face({ turn: 0.3 })), { timeout: 70000 }), /(?:We couldn’t complete|test timeout)/);
console.log('face logic ok');
