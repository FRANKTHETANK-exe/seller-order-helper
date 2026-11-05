// DOM/transport simulation only; no browser rendering or external requests.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = fs.readFileSync('sandbox-review-ui.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const scenario = process.argv[2];
const elements = new Map();
const el = id => {
  if (!elements.has(id)) elements.set(id, {disabled:false, hidden:false, textContent:'', value:'synthetic question', handlers:{}, addEventListener(event, fn) {this.handlers[event]=fn;}});
  return elements.get(id);
};
let state = {client_kind:'stub', generation_attempted:false, live_refresh_enabled:false, review_status:'pending', source:'saved_paypal_sandbox_receipt', order_status:'CREATED', payment_truth:'CREATED_NOT_APPROVED', observed_at:'2026-01-01T00:00:00Z', amount:'10.00', currency:'USD', draft:null};
if (scenario === 'stale-live-stub' || scenario === 'stale-live-granite') {
  state = {...state, client_kind:scenario === 'stale-live-stub' ? 'stub' : 'granite',
    model_name:scenario === 'stale-live-stub' ? 'stub' : 'granite4.2:3b',
    live_refresh_enabled:true, live_refresh_status:'stale', live_facts_fresh:false,
    source:'reviewer_live_paypal_sandbox_read', generation_attempted:true,
    review_status:'pending_human_review',
    draft:{text:'Earlier validated read, now stale.',digest:'fixture-digest',question_intent:'payment_status'}};
}
let failState = scenario === 'initial-unavailable';
const requests = [];
let held;
const response = value => ({ok:true, json:async()=>value});
async function fetch(path, options) {
  requests.push({path, method:options?.method || 'GET'});
  if (path === '/api/state') {
    if (scenario === 'initial-pending' && requests.length === 1) return new Promise(resolve => {held=resolve;});
    if (failState) throw new Error('local connection closed');
    return response({...state});
  }
  if (path === '/api/generate') {
    state = {...state, generation_attempted:true, draft:{text:'Historical facts. Current state unknown.', digest:'fixture-digest',question_intent:'payment_status'}};
    if (scenario === 'generate-lost-state-unavailable') failState=true;
    throw new Error('Response interrupted.');
  }
  if (path === '/api/review') { state={...state, review_status:'reviewed_not_sent'}; throw new Error('Response interrupted.'); }
  if (path === '/api/refresh') { state={...state, live_refresh_status:'failed',live_refresh_failure:{stage:'order_get',code:'ORDER_HTTP_REJECTED',http_status:404,call_counts:{oauth_attempts:1,pinned_order_get_attempts:1}}}; throw new Error('Refresh stopped.'); }
  throw new Error('Unexpected route');
}
const context={document:{getElementById:el},fetch};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function click(id) {assert.equal(el(id).disabled,false, id+' must be available');await el(id).handlers.click();await tick();}
(async()=>{
 vm.runInNewContext(script,context);
 if (scenario==='initial-pending') {
   assert.equal(el('generate').disabled,true);assert.equal(el('review').disabled,true);assert.equal(el('buyer-question').disabled,true);
   held(response(state));await tick();assert.equal(el('generate').disabled,false);
 } else {
   await tick();
   if (scenario==='initial-unavailable') {
     assert.equal(el('generate').disabled,true);assert.match(el('status').textContent,/unavailable/);
     failState=false;await click('reload-state');assert.equal(el('generate').disabled,false);
   } else if (scenario.startsWith('generate-lost')) {
     await click('generate');assert.equal(el('generate').disabled,true);
     if (failState) {assert.equal(el('review').disabled,true);assert.match(el('status').textContent,/unavailable/);failState=false;await click('reload-state');}
     assert.equal(el('review').disabled,false);assert.equal(el('buyer-question').disabled,true);
     assert.equal(requests.filter(r=>r.path==='/api/generate').length,1);
     if (scenario==='generate-lost-review-lost') {
       await click('review');assert.equal(el('review').disabled,true);assert.match(el('review-state').textContent,/Not sent/);
       assert.equal(requests.filter(r=>r.path==='/api/review').length,1);
     }
   } else if (scenario==='refresh-lost') {
     state={...state,live_refresh_enabled:true,live_refresh_status:'not_requested'};await click('reload-state');await click('refresh');
     assert.equal(el('generate').disabled,true);assert.equal(el('refresh').disabled,true);assert.match(el('refresh-state').textContent,/HTTP 404/);
     await click('reload-state');assert.equal(requests.filter(r=>r.path==='/api/refresh').length,1);
   } else if (scenario === 'stale-live-stub' || scenario === 'stale-live-granite') {
     assert.match(el('source').textContent,/Live sandbox read/);
     assert.match(el('facts-source-note').textContent,/stale/);
     assert.match(el('model-intro').textContent,/earlier validated.*now stale/i);
     assert.doesNotMatch(el('model-intro').textContent,/from the saved receipt/i);
     assert.equal(el('generate').disabled,true);
     assert.equal(el('review').disabled,true);
   } else throw new Error('Unknown scenario');
 }
 assert(requests.every(r=>r.path.startsWith('/api/')));
 assert.equal(requests.filter(r=>r.method==='POST').length,scenario==='generate-lost-review-lost'?2:scenario.startsWith('generate-lost')||scenario==='refresh-lost'?1:0);
 console.log('PASS '+scenario+' — simulated DOM/fetch, zero external requests');
})().catch(error=>{console.error(error);process.exitCode=1;});
